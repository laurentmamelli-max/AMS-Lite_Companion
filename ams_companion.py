#!/usr/bin/env python3
"""AMS Lite Companion - local filament usage tracker for Bambu printers.

Uses only the Python standard library.  It reads per-filament ``used_g`` from
a sliced Bambu/Orca .gcode.3mf and observes RUNNING -> FINISH over the
printer's local MQTT endpoint.  It never sends print commands.
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import io
import json
import os
import queue
import re
import secrets
import signal
import socket
import sqlite3
import ssl
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path.home() / "Library" / "Application Support" / "AMS Lite Companion"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "companion.log"
INVENTORY_FILE = APP_DIR / "inventory.sqlite3"
HOST, PORT = "127.0.0.1", 8765
__version__ = "1.4.1"
MAX_IMPORT_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 200
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 100
MAX_AUTO_IMPORT_AGE_SECONDS = 90
TERMINAL_OK = {"FINISH", "FINISHED", "COMPLETED", "COMPLETE"}
RUNNING = {"RUNNING", "PRINTING", "PREPARE", "PREPARING", "SLICING"}
TERMINAL_BAD = {"FAILED", "CANCEL", "CANCELLED", "CANCELED"}
TERMINAL_STATES = TERMINAL_OK | TERMINAL_BAD


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def secure_directory(path: Path) -> None:
    """Create the local data directory with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def log(message: str) -> None:
    line = f"{now_iso()} {message}\n"
    try:
        secure_directory(APP_DIR)
        with LOG_FILE.open("a", encoding="utf-8") as out:
            out.write(line)
        os.chmod(LOG_FILE, 0o600)
    except OSError:
        # Tests and read-only recovery environments may not expose a writable
        # macOS home directory. Runtime state still uses its explicit path.
        pass
    print(line, end="", flush=True)


def default_state() -> dict[str, Any]:
    return {
        "version": 2,
        "config": {"ip": "", "serial": "", "access_code": ""},
        "spools": {
            str(i): {"name": f"Bobine A{i}", "initial_g": 1000.0, "remaining_g": 1000.0}
            for i in range(1, 5)
        },
        "armed_job": None,
        "active_job": None,
        "accounted": [],
        "history": [],
        "printer": {"connected": False, "state": "INCONNU", "progress": 0, "job": "",
                    "rfid_status": "En attente de lecture RFID"},
        "bridge": {
            "enabled": True,
            "fallback_enabled": True,
            "default_mapping": {str(i): str(i) for i in range(1, 5)},
            "status": "En attente de Bambu Studio",
            "last_file": "",
            "last_sha256": "",
            "last_detected_at": "",
            "mapping_source": "",
            "request_capture": False,
        },
        "recovery_notice": "",
    }


def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    state = default_state()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            for key in state:
                if key not in loaded:
                    continue
                if key == "bridge" and isinstance(loaded[key], dict):
                    state[key].update(loaded[key])
                    defaults = default_state()["bridge"]["default_mapping"]
                    defaults.update(state[key].get("default_mapping", {}))
                    state[key]["default_mapping"] = defaults
                else:
                    state[key] = loaded[key]
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{path.stem}.corrompu-{stamp}{path.suffix}")
            index = 2
            while backup.exists():
                backup = path.with_name(f"{path.stem}.corrompu-{stamp}-{index}{path.suffix}")
                index += 1
            try:
                os.replace(path, backup)
                os.chmod(backup, 0o600)
                state["recovery_notice"] = (
                    f"État illisible sauvegardé dans {backup.name}. "
                    "La configuration doit être vérifiée avant utilisation."
                )
                log(f"État illisible sauvegardé: {backup.name} ({exc})")
            except OSError as backup_exc:
                state["recovery_notice"] = "État illisible: aucune donnée n’a été écrasée."
                log(f"État illisible, sauvegarde impossible: {backup_exc}")
    return state


def atomic_save(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    secure_directory(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def inventory_path_for_state(state_path: Path) -> Path:
    """Keep test and portable state files isolated from the real inventory."""
    return INVENTORY_FILE if state_path == STATE_FILE else state_path.with_name("inventory.sqlite3")


class Inventory:
    """Durable spool catalogue and the four temporary AMS assignments."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        secure_directory(self.path.parent)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return connection

    def initialize(self, legacy_spools: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS spools (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    material TEXT NOT NULL DEFAULT '',
                    brand TEXT NOT NULL DEFAULT '',
                    color TEXT NOT NULL DEFAULT '',
                    rfid_tag TEXT NOT NULL DEFAULT '',
                    rfid_info TEXT NOT NULL DEFAULT '',
                    initial_g REAL NOT NULL,
                    remaining_g REAL NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS slot_assignments (
                    slot TEXT PRIMARY KEY CHECK(slot IN ('1', '2', '3', '4')),
                    spool_id INTEGER NOT NULL UNIQUE REFERENCES spools(id),
                    assigned_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inventory_history (
                    id INTEGER PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    spool_id INTEGER REFERENCES spools(id),
                    slot TEXT,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS print_settlements (
                    settlement_key TEXT PRIMARY KEY,
                    deductions_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(spools)")}
            if "rfid_tag" not in columns:
                connection.execute("ALTER TABLE spools ADD COLUMN rfid_tag TEXT NOT NULL DEFAULT ''")
            if "rfid_info" not in columns:
                connection.execute("ALTER TABLE spools ADD COLUMN rfid_info TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS spools_rfid_tag ON spools(rfid_tag) WHERE rfid_tag != ''"
            )
            # Early builds stored the Bambu RGB value (for example #C12E1F).
            # The catalogue is user-facing, so migrate those values to a
            # readable French colour name as soon as it opens.
            for row in connection.execute("SELECT id, color FROM spools WHERE color GLOB '#[0-9A-Fa-f]*'"):
                connection.execute("UPDATE spools SET color = ? WHERE id = ?", (rfid_color(row["color"]), row["id"]))
            if connection.execute("SELECT COUNT(*) FROM spools").fetchone()[0]:
                return
            for slot in map(str, range(1, 5)):
                legacy = legacy_spools.get(slot, {})
                name = str(legacy.get("name") or f"Bobine A{slot}")[:80]
                initial_g = max(0.0, _float(legacy.get("initial_g", 1000)))
                remaining_g = max(0.0, _float(legacy.get("remaining_g", initial_g)))
                created_at = now_iso()
                cursor = connection.execute(
                    """
                    INSERT INTO spools(name, initial_g, remaining_g, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, initial_g, remaining_g, created_at, created_at),
                )
                spool_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO slot_assignments(slot, spool_id, assigned_at) VALUES (?, ?, ?)",
                    (slot, spool_id, created_at),
                )
                connection.execute(
                    """
                    INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at)
                    VALUES ('migration', ?, ?, 'Bobine existante importée depuis state.json', ?)
                    """,
                    (spool_id, slot, created_at),
                )

    @staticmethod
    def _spool_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "material": row["material"],
            "brand": row["brand"],
            "color": row["color"],
            "rfid_tag": row["rfid_tag"],
            "rfid_info": row["rfid_info"],
            "initial_g": row["initial_g"],
            "remaining_g": row["remaining_g"],
            "archived": bool(row["archived"]),
            "slot": row["slot"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def public_state(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT spools.*, slot_assignments.slot
                FROM spools
                LEFT JOIN slot_assignments ON slot_assignments.spool_id = spools.id
                WHERE spools.archived = 0
                ORDER BY spools.id DESC
                """
            ).fetchall()
        spools = [self._spool_dict(row) for row in rows]
        return {
            "spools": spools,
            "slots": {spool["slot"]: spool["id"] for spool in spools if spool["slot"]},
        }

    def slot_spools(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT spools.*, slot_assignments.slot
                FROM slot_assignments
                JOIN spools ON spools.id = slot_assignments.spool_id
                WHERE spools.archived = 0
                """
            ).fetchall()
        return {str(row["slot"]): self._spool_dict(row) for row in rows}

    def create_spool(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name", "")).strip()[:80]
        if not name:
            raise ValueError("Donnez un nom à la bobine")
        initial_g = max(0.0, _float(data.get("initial_g", 1000)))
        remaining_g = max(0.0, _float(data.get("remaining_g", initial_g)))
        created_at = now_iso()
        values = (
            name,
            str(data.get("material", "")).strip()[:40],
            str(data.get("brand", "")).strip()[:60],
            str(data.get("color", "")).strip()[:40],
            initial_g,
            remaining_g,
            created_at,
            created_at,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO spools(name, material, brand, color, initial_g, remaining_g, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            spool_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO inventory_history(event_type, spool_id, detail, created_at) VALUES ('create', ?, ?, ?)",
                (spool_id, "Nouvelle bobine", created_at),
            )
        return self.spool(spool_id)

    def spool(self, spool_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT spools.*, slot_assignments.slot
                FROM spools LEFT JOIN slot_assignments ON slot_assignments.spool_id = spools.id
                WHERE spools.id = ? AND spools.archived = 0
                """,
                (spool_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Bobine introuvable")
        return self._spool_dict(row)

    def history_for_spool(self, spool_id: int) -> dict[str, Any]:
        spool = self.spool(spool_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_type, slot, detail, created_at
                FROM inventory_history WHERE spool_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (spool_id,),
            ).fetchall()
        return {
            "spool": spool,
            "events": [{
                "id": row["id"], "type": row["event_type"], "slot": row["slot"],
                "detail": row["detail"], "created_at": row["created_at"],
            } for row in rows],
        }

    def update_spool(self, spool_id: int, data: dict[str, Any]) -> dict[str, Any]:
        current = self.spool(spool_id)
        name = str(data.get("name", current["name"])).strip()[:80]
        if not name:
            raise ValueError("Donnez un nom à la bobine")
        initial_g = max(0.0, _float(data.get("initial_g", current["initial_g"])))
        remaining_g = max(0.0, _float(data.get("remaining_g", current["remaining_g"])))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE spools
                SET name = ?, material = ?, brand = ?, color = ?, initial_g = ?, remaining_g = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    str(data.get("material", current["material"])).strip()[:40],
                    str(data.get("brand", current["brand"])).strip()[:60],
                    str(data.get("color", current["color"])).strip()[:40],
                    initial_g,
                    remaining_g,
                    now_iso(),
                    spool_id,
                ),
            )
        return self.spool(spool_id)

    def archive_spool(self, spool_id: int) -> dict[str, Any]:
        """Hide a spool from the catalogue while retaining its audit trail."""
        archived_at = now_iso()
        with self._connect() as connection:
            spool = connection.execute(
                "SELECT id, name FROM spools WHERE id = ? AND archived = 0", (spool_id,)
            ).fetchone()
            if spool is None:
                raise ValueError("Bobine introuvable")
            assignment = connection.execute(
                "SELECT slot FROM slot_assignments WHERE spool_id = ?", (spool_id,)
            ).fetchone()
            slot = str(assignment["slot"]) if assignment else None
            connection.execute("DELETE FROM slot_assignments WHERE spool_id = ?", (spool_id,))
            connection.execute(
                "UPDATE spools SET archived = 1, updated_at = ? WHERE id = ?",
                (archived_at, spool_id),
            )
            connection.execute(
                """
                INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at)
                VALUES ('archive', ?, ?, ?, ?)
                """,
                (spool_id, slot, "Bobine supprimée du catalogue", archived_at),
            )
        return {"message": f"{spool['name']} a été supprimée du catalogue."}

    def sync_rfid_slot(self, slot: str, data: dict[str, str]) -> tuple[dict[str, Any], bool]:
        """Associate an AMS slot with the physical RFID tag currently read there.

        A tag UID (or the printer-provided tray UUID) is required: material and
        colour alone are not enough to tell two identical rolls apart.
        """
        if slot not in {"1", "2", "3", "4"}:
            raise ValueError("Emplacement AMS invalide")
        tag = str(data.get("tag") or "").strip()[:128]
        if not tag:
            raise ValueError("Identifiant RFID absent")
        now = now_iso()
        name = str(data.get("name") or "Bobine Bambu Lab")[:80]
        material = str(data.get("material") or "")[:40]
        brand = str(data.get("brand") or "Bambu Lab")[:60]
        color = str(data.get("color") or "")[:40]
        info = str(data.get("info") or "")[:80]
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM spools WHERE rfid_tag = ? AND archived = 0", (tag,)
            ).fetchone()
            changed = False
            if row is None:
                # Preserve a migrated placeholder and its manually entered
                # weight when this is the first RFID reading for that slot.
                current = connection.execute(
                    """
                    SELECT spools.* FROM slot_assignments
                    JOIN spools ON spools.id = slot_assignments.spool_id
                    WHERE slot_assignments.slot = ? AND spools.archived = 0
                    """,
                    (slot,),
                ).fetchone()
                placeholder = current and (
                    current["name"] == f"Bobine A{slot}"
                    and not current["material"] and not current["brand"] and not current["color"]
                    and not current["rfid_tag"]
                )
                if placeholder:
                    spool_id = int(current["id"])
                    connection.execute(
                        """
                        UPDATE spools SET name = ?, material = ?, brand = ?, color = ?,
                        rfid_tag = ?, rfid_info = ?, updated_at = ? WHERE id = ?
                        """,
                        (name, material, brand, color, tag, info, now, spool_id),
                    )
                    detail = "Bobine existante associée au tag RFID"
                else:
                    cursor = connection.execute(
                        """
                        INSERT INTO spools(name, material, brand, color, rfid_tag, rfid_info,
                                           initial_g, remaining_g, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, 1000, 1000, ?, ?)
                        """,
                        (name, material, brand, color, tag, info, now, now),
                    )
                    spool_id = int(cursor.lastrowid)
                    detail = "Nouvelle bobine créée depuis le tag RFID"
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('rfid', ?, ?, ?, ?)",
                    (spool_id, slot, detail, now),
                )
                changed = True
            else:
                spool_id = int(row["id"])
                # Refresh the descriptive fields supplied by the printer but
                # preserve a name the owner may have personalised.
                connection.execute(
                    """
                    UPDATE spools SET material = CASE WHEN ? != '' THEN ? ELSE material END,
                    brand = CASE WHEN ? != '' THEN ? ELSE brand END,
                    color = CASE WHEN ? != '' THEN ? ELSE color END,
                    rfid_info = CASE WHEN ? != '' THEN ? ELSE rfid_info END, updated_at = ?
                    WHERE id = ?
                    """,
                    (material, material, brand, brand, color, color, info, info, now, spool_id),
                )
            assigned = connection.execute(
                "SELECT spool_id FROM slot_assignments WHERE slot = ?", (slot,)
            ).fetchone()
            if assigned is None or int(assigned["spool_id"]) != spool_id:
                connection.execute("DELETE FROM slot_assignments WHERE spool_id = ?", (spool_id,))
                connection.execute("DELETE FROM slot_assignments WHERE slot = ?", (slot,))
                connection.execute(
                    "INSERT INTO slot_assignments(slot, spool_id, assigned_at) VALUES (?, ?, ?)",
                    (slot, spool_id, now),
                )
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('assign', ?, ?, ?, ?)",
                    (spool_id, slot, "Bobine placée automatiquement après lecture RFID", now),
                )
                changed = True
        return self.spool(spool_id), changed

    def assign(self, slot: str, spool_id: int | None) -> dict[str, Any]:
        """Place a spool in an AMS slot without silently losing another one.

        Moving a spool onto an occupied slot exchanges the two positions when
        the selected spool already has one.  A repeated save is a no-op, so the
        UI can safely retry without duplicating inventory history.
        """
        if slot not in {"1", "2", "3", "4"}:
            raise ValueError("Emplacement AMS invalide")
        assigned_at = now_iso()
        with self._connect() as connection:
            if spool_id is None:
                current = connection.execute(
                    "SELECT spool_id FROM slot_assignments WHERE slot = ?", (slot,)
                ).fetchone()
                if current is None:
                    return {"action": "unchanged", "message": f"A{slot} est déjà libre."}
                connection.execute("DELETE FROM slot_assignments WHERE slot = ?", (slot,))
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('remove', ?, ?, ?, ?)",
                    (int(current["spool_id"]), slot, "Bobine retirée de l'AMS", assigned_at),
                )
                return {"action": "removed", "message": f"Bobine retirée de A{slot}."}
            selected = connection.execute(
                "SELECT id, name FROM spools WHERE id = ? AND archived = 0", (spool_id,)
            ).fetchone()
            if selected is None:
                raise ValueError("Bobine introuvable")
            source = connection.execute(
                "SELECT slot FROM slot_assignments WHERE spool_id = ?", (spool_id,)
            ).fetchone()
            occupant = connection.execute(
                """
                SELECT slot_assignments.spool_id, spools.name
                FROM slot_assignments JOIN spools ON spools.id = slot_assignments.spool_id
                WHERE slot_assignments.slot = ?
                """, (slot,),
            ).fetchone()
            source_slot = str(source["slot"]) if source else ""
            if occupant is not None and int(occupant["spool_id"]) == spool_id:
                return {"action": "unchanged", "message": f"{selected['name']} est déjà en A{slot}."}

            if source_slot:
                connection.execute("DELETE FROM slot_assignments WHERE slot = ?", (source_slot,))
            if occupant is not None:
                connection.execute("DELETE FROM slot_assignments WHERE slot = ?", (slot,))
            connection.execute(
                "INSERT INTO slot_assignments(slot, spool_id, assigned_at) VALUES (?, ?, ?)",
                (slot, spool_id, assigned_at),
            )
            if occupant is not None and source_slot:
                displaced_id = int(occupant["spool_id"])
                connection.execute(
                    "INSERT INTO slot_assignments(slot, spool_id, assigned_at) VALUES (?, ?, ?)",
                    (source_slot, displaced_id, assigned_at),
                )
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('assign', ?, ?, ?, ?)",
                    (spool_id, slot, f"Échange A{source_slot} → A{slot}", assigned_at),
                )
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('assign', ?, ?, ?, ?)",
                    (displaced_id, source_slot, f"Échange A{slot} → A{source_slot}", assigned_at),
                )
                return {
                    "action": "swapped",
                    "message": f"Échange effectué : {selected['name']} est en A{slot}, {occupant['name']} passe en A{source_slot}.",
                }
            if occupant is not None:
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('remove', ?, ?, ?, ?)",
                    (int(occupant["spool_id"]), slot, f"Remplacée par {selected['name']}", assigned_at),
                )
                detail = f"Placée en A{slot}, remplace {occupant['name']}"
                action = "replaced"
            elif source_slot:
                detail = f"Déplacée de A{source_slot} vers A{slot}"
                action = "moved"
            else:
                detail = "Bobine placée dans l'AMS"
                action = "placed"
            connection.execute(
                "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('assign', ?, ?, ?, ?)",
                (spool_id, slot, detail, assigned_at),
            )
        return {"action": action, "message": f"{selected['name']} est maintenant en A{slot}."}

    def unassign(self, spool_id: int) -> dict[str, Any]:
        assigned_at = now_iso()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT slot FROM slot_assignments WHERE spool_id = ?", (spool_id,)
            ).fetchone()
            if row is None:
                return {"action": "unchanged", "message": "Cette bobine est déjà hors AMS."}
            connection.execute("DELETE FROM slot_assignments WHERE spool_id = ?", (spool_id,))
            connection.execute(
                "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('remove', ?, ?, ?, ?)",
                (spool_id, row["slot"] if row else None, "Bobine retirée de l'AMS", assigned_at),
            )
        return {"action": "removed", "message": f"Bobine retirée de A{row['slot']}."}

    def spool_id_for_slot(self, slot: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT spool_id FROM slot_assignments WHERE slot = ?", (slot,)
            ).fetchone()
        return int(row["spool_id"]) if row else None

    def deduct(self, spool_id: int, used_g: float) -> tuple[float, float]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT remaining_g FROM spools WHERE id = ? AND archived = 0", (spool_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Bobine introuvable au moment du décompte")
            before = _float(row["remaining_g"])
            after = round(max(0.0, before - max(0.0, used_g)), 3)
            connection.execute(
                "UPDATE spools SET remaining_g = ?, updated_at = ? WHERE id = ?",
                (after, now_iso(), spool_id),
            )
            connection.execute(
                "INSERT INTO inventory_history(event_type, spool_id, detail, created_at) VALUES ('deduct', ?, ?, ?)",
                (spool_id, f"-{round(used_g, 3)} g · {round(before, 3)} → {round(after, 3)} g", now_iso()),
            )
        return before, after

    def settle_print(self, settlement_key: str, lines: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """Debit every spool once, in one SQLite transaction.

        The durable settlement key is the authority for idempotency.  It is
        deliberately stored with the inventory rather than only in state.json,
        so a crash between the debit and JSON save cannot charge a job twice.
        """
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT deductions_json FROM print_settlements WHERE settlement_key = ?", (settlement_key,)
            ).fetchone()
            if existing is not None:
                return json.loads(existing["deductions_json"]), False
            deductions: list[dict[str, Any]] = []
            for line in lines:
                spool_id = int(line["spool_id"])
                used_g = max(0.0, _float(line["used_g"]))
                row = connection.execute(
                    "SELECT remaining_g FROM spools WHERE id = ? AND archived = 0", (spool_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("Bobine introuvable au moment du décompte")
                before = _float(row["remaining_g"])
                after = round(max(0.0, before - used_g), 3)
                connection.execute(
                    "UPDATE spools SET remaining_g = ?, updated_at = ? WHERE id = ?",
                    (after, now_iso(), spool_id),
                )
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, detail, created_at) VALUES ('deduct', ?, ?, ?)",
                    (spool_id, f"-{round(used_g, 3)} g · {round(before, 3)} → {round(after, 3)} g", now_iso()),
                )
                deductions.append({
                    "slot": str(line["slot"]), "spool_id": spool_id, "used_g": used_g,
                    "before_g": before, "after_g": after,
                })
            connection.execute(
                "INSERT INTO print_settlements(settlement_key, deductions_json, created_at) VALUES (?, ?, ?)",
                (settlement_key, json.dumps(deductions, ensure_ascii=False), now_iso()),
            )
        return deductions, True


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def rfid_identity(value: Any) -> str:
    """Return a usable physical tag identifier, never Bambu's all-zero sentinel."""
    candidate = re.sub(r"[^0-9A-Za-z_-]", "", str(value or "")).upper()
    return "" if not candidate or set(candidate) == {"0"} else candidate


def rfid_color(value: Any) -> str:
    color = str(value or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9A-Fa-f]{8}", color):
        color = color[:6]
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        return color[:40]
    red, green, blue = (int(color[index:index + 2], 16) / 255 for index in (0, 2, 4))
    hue, saturation, brightness = colorsys.rgb_to_hsv(red, green, blue)
    if brightness < 0.16:
        return "Noir"
    if saturation < 0.13:
        if brightness > 0.9:
            return "Blanc"
        return "Gris clair" if brightness > 0.58 else "Gris"
    degrees = hue * 360
    if degrees < 15 or degrees >= 345:
        return "Rouge"
    if degrees < 42:
        return "Orange"
    if degrees < 68:
        return "Jaune"
    if degrees < 165:
        return "Vert"
    if degrees < 205:
        return "Cyan"
    if degrees < 258:
        return "Bleu"
    if degrees < 295:
        return "Violet"
    if degrees < 338:
        return "Rose"
    return "Rouge"


def rfid_slots(report: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    """Read the documented/observed AMS MQTT tray layouts without guessing a spool.

    A1/AMS Lite reports use the same ``print.ams.ams[].tray[]`` family as
    other Bambu models, while firmware revisions sometimes omit the outer AMS
    list. Only a non-zero tag UID or tray UUID qualifies as an RFID reading.
    """
    source = report.get("ams")
    groups: list[Any]
    if isinstance(source, dict):
        nested = source.get("ams")
        groups = nested if isinstance(nested, list) else [source]
    elif isinstance(source, list):
        groups = source
    else:
        return []
    result: list[tuple[str, dict[str, str]]] = []
    for group_index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        trays = group.get("tray") or group.get("trays") or []
        if not isinstance(trays, list):
            continue
        for tray_index, tray in enumerate(trays):
            if not isinstance(tray, dict):
                continue
            try:
                tray_id = int(tray.get("id", tray_index))
            except (TypeError, ValueError):
                tray_id = tray_index
            slot = group_index * 4 + tray_id + 1
            if slot not in {1, 2, 3, 4}:
                continue
            tag = rfid_identity(tray.get("tag_uid")) or rfid_identity(tray.get("tray_uuid"))
            if not tag:
                continue
            material = str(tray.get("tray_type") or tray.get("type") or "").strip()[:40]
            color = rfid_color(tray.get("tray_color") or tray.get("color"))
            brand = str(tray.get("tray_sub_brands") or "Bambu Lab").strip()[:60]
            label = str(tray.get("tray_id_name") or "").strip()[:80]
            name = label or " · ".join(part for part in (brand, material, color) if part)[:80] or "Bobine Bambu Lab"
            result.append((str(slot), {
                "tag": tag,
                "info": str(tray.get("tray_info_idx") or "").strip()[:80],
                "name": name,
                "material": material,
                "brand": brand,
                "color": color,
            }))
    return result


def parse_slice_info(data: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(data)
    plates: list[dict[str, Any]] = []
    plate_nodes = [node for node in root.iter() if local_name(node.tag) == "plate"]
    if not plate_nodes:
        plate_nodes = [root]
    for pidx, plate in enumerate(plate_nodes, 1):
        filaments: list[dict[str, Any]] = []
        seen: set[tuple[str, float]] = set()
        for node in plate.iter():
            if local_name(node.tag) != "filament":
                continue
            attrs = {local_name(k): v for k, v in node.attrib.items()}
            used = _float(attrs.get("used_g") or attrs.get("weight") or attrs.get("used_weight"))
            if used <= 0:
                continue
            fid = str(attrs.get("id") or attrs.get("filament_id") or len(filaments) + 1)
            key = (fid, round(used, 5))
            if key in seen:
                continue
            seen.add(key)
            filaments.append({
                "id": fid,
                "type": attrs.get("type") or attrs.get("filament_type") or "Filament",
                "color": attrs.get("color") or attrs.get("filament_color") or "",
                "used_g": round(used, 3),
            })
        plate_id = str(plate.attrib.get("id") or plate.attrib.get("index") or pidx)
        if filaments:
            plates.append({"id": plate_id, "filaments": filaments})
    return plates


def parse_gcode_weights(text: str) -> list[dict[str, Any]]:
    patterns = [
        r"total filament weight \[g\]\s*[:=]\s*([^\r\n;]+)",
        r"filament used \[g\]\s*[:=]\s*([^\r\n;]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        values = [_float(v) for v in re.split(r"[,; ]+", match.group(1).strip())]
        values = [v for v in values if v > 0]
        if values:
            return [{"id": str(i + 1), "type": "Filament", "color": "", "used_g": round(v, 3)}
                    for i, v in enumerate(values)]
    return []


def validate_3mf_archive(archive: zipfile.ZipFile) -> None:
    """Reject archives whose declared contents are unsafe to inspect locally."""
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("Archive 3MF trop complexe (trop de fichiers)")
    total = 0
    for entry in entries:
        if entry.is_dir():
            continue
        if entry.file_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("Archive 3MF trop volumineuse après décompression")
        if entry.compress_size and entry.file_size / entry.compress_size > MAX_ARCHIVE_COMPRESSION_RATIO:
            raise ValueError("Archive 3MF avec taux de compression anormal")
        total += entry.file_size
        if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("Archive 3MF trop volumineuse après décompression")


def extract_3mf_plates(archive: zipfile.ZipFile) -> list[dict[str, Any]]:
    validate_3mf_archive(archive)
    names = archive.namelist()
    slice_names = [n for n in names if n.lower().endswith("metadata/slice_info.config")]
    plates: list[dict[str, Any]] = []
    if slice_names:
        plates = parse_slice_info(archive.read(slice_names[0]))
    if not plates:
        for name in sorted(n for n in names if re.search(r"metadata/plate_\d+\.gcode$", n, re.I)):
            with archive.open(name) as gcode:
                text = gcode.read(250000).decode("utf-8", "replace")
            filaments = parse_gcode_weights(text)
            if filaments:
                number = re.search(r"plate_(\d+)", name, re.I).group(1)
                plates.append({"id": number, "filaments": filaments})
    return plates


def parsed_3mf_result(plates: list[dict[str, Any]], digest: str, filename: str) -> dict[str, Any]:
    if not plates:
        raise ValueError("Aucune consommation used_g trouvée. Exportez d’abord le plateau tranché en .gcode.3mf.")
    return {"filename": Path(filename).name, "sha256": digest, "plates": plates}


def parse_3mf(raw: bytes, filename: str = "travail.3mf") -> dict[str, Any]:
    if len(raw) > MAX_IMPORT_BYTES:
        raise ValueError("Fichier trop volumineux (32 Mo maximum)")
    digest = hashlib.sha256(raw).hexdigest()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        plates = extract_3mf_plates(archive)
    return parsed_3mf_result(plates, digest, filename)


def parse_3mf_path(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_IMPORT_BYTES:
            raise ValueError("Fichier trop volumineux (32 Mo maximum)")
    except OSError:
        raise
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    with zipfile.ZipFile(path) as archive:
        plates = extract_3mf_plates(archive)
    return parsed_3mf_result(plates, digest.hexdigest(), path.name)


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value % 128
        value //= 128
        if value:
            byte |= 0x80
        out.append(byte)
        if not value:
            return bytes(out)


def mqtt_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def read_varint(sock: ssl.SSLSocket) -> int:
    multiplier, value = 1, 0
    for _ in range(4):
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("Connexion MQTT fermée")
        value += (byte[0] & 127) * multiplier
        if not byte[0] & 128:
            return value
        multiplier *= 128
    raise ValueError("Longueur MQTT invalide")


def recv_exact(sock: ssl.SSLSocket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("Connexion MQTT fermée")
        data.extend(chunk)
    return bytes(data)


@dataclass
class MQTTConfig:
    ip: str
    serial: str
    access_code: str


class LocalMQTT(threading.Thread):
    def __init__(self, app: "Companion") -> None:
        super().__init__(name="local-mqtt", daemon=True)
        self.app = app
        self.stop_event = threading.Event()
        self.restart_event = threading.Event()

    def restart(self) -> None:
        self.restart_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.restart_event.set()

    def run(self) -> None:
        delay = 2
        while not self.stop_event.is_set():
            cfg = self.app.mqtt_config()
            if not cfg.ip or not cfg.serial or not cfg.access_code:
                self.restart_event.wait(2)
                self.restart_event.clear()
                continue
            try:
                self.session(cfg)
                delay = 2
            except Exception as exc:
                self.app.set_connected(False)
                log(f"MQTT déconnecté: {exc}")
                self.restart_event.wait(delay)
                self.restart_event.clear()
                delay = min(delay * 2, 30)

    def session(self, cfg: MQTTConfig) -> None:
        # Bambu printers commonly use a self-signed certificate.  Keep the
        # compatible TLS handshake, then pin the certificate on first trusted
        # connection and refuse any later substitution.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((cfg.ip, 8883), timeout=10)
        sock = context.wrap_socket(raw, server_hostname=cfg.ip)
        fingerprint = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
        self.app.verify_or_remember_mqtt_certificate(fingerprint)
        sock.settimeout(5)
        client_id = f"ams-companion-{os.getpid()}-{int(time.time())}"
        payload = mqtt_string(client_id) + mqtt_string("bblp") + mqtt_string(cfg.access_code)
        variable = mqtt_string("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 30)
        sock.sendall(bytes([0x10]) + encode_varint(len(variable) + len(payload)) + variable + payload)
        header = recv_exact(sock, 1)
        body = recv_exact(sock, read_varint(sock))
        if header[0] >> 4 != 2 or len(body) < 2 or body[1] != 0:
            raise ConnectionError(f"Authentification MQTT refusée ({body.hex()})")
        report_topic = f"device/{cfg.serial}/report"
        request_topic = f"device/{cfg.serial}/request"
        # Several A1/A1 mini firmwares close the entire MQTT connection when a
        # third-party client subscribes to the write-only ``request`` topic.
        # Subscribe only to the supported report channel; request remains the
        # publication target for pushall.
        sub = struct.pack("!H", 1) + mqtt_string(report_topic) + b"\x00"
        sock.sendall(bytes([0x82]) + encode_varint(len(sub)) + sub)
        request = json.dumps({"pushing": {"sequence_id": "1", "command": "pushall"}}, separators=(",", ":")).encode()
        publish = mqtt_string(request_topic) + request
        sock.sendall(bytes([0x30]) + encode_varint(len(publish)) + publish)
        self.app.set_connected(True)
        log(f"MQTT connecté à {cfg.ip} ({cfg.serial})")
        last_ping = time.monotonic()
        while not self.stop_event.is_set() and not self.restart_event.is_set():
            try:
                first = sock.recv(1)
                if not first:
                    raise ConnectionError("socket fermée")
                remaining = read_varint(sock)
                packet = recv_exact(sock, remaining)
                kind = first[0] >> 4
                if kind == 3 and len(packet) >= 2:
                    topic_len = struct.unpack("!H", packet[:2])[0]
                    offset = 2 + topic_len
                    if first[0] & 0x06:
                        offset += 2
                    try:
                        incoming_topic = packet[2:2 + topic_len].decode("utf-8", "replace")
                        self.app.on_mqtt_message(incoming_topic, json.loads(packet[offset:].decode("utf-8")))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        pass
            except socket.timeout:
                pass
            if time.monotonic() - last_ping > 20:
                sock.sendall(b"\xC0\x00")
                last_ping = time.monotonic()
        self.restart_event.clear()
        sock.close()


def default_bridge_roots() -> list[Path]:
    """Directories where Bambu Studio creates its automatic print archives."""
    home = Path.home()
    candidates = [
        Path(tempfile.gettempdir()) / "bamboo_model",
        home / "Library" / "Application Support" / "BambuStudio" / "tmp" / "bamboo_model",
        home / "Library" / "Application Support" / "BambuStudio" / "tmp",
    ]
    result: list[Path] = []
    for path in candidates:
        if path not in result:
            result.append(path)
    return result


def decode_ams_mapping(value: Any) -> list[int]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [item for item in re.split(r"[,; ]+", value.strip("[] ")) if item]
    if not isinstance(value, (list, tuple)):
        return []
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            return []
    return result


class StudioBridge(threading.Thread):
    """Watches the private print archive generated by official Bambu Studio."""

    def __init__(self, app: "Companion", roots: list[Path] | None = None,
                 poll_interval: float = 1.0, stable_seconds: float = 1.0) -> None:
        super().__init__(name="bambu-studio-bridge", daemon=True)
        self.app = app
        self.roots = roots or default_bridge_roots()
        self.poll_interval = poll_interval
        self.stable_seconds = stable_seconds
        self.stop_event = threading.Event()
        self.started_at = time.time()
        self.observed: dict[str, tuple[int, int, float]] = {}
        self.handled: dict[str, tuple[int, int]] = {}
        self.latest_handled_mtime_ns = 0

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        log("Passerelle Bambu Studio active")
        while not self.stop_event.wait(self.poll_interval):
            self.scan_once()

    def candidates(self) -> list[Path]:
        files: list[Path] = []
        for root in self.roots:
            try:
                if root.exists():
                    files.extend(path for path in root.rglob("*.3mf")
                                 if path.parent.name.lower() == "metadata"
                                 and not path.name.lower().endswith("_config.3mf"))
            except OSError as exc:
                log(f"Passerelle: dossier temporaire illisible {root}: {exc}")
        try:
            return sorted(set(files), key=lambda path: path.stat().st_mtime_ns, reverse=True)
        except OSError:
            return files

    def scan_once(self) -> None:
        now = time.time()
        for path in self.candidates():
            try:
                stat = path.stat()
            except OSError:
                continue
            # Ignore old archives already present before Companion started.
            if stat.st_mtime < self.started_at - 30:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            key = str(path)
            if stat.st_mtime_ns <= self.latest_handled_mtime_ns:
                continue
            if self.handled.get(key) == signature:
                continue
            previous = self.observed.get(key)
            if previous is None or previous[:2] != signature:
                self.observed[key] = (signature[0], signature[1], now)
                # This is the newest unhandled archive. Do not fall back to an
                # older one while Bambu Studio is still writing it.
                break
            if now - previous[2] < self.stable_seconds:
                break
            try:
                parsed = parse_3mf_path(path)
                after = path.stat()
            except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError):
                # Bambu Studio may still be writing the ZIP. A changed size or
                # timestamp will automatically trigger another attempt.
                break
            if (after.st_size, after.st_mtime_ns) != signature:
                self.observed[key] = (after.st_size, after.st_mtime_ns, now)
                break
            self.handled[key] = signature
            self.latest_handled_mtime_ns = stat.st_mtime_ns
            self.app.on_studio_archive(path, parsed)
            break
        self.app.bridge_tick()


class Companion:
    def __init__(self, state_path: Path = STATE_FILE,
                 bridge_roots: list[Path] | None = None) -> None:
        self.state_path = state_path
        self.lock = threading.RLock()
        self.state = load_state(state_path)
        self.inventory = Inventory(inventory_path_for_state(state_path))
        self.inventory.initialize(self.state["spools"])
        self._sync_spools_from_inventory()
        self.last_import: dict[str, Any] | None = None
        self.auto_import: dict[str, Any] | None = None
        self.pending_request: dict[str, Any] | None = None
        armed = self.state.get("armed_job")
        if armed and armed.get("auto_bridge"):
            armed_epoch = _float(armed.get("armed_epoch"))
            if not armed_epoch or time.time() - armed_epoch > 600:
                self.state["armed_job"] = None
                self.state["bridge"]["status"] = "Ancien armement automatique supprimé au démarrage"
                atomic_save(self.state, self.state_path)
        self.mqtt = LocalMQTT(self)
        self.bridge = StudioBridge(self, bridge_roots)

    def save(self) -> None:
        atomic_save(self.state, self.state_path)

    def _sync_spools_from_inventory(self) -> None:
        """Maintain the legacy A1-A4 view while SQLite owns spool records."""
        installed = self.inventory.slot_spools()
        self.state["spools"] = {
            slot: {
                "name": installed.get(slot, {}).get("name", f"A{slot} libre"),
                "initial_g": installed.get(slot, {}).get("initial_g", 0.0),
                "remaining_g": installed.get(slot, {}).get("remaining_g", 0.0),
                "spool_id": installed.get(slot, {}).get("id"),
            }
            for slot in map(str, range(1, 5))
        }

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            self._sync_spools_from_inventory()
            clean = json.loads(json.dumps(self.state))
            clean["config"]["access_code"] = "" if not self.state["config"].get("access_code") else "********"
            clean["imported"] = self.last_import
            clean["auto_import_available"] = self.auto_import is not None
            clean["inventory"] = self.inventory.public_state()
            return clean

    def mqtt_config(self) -> MQTTConfig:
        with self.lock:
            c = self.state["config"]
            return MQTTConfig(c.get("ip", ""), c.get("serial", ""), c.get("access_code", ""))

    def verify_or_remember_mqtt_certificate(self, fingerprint: str) -> None:
        with self.lock:
            config = self.state["config"]
            remembered = str(config.get("mqtt_certificate_sha256") or "")
            if remembered and not secrets.compare_digest(remembered, fingerprint):
                raise ConnectionError(
                    "Le certificat MQTT de l’imprimante a changé; vérifiez le réseau avant de le réinitialiser."
                )
            if not remembered:
                config["mqtt_certificate_sha256"] = fingerprint
                self.save()
                log("Certificat MQTT local épinglé pour les prochaines connexions")

    def set_connected(self, connected: bool) -> None:
        with self.lock:
            self.state["printer"]["connected"] = connected

    def on_mqtt_message(self, topic: str, payload: dict[str, Any]) -> None:
        if topic.endswith("/request"):
            self.on_print_request(payload)
        elif topic.endswith("/report"):
            self.on_message(payload)

    def _sync_rfid_from_report_locked(self, report: dict[str, Any]) -> bool:
        readings = rfid_slots(report)
        if not readings:
            return False
        changed = False
        synced = []
        for slot, data in readings:
            spool, slot_changed = self.inventory.sync_rfid_slot(slot, data)
            changed = changed or slot_changed
            synced.append(f"A{slot} : {spool['name']}")
        self._sync_spools_from_inventory()
        status = "RFID synchronisé — " + " · ".join(synced)
        changed = changed or self.state["printer"].get("rfid_status") != status
        self.state["printer"]["rfid_status"] = status
        return changed

    def on_print_request(self, payload: dict[str, Any]) -> None:
        report = payload.get("print")
        if not isinstance(report, dict) or "ams_mapping" not in report:
            return
        mapping = decode_ams_mapping(report.get("ams_mapping"))
        if not mapping:
            return
        source = str(report.get("param") or report.get("file") or report.get("url") or "")
        plate_match = re.search(r"plate_(\d+)\.gcode", source, re.I)
        with self.lock:
            self.pending_request = {
                "mapping": mapping,
                "plate": plate_match.group(1) if plate_match else "",
                "job": str(report.get("subtask_name") or report.get("project_name") or ""),
                "received_epoch": time.time(),
            }
            bridge = self.state["bridge"]
            bridge["request_capture"] = True
            bridge["status"] = "Commande d’impression Bambu Studio détectée"
            self._try_auto_arm_locked()
            self.save()

    def on_studio_archive(self, path: Path, parsed: dict[str, Any]) -> None:
        with self.lock:
            if not self.state["bridge"].get("enabled", True):
                return
            detected = dict(parsed)
            detected["source_path"] = str(path)
            detected["detected_epoch"] = time.time()
            self.auto_import = detected
            self.last_import = parsed
            bridge = self.state["bridge"]
            bridge["last_file"] = str(path)
            bridge["last_sha256"] = parsed["sha256"]
            bridge["last_detected_at"] = now_iso()
            bridge["status"] = "Fichier Bambu Studio récupéré — confirmation requise"
            log(f"Passerelle: archive détectée {path}")
            self._try_auto_arm_locked()
            self.save()

    def configure_bridge(self, data: dict[str, Any]) -> None:
        with self.lock:
            bridge = self.state["bridge"]
            if "enabled" in data:
                bridge["enabled"] = bool(data["enabled"])
            if "fallback_enabled" in data:
                bridge["fallback_enabled"] = bool(data["fallback_enabled"])
            incoming = data.get("default_mapping", {})
            for filament_id in map(str, range(1, 5)):
                slot = str(incoming.get(filament_id, bridge["default_mapping"].get(filament_id, filament_id)))
                if slot in {"1", "2", "3", "4"}:
                    bridge["default_mapping"][filament_id] = slot
            if not bridge["enabled"]:
                bridge["status"] = "Passerelle désactivée"
            elif not self.auto_import:
                bridge["status"] = "En attente de Bambu Studio"
            self._try_auto_arm_locked()
            self.save()

    def bridge_tick(self) -> None:
        with self.lock:
            if self._try_auto_arm_locked():
                self.save()

    def _mapping_from_request(self, filaments: list[dict[str, Any]]) -> dict[str, str]:
        request = self.pending_request
        if not request or not self.auto_import:
            return {}
        if abs(request["received_epoch"] - self.auto_import["detected_epoch"]) > MAX_AUTO_IMPORT_AGE_SECONDS:
            return {}
        values = request["mapping"]
        result: dict[str, str] = {}
        for position, filament in enumerate(filaments):
            filament_id = str(filament["id"])
            try:
                index = int(filament_id) - 1
            except ValueError:
                index = position
            if index < 0 or index >= len(values):
                return {}
            tray = values[index]
            if tray < 0 or tray > 3:
                return {}
            result[filament_id] = str(tray + 1)
        return result

    def _try_auto_arm_locked(self, force_fallback: bool = False) -> bool:
        bridge = self.state["bridge"]
        if not bridge.get("enabled", True) or not self.auto_import or self.state.get("active_job"):
            return False
        age = time.time() - self.auto_import["detected_epoch"]
        if age > MAX_AUTO_IMPORT_AGE_SECONDS:
            self.auto_import = None
            self.pending_request = None
            bridge["status"] = "Import automatique expiré — confirmation requise pour un nouveau travail"
            return True
        existing = self.state.get("armed_job")
        if existing and not existing.get("auto_bridge"):
            changed = bridge.get("status") != "Fichier détecté, travail manuel conservé"
            bridge["status"] = "Fichier détecté, travail manuel conservé"
            return changed

        plates = self.auto_import.get("plates", [])
        if not plates:
            return False
        requested_plate = self.pending_request.get("plate", "") if self.pending_request else ""
        plate = next((item for item in plates if str(item["id"]) == requested_plate), None)
        if plate is None and len(plates) == 1:
            plate = plates[0]
        if plate is None:
            changed = bridge.get("status") != "Fichier récupéré, plateau en attente"
            bridge["status"] = "Fichier récupéré, plateau en attente"
            return changed

        filaments = plate["filaments"]
        mapping = self._mapping_from_request(filaments)
        mapping_source = "Commande Bambu Studio"
        if not mapping:
            if not bridge.get("fallback_enabled", True) or not force_fallback:
                changed = bridge.get("status") != "Fichier récupéré — confirmation de la correspondance AMS requise"
                bridge["status"] = "Fichier récupéré — confirmation de la correspondance AMS requise"
                return changed
            defaults = bridge.get("default_mapping", {})
            mapping = {str(item["id"]): str(defaults.get(str(item["id"]), "")) for item in filaments}
            if any(slot not in {"1", "2", "3", "4"} for slot in mapping.values()):
                changed = bridge.get("status") != "Correspondance AMS à compléter"
                bridge["status"] = "Correspondance AMS à compléter"
                return changed
            mapping_source = "Correspondance enregistrée"

        lines = [{"slot": mapping[str(item["id"])], "used_g": item["used_g"], "filament": item}
                 for item in filaments]
        token = hashlib.sha256(f"{self.auto_import['sha256']}:{plate['id']}".encode()).hexdigest()
        if (existing and existing.get("auto_bridge") and existing.get("token") == token
                and existing.get("mapping_source") == mapping_source
                and existing.get("lines") == lines):
            return False
        job_name = ""
        if self.pending_request:
            job_name = self.pending_request.get("job", "")
        self.state["armed_job"] = {
            "token": token,
            "file": job_name or self.auto_import["filename"],
            "plate": str(plate["id"]),
            "lines": lines,
            "armed_at": now_iso(),
            "armed_epoch": time.time(),
            "auto_bridge": True,
            "mapping_source": mapping_source,
        }
        bridge["mapping_source"] = mapping_source
        bridge["status"] = f"Travail armé automatiquement ({mapping_source})"
        log(f"Passerelle: travail armé automatiquement, plateau {plate['id']}, source={mapping_source}")
        return True

    def confirm_auto_import(self) -> dict[str, Any]:
        """Explicitly arm the most recent Studio archive with saved slots."""
        with self.lock:
            if not self.auto_import:
                raise ValueError("Aucun fichier Bambu Studio récent à confirmer")
            if not self._try_auto_arm_locked(force_fallback=True):
                if not self.state.get("armed_job"):
                    raise ValueError("Le fichier ne peut pas être armé automatiquement")
            self.save()
            return self.state["armed_job"]

    def configure(self, data: dict[str, Any]) -> None:
        with self.lock:
            current = self.state["config"]
            current["ip"] = str(data.get("ip", current.get("ip", ""))).strip()
            serial = str(data.get("serial", current.get("serial", ""))).strip()
            if serial != current.get("serial", ""):
                current.pop("mqtt_certificate_sha256", None)
            current["serial"] = serial
            code = str(data.get("access_code", "")).strip()
            if code and code != "********":
                current["access_code"] = code
            self.save()
        self.mqtt.restart()

    def update_spools(self, data: dict[str, Any]) -> None:
        with self.lock:
            for slot in map(str, range(1, 5)):
                incoming = data.get(slot, {})
                spool_id = self.inventory.spool_id_for_slot(slot)
                if spool_id is not None and incoming:
                    self.inventory.update_spool(spool_id, incoming)
            self._sync_spools_from_inventory()
            self.save()

    def create_spool(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            spool = self.inventory.create_spool(data)
            self._sync_spools_from_inventory()
            self.save()
            return spool

    def assign_spool(self, data: dict[str, Any]) -> dict[str, Any]:
        slot = str(data.get("slot") or "")
        raw_spool_id = data.get("spool_id")
        spool_id = None if raw_spool_id in (None, "") else int(raw_spool_id)
        with self.lock:
            if not slot:
                if spool_id is None:
                    raise ValueError("Choisissez une bobine à retirer")
                result = self.inventory.unassign(spool_id)
            else:
                result = self.inventory.assign(slot, spool_id)
            self._sync_spools_from_inventory()
            self.save()
            return {"ok": True, **result}

    def update_inventory_spool(self, spool_id: int, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            spool = self.inventory.update_spool(spool_id, data)
            self._sync_spools_from_inventory()
            self.save()
            return spool

    def delete_inventory_spool(self, spool_id: int) -> dict[str, Any]:
        with self.lock:
            active_job = self.state.get("active_job") or {}
            active_spool_ids = {
                int(line["spool_id"])
                for line in active_job.get("lines", [])
                if line.get("spool_id") is not None
            }
            if spool_id in active_spool_ids:
                raise ValueError("Impossible de supprimer une bobine utilisée par l’impression en cours")
            result = self.inventory.archive_spool(spool_id)
            self._sync_spools_from_inventory()
            self.save()
            return {"ok": True, **result}

    def spool_history(self, spool_id: int) -> dict[str, Any]:
        with self.lock:
            return self.inventory.history_for_spool(spool_id)

    def import_3mf(self, raw: bytes, filename: str) -> dict[str, Any]:
        parsed = parse_3mf(raw, filename)
        with self.lock:
            self.last_import = parsed
        return parsed

    def arm(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if not self.last_import:
                raise ValueError("Importez d’abord un .gcode.3mf tranché")
            plate_id = str(data.get("plate", ""))
            plate = next((p for p in self.last_import["plates"] if str(p["id"]) == plate_id), None)
            if not plate:
                raise ValueError("Plateau introuvable")
            mappings = {str(m["filament_id"]): str(m["slot"]) for m in data.get("mappings", [])}
            lines = []
            for filament in plate["filaments"]:
                slot = mappings.get(str(filament["id"]))
                if slot not in {"1", "2", "3", "4"}:
                    raise ValueError(f"Associez le filament {filament['id']} à A1–A4")
                lines.append({"slot": slot, "used_g": filament["used_g"], "filament": filament})
            token = hashlib.sha256(f"{self.last_import['sha256']}:{plate_id}".encode()).hexdigest()
            self.state["armed_job"] = {
                "token": token, "file": self.last_import["filename"], "plate": plate_id,
                "lines": lines, "armed_at": now_iso(),
            }
            self.save()
            return self.state["armed_job"]

    def on_message(self, payload: dict[str, Any]) -> None:
        report = payload.get("print")
        if not isinstance(report, dict):
            return
        with self.lock:
            rfid_changed = self._sync_rfid_from_report_locked(report)
            if rfid_changed:
                self.save()
            printer = self.state["printer"]
            raw_state = (
                report.get("gcode_state")
                or report.get("print_status")
                or printer.get("state", "INCONNU")
            )
            state = str(raw_state).upper()
            task_id = str(report.get("subtask_id") or report.get("task_id") or "")
            active = self.state.get("active_job")

            # A terminal frame for an earlier task can arrive after the next
            # print has started (for example after a local MQTT reconnect).
            # Never let that stale frame change the UI state or debit the
            # currently active task.
            if (
                state in TERMINAL_STATES
                and active
                and task_id
                and active.get("task_id")
                and task_id != active["task_id"]
            ):
                log(
                    "État terminal ignoré pour un autre travail: "
                    f"task={task_id}, actif={active['task_id']}"
                )
                return

            printer["state"] = state
            printer["progress"] = max(
                0,
                min(100, int(_float(report.get("mc_percent", printer.get("progress", 0))))),
            )
            printer["job"] = str(
                report.get("subtask_name")
                or report.get("gcode_file")
                or printer.get("job", "")
            )
            if (state in RUNNING and active and task_id and active.get("task_id")
                    and task_id != active.get("task_id")):
                # Companion may have missed the terminal frame during a network
                # outage. Never charge that stale job against a newer print.
                self.state["history"].insert(0, {
                    **active,
                    "result": "REMPLACÉ",
                    "ended_at": now_iso(),
                    "deducted": False,
                })
                self.state["history"] = self.state["history"][:100]
                self.state["active_job"] = None
                log(f"Ancien travail abandonné sans déduction: task={active.get('task_id')} remplacé par {task_id}")
                self.save()
            if state in RUNNING and not self.state.get("active_job"):
                # Only a recent printer request can arm a replacement here.
                # Saved A1–A4 defaults always require explicit confirmation.
                self._try_auto_arm_locked()
            if state in RUNNING and self.state.get("armed_job") and not self.state.get("active_job"):
                active = json.loads(json.dumps(self.state["armed_job"]))
                missing_slots = []
                for line in active["lines"]:
                    spool_id = self.inventory.spool_id_for_slot(line["slot"])
                    line["spool_id"] = spool_id
                    if spool_id is None:
                        missing_slots.append(f"A{line['slot']}")
                if missing_slots:
                    active["tracking_error"] = (
                        "Bobine non enregistrée au démarrage : " + ", ".join(missing_slots)
                    )
                    log(active["tracking_error"])
                active.update({"task_id": task_id, "started_at": now_iso(), "saw_running": True})
                self.state["active_job"] = active
                self.state["armed_job"] = None
                log(f"Travail détecté: {active['file']} plateau {active['plate']} task={task_id or '?'}")
                self.save()
            active = self.state.get("active_job")
            if not active:
                return
            if task_id and not active.get("task_id"):
                active["task_id"] = task_id
            if state in TERMINAL_BAD:
                self.state["history"].insert(0, {**active, "result": state, "ended_at": now_iso(), "deducted": False})
                self.state["history"] = self.state["history"][:100]
                self.state["active_job"] = None
                self.auto_import = None
                self.pending_request = None
                self.state["bridge"]["status"] = "Impression arrêtée, en attente de Bambu Studio"
                log(f"Travail {state}: aucune déduction")
                self.save()
            elif state in TERMINAL_OK and active.get("saw_running"):
                key = f"{self.state['config'].get('serial','')}:{active.get('task_id') or active['token']}"
                missing_slots = [
                    line["slot"]
                    for line in active["lines"]
                    if not line.get("spool_id") and not self.inventory.spool_id_for_slot(line["slot"])
                ]
                if missing_slots:
                    self.state["history"].insert(0, {
                        **active,
                        "result": "SUIVI_INCOMPLET",
                        "ended_at": now_iso(),
                        "deducted": False,
                    })
                    self.state["history"] = self.state["history"][:100]
                    self.state["active_job"] = None
                    self.auto_import = None
                    self.pending_request = None
                    self.state["bridge"]["status"] = "Impression terminée sans décompte (bobine non enregistrée)"
                    log(f"Travail terminé sans décompte: bobine absente dans A{', A'.join(missing_slots)}")
                    self.save()
                    return
                if key not in self.state["accounted"]:
                    settlement_lines = []
                    for line in active["lines"]:
                        spool_id = line.get("spool_id") or self.inventory.spool_id_for_slot(line["slot"])
                        if spool_id is None:
                            raise ValueError(f"Aucune bobine enregistrée dans A{line['slot']}")
                        settlement_lines.append({
                            "slot": line["slot"], "spool_id": int(spool_id), "used_g": line["used_g"],
                        })
                    deductions, newly_settled = self.inventory.settle_print(key, settlement_lines)
                    self._sync_spools_from_inventory()
                    self.state["accounted"].append(key)
                    self.state["accounted"] = self.state["accounted"][-1000:]
                    self.state["history"].insert(0, {**active, "result": state, "ended_at": now_iso(), "deducted": True, "deductions": deductions})
                    log(f"Travail {'terminé et débité' if newly_settled else 'déjà comptabilisé'}: {key}")
                self.state["history"] = self.state["history"][:100]
                self.state["active_job"] = None
                self.auto_import = None
                self.pending_request = None
                self.state["bridge"]["status"] = "Impression terminée, en attente de Bambu Studio"
                self.save()


class Handler(BaseHTTPRequestHandler):
    server_version = f"AMSLiteCompanion/{__version__}"

    @property
    def app(self) -> Companion:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    @property
    def api_token(self) -> str:
        return self.server.api_token  # type: ignore[attr-defined]

    def _local_request_is_valid(self) -> bool:
        port = self.server.server_port
        allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if self.headers.get("Host", "") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in allowed_origins:
            return False
        supplied = self.headers.get("X-AMS-Token", "")
        return bool(supplied) and secrets.compare_digest(supplied, self.api_token)

    def _require_api_access(self) -> bool:
        if self._local_request_is_valid():
            return True
        self.send_json({"error": "Accès local non autorisé"}, 403)
        return False

    def send_json(self, value: Any, status: int = 200) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Longueur de requête invalide") from exc
        if not 0 <= length <= MAX_IMPORT_BYTES:
            raise ValueError("Fichier trop volumineux (32 Mo maximum)")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("Requête incomplète")
        return body

    def json_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            raise ValueError("Content-Type application/json requis")
        value = json.loads(self.body())
        if not isinstance(value, dict):
            raise ValueError("Objet JSON requis")
        return value

    def do_GET(self) -> None:
        request_url = urllib.parse.urlparse(self.path)
        path = request_url.path
        if self.path == "/" or self.path.startswith("/?"):
            raw = render_html(self.api_token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; object-src 'none'")
            self.end_headers()
            self.wfile.write(raw)
        elif path == "/api/health":
            self.send_json({"ok": True})
        elif not self._require_api_access():
            return
        elif path == "/api/state":
            self.send_json(self.app.public_state())
        elif match := re.fullmatch(r"/api/inventory/spools/(\d+)/history", path):
            self.send_json(self.app.spool_history(int(match.group(1))))
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        try:
            request_url = urllib.parse.urlparse(self.path)
            path = request_url.path
            if not self._require_api_access():
                return
            if path == "/api/config":
                self.app.configure(self.json_body())
                self.send_json({"ok": True})
            elif path == "/api/bridge":
                self.app.configure_bridge(self.json_body())
                self.send_json({"ok": True})
            elif path == "/api/spools":
                self.app.update_spools(self.json_body())
                self.send_json({"ok": True})
            elif path == "/api/inventory/spools":
                self.send_json(self.app.create_spool(self.json_body()), 201)
            elif path == "/api/inventory/assign":
                self.send_json(self.app.assign_spool(self.json_body()))
            elif match := re.fullmatch(r"/api/inventory/spools/(\d+)/archive", path):
                self.json_body()
                self.send_json(self.app.delete_inventory_spool(int(match.group(1))))
            elif match := re.fullmatch(r"/api/inventory/spools/(\d+)", path):
                self.send_json(self.app.update_inventory_spool(int(match.group(1)), self.json_body()))
            elif path == "/api/import":
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type not in {"application/octet-stream", "application/zip", ""}:
                    raise ValueError("Type de fichier 3MF invalide")
                query = urllib.parse.parse_qs(request_url.query)
                filename = query.get("filename", ["travail.3mf"])[0]
                self.send_json(self.app.import_3mf(self.body(), filename))
            elif path == "/api/arm":
                self.send_json(self.app.arm(self.json_body()))
            elif path == "/api/bridge/confirm":
                self.json_body()
                self.send_json(self.app.confirm_auto_import())
            elif path == "/api/shutdown":
                self.json_body()
                self.send_json({"ok": True, "message": "Companion arrêté proprement"})
                log("Arrêt demandé depuis le tableau de bord")
                # shutdown() must run outside the request-handling thread.
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_error(404)
        except Exception as exc:
            log(f"Erreur API {self.path}: {exc}")
            self.send_json({"error": "Requête invalide ou donnée non exploitable"}, 400)


HTML = r'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AMS Lite Companion</title><style>
body.embedded .spools-card{order:1!important}body.embedded .printer-card{order:2!important}.inventory-card{display:none}.catalog-fields{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.catalog-actions button{width:100%}#catalogWindow{display:none}.catalog-window{max-width:1400px;margin:auto}.catalog-toolbar{display:flex;justify-content:space-between;align-items:end;gap:18px}.catalog-toolbar h2{font-size:24px;margin:0}.table-wrap{overflow:auto;border:1px solid #dfe3e7;border-radius:12px;background:white}.catalog-table{width:100%;border-collapse:collapse;min-width:1020px}.catalog-table th{background:#f0f3f5;color:#4e5863;text-align:left;font-size:12px;white-space:nowrap}.catalog-table th,.catalog-table td{padding:9px;border-bottom:1px solid #e7eaed;vertical-align:middle}.catalog-table tr:last-child td{border-bottom:0}.catalog-table tr[data-spool]{cursor:pointer}.catalog-table tr.selected td{background:#eaf8ef}.catalog-table input,.catalog-table select{min-width:90px;padding:7px;border:1px solid transparent;background:transparent;border-radius:6px}.catalog-table input:focus,.catalog-table select:focus{background:white;border-color:#00ae42;outline:none}.catalog-table .id-cell{color:#69717b;font-variant-numeric:tabular-nums}.catalog-table .actions{white-space:nowrap}.catalog-table .actions button{margin:0 3px 0 0;padding:8px 10px;font-size:12px}.catalog-add{display:grid;grid-template-columns:1.5fr repeat(3,1fr) .8fr .8fr auto;gap:8px;align-items:end;margin-top:14px;padding:14px;background:white;border:1px solid #dfe3e7;border-radius:12px}.catalog-add label{margin-top:0}.spool-timeline{margin-top:16px;padding:16px;background:white;border:1px solid #dfe3e7;border-radius:12px}.spool-timeline h3{margin:0 0 4px}.timeline{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(170px,1fr);gap:12px;overflow-x:auto;padding:26px 4px 6px;position:relative}.timeline:before{content:'';position:absolute;left:26px;right:26px;top:34px;height:3px;background:#cdebd8}.timeline-event{position:relative;z-index:1;padding-top:20px}.timeline-dot{position:absolute;top:0;left:12px;width:18px;height:18px;border-radius:50%;background:#00ae42;border:4px solid #eaf8ef}.timeline-event.remove .timeline-dot{background:#ef9b20}.timeline-event.deduct .timeline-dot{background:#3976db}.timeline-event .when{font-size:11px;color:#69717b}.timeline-event .what{font-weight:700;font-size:13px;margin:5px 0}.timeline-event .detail{font-size:12px;color:#505861}.timeline-empty{color:#69717b;padding:16px 0}body.catalog-view .wrap{max-width:none;padding:20px}body.catalog-view h1,body.catalog-view .sub,body.catalog-view .grid{display:none}body.catalog-view #catalogWindow{display:block}@media(max-width:700px){.catalog-fields{grid-template-columns:1fr 1fr}.catalog-add{grid-template-columns:1fr 1fr}}
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#20242a;background:#f4f5f6}body{margin:0}.wrap{max-width:1050px;margin:auto;padding:24px}h1{margin:0 0 4px}.sub{color:#69717b;margin-bottom:20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}.card{background:white;border:1px solid #dfe3e7;border-radius:14px;padding:18px;box-shadow:0 2px 10px #0000000b}.wide{grid-column:1/-1}h2{font-size:17px;margin:0 0 14px}label{display:block;font-size:12px;color:#656d76;margin:9px 0 4px}input,select,button{box-sizing:border-box;border:1px solid #cbd1d7;border-radius:8px;padding:9px;font:inherit}input,select{width:100%}input[type=checkbox]{width:auto;margin-right:7px}button{background:#00ae42;color:white;border:0;font-weight:600;cursor:pointer;margin-top:12px}button.secondary{background:#59636e}.status{display:inline-flex;gap:7px;align-items:center;font-weight:600}.dot{width:10px;height:10px;border-radius:50%;background:#d33}.on .dot{background:#00ae42}.spools{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.spool{padding:12px;border:1px solid #e1e4e7;border-radius:10px}.spool b{color:#00a23d}.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.bridge-map,.catalog-form{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.catalog{display:grid;grid-template-columns:1fr 160px;gap:12px;align-items:end;border-top:1px solid #eee;padding:12px 0}.catalog:first-child{border-top:0;padding-top:0}.check{font-size:14px;color:#20242a}.notice{padding:10px;border-radius:8px;background:#eef8f1;margin:10px 0}.error{background:#ffecec;color:#a11}.muted{color:#69717b;font-size:13px;overflow-wrap:anywhere}.line{display:grid;grid-template-columns:1fr 100px 90px;gap:8px;align-items:end}.history{font-size:13px;border-top:1px solid #eee;padding:8px 0}body.embedded .wrap{padding:10px;max-width:none}body.embedded h1,body.embedded .sub,body.embedded .manual-card,body.embedded .shutdown-card,body.embedded .inventory-card{display:none}body.embedded .grid{grid-template-columns:1fr;gap:10px}body.embedded .wide{grid-column:auto}body.embedded .card{padding:14px;border-radius:10px;box-shadow:none}body.embedded .spools-card{order:1}body.embedded .printer-card{order:2}body.embedded .bridge-card{order:3}body.embedded .history-card{order:4}@media(max-width:700px){.spools,.bridge-map,.catalog-form{grid-template-columns:1fr 1fr}.catalog{grid-template-columns:1fr}.line{grid-template-columns:1fr}.wrap{padding:12px}}</style></head><body><div class="wrap">
<h1>AMS Lite Companion</h1><div class="sub">Compteur local v1.4.1 — panneau natif lié à Bambu Studio officiel.</div><div id="msg"></div>
<div class="grid"><section class="card printer-card"><h2>Imprimante locale</h2><div id="conn" class="status"><span class="dot"></span><span>Déconnectée</span></div><div id="pstate"></div>
<label>Adresse IP</label><input id="ip" placeholder="192.168.1.50"><label>Numéro de série</label><input id="serial" placeholder="01S00A..."><label>Code d’accès LAN <span class="muted">(laisse vide pour conserver le code enregistré)</span></label><input id="code" type="password" placeholder="8 chiffres"><button onclick="saveConfig()">Enregistrer et connecter</button></section>
<section class="card bridge-card"><h2>Passerelle Bambu Studio</h2><div id="bridgeStatus" class="notice">En attente de Bambu Studio</div><label class="check"><input id="autoEnabled" type="checkbox">Récupérer automatiquement le .gcode.3mf</label><label class="check"><input id="fallbackEnabled" type="checkbox">Armer avec la correspondance A1–A4 enregistrée ci-dessous</label><div class="bridge-map" id="bridgeMap"></div><button onclick="saveBridge()">Enregistrer la passerelle</button><div id="bridgeDetails" class="muted"></div></section>
<section class="card wide manual-card"><h2>Import manuel de secours</h2><label>Fichier tranché .gcode.3mf</label><input id="file" type="file" accept=".3mf"><div id="imported"></div><button onclick="importFile()">Analyser le fichier</button><div id="mapping"></div></section>
<section class="card wide spools-card"><h2>Bobines actuellement dans l’AMS Lite</h2><div id="rfidStatus" class="muted">En attente de lecture RFID</div><div class="spools" id="spools"></div><button onclick="saveSpools()">Enregistrer les poids</button><button class="secondary" onclick="openCatalog()">Gérer le catalogue de bobines…</button></section>
<section class="card wide history-card"><h2>Historique</h2><div id="history">Aucun travail comptabilisé.</div></section>
<section class="card wide shutdown-card"><h2>Companion</h2><p>Utilise ce bouton après l’impression pour enregistrer et arrêter complètement Companion.</p><button class="secondary" onclick="shutdownCompanion()">Arrêter Companion</button></section></div><section id="catalogWindow" class="catalog-window"><div class="catalog-toolbar"><div><h2>Catalogue de bobines</h2><p class="muted">Une ligne par bobine. Son poids est conservé quand elle sort de l’AMS. Clique une ligne pour voir sa frise.</p></div></div><div class="table-wrap"><table class="catalog-table"><thead><tr><th>#</th><th>Nom</th><th>Matière</th><th>Marque</th><th>Couleur</th><th>Initial (g)</th><th>Restant (g)</th><th>Dans l’AMS</th><th>Actions</th></tr></thead><tbody id="catalog"></tbody></table></div><div class="catalog-add"><div><label>Nom</label><input id="newSpoolName" placeholder="PLA rouge mat"></div><div><label>Matière</label><input id="newSpoolMaterial" placeholder="PLA"></div><div><label>Marque</label><input id="newSpoolBrand" placeholder="Bambu Lab"></div><div><label>Couleur</label><input id="newSpoolColor" placeholder="Rouge"></div><div><label>Initial (g)</label><input id="newSpoolInitial" type="number" min="0" step="0.1" value="1000"></div><div><label>Restant (g)</label><input id="newSpoolRemaining" type="number" min="0" step="0.1" value="1000"></div><button onclick="createSpool()">Ajouter</button></div><section class="spool-timeline"><h3 id="timelineTitle">Historique de la bobine</h3><p id="timelineSummary" class="muted">Clique une ligne du catalogue pour afficher sa frise chronologique.</p><div id="timeline" class="timeline-empty">Aucune bobine sélectionnée.</div></section></section></div>
<script>
const embedded=new URLSearchParams(location.search).get('embedded')==='1',catalogView=new URLSearchParams(location.search).get('catalog')==='1',apiToken='__API_TOKEN__';if(embedded)document.body.classList.add('embedded');if(catalogView)document.body.classList.add('catalog-view');let S=null, imported=null, formDirty=false, selectedSpoolId=null;const $=id=>document.getElementById(id);function msg(t,e=false){$('msg').textContent=t||'';$('msg').className=t?`notice ${e?'error':''}`:''}function openCatalog(){if(window.webkit?.messageHandlers?.companion)window.webkit.messageHandlers.companion.postMessage('openCatalog');else window.open('/?catalog=1','ams-lite-catalog')}
async function api(path,opt={}){let headers=new Headers(opt.headers||{});headers.set('X-AMS-Token',apiToken);if(opt.body&&typeof opt.body==='string'&&!headers.has('Content-Type'))headers.set('Content-Type','application/json');let r=await fetch(path,{...opt,headers,credentials:'same-origin'}),j=await r.json();if(!r.ok)throw Error(j.error||'Erreur');return j}
function render(s){S=s;if(catalogView){if(!formDirty)renderCatalog(s.inventory);return}$('conn').className='status '+(s.printer.connected?'on':'');$('conn').lastElementChild.textContent=s.printer.connected?'Connectée':'Déconnectée';$('pstate').textContent=`${s.printer.state||''} ${s.printer.progress||0}% ${s.printer.job||''}`;$('rfidStatus').textContent=s.printer.rfid_status||'En attente de lecture RFID';
if(!formDirty){$('ip').value=s.config.ip||'';$('serial').value=s.config.serial||'';$('code').placeholder=s.config.access_code?'Code enregistré':'8 chiffres';
$('autoEnabled').checked=!!s.bridge.enabled;$('fallbackEnabled').checked=!!s.bridge.fallback_enabled;
$('bridgeMap').innerHTML=[1,2,3,4].map(i=>`<div><label>Filament ${i}</label><select id="bm${i}">${[1,2,3,4].map(slot=>`<option value="${slot}" ${String(s.bridge.default_mapping[i])==String(slot)?'selected':''}>A${slot}</option>`).join('')}</select></div>`).join('');
$('spools').innerHTML=[1,2,3,4].map(i=>{let x=s.spools[i]||{};return x.spool_id?`<div class="spool"><b>A${i}</b><label>Nom</label><input id="n${i}" value="${esc(x.name)}"><div class="row"><div><label>Initial (g)</label><input id="i${i}" type="number" step="0.1" value="${x.initial_g}"></div><div><label>Restant (g)</label><input id="r${i}" type="number" step="0.1" value="${x.remaining_g}"></div></div></div>`:`<div class="spool"><b>A${i}</b><div class="muted">Libre — choisis une bobine dans le catalogue.</div></div>`}).join('');}
if(!formDirty)renderCatalog(s.inventory);
$('bridgeStatus').textContent=s.bridge.status||'En attente de Bambu Studio';let bd=[];if(s.bridge.last_file)bd.push(`Dernier fichier : ${s.bridge.last_file}`);if(s.bridge.mapping_source)bd.push(`Correspondance : ${s.bridge.mapping_source}`);if(s.bridge.request_capture)bd.push('Capture des commandes AMS disponible sur ce Mac');let bj=s.active_job?.auto_bridge?s.active_job:s.armed_job?.auto_bridge?s.armed_job:null;if(bj)bd.push('Décompte : '+bj.lines.map(x=>`filament ${x.filament.id} → A${x.slot} (${x.used_g} g)`).join(', '));$('bridgeDetails').innerHTML=bd.map(esc).join('<br>');
let active=s.active_job?`En cours : ${esc(s.active_job.file)} — plateau ${s.active_job.plate}`:s.armed_job?`Armé : ${esc(s.armed_job.file)} — en attente de RUNNING`:'Aucun travail armé';let confirm=s.auto_import_available&&!s.active_job&&!s.armed_job?'<button onclick="confirmDetectedImport()">Confirmer le travail détecté</button>':'';$('imported').innerHTML=`<div class="notice">${active}</div>${confirm}`;
$('history').innerHTML=s.history.length?s.history.map(h=>`<div class="history"><b>${esc(h.file||'Travail')}</b> — ${esc(h.result)} — ${h.deducted?'déduction effectuée':'aucune déduction'}<br>${esc(h.ended_at||'')}</div>`).join(''):'Aucun travail comptabilisé.'}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function renderCatalog(inventory){let spools=inventory?.spools||[],occupants=Object.fromEntries(spools.filter(x=>x.slot).map(x=>[String(x.slot),x]));let label=(slot,x)=>{let other=occupants[String(slot)];return other&&other.id!==x.id?`A${slot} · échange avec ${other.name}`:`A${slot}${other?' · position actuelle':''}`};$('catalog').innerHTML=spools.length?spools.map(x=>`<tr data-spool="${x.id}" class="${selectedSpoolId===x.id?'selected':''}" onclick="selectSpool(${x.id})"><td class="id-cell">#${x.id}</td><td><input onclick="event.stopPropagation()" id="cn${x.id}" value="${esc(x.name)}"></td><td><input onclick="event.stopPropagation()" id="cm${x.id}" value="${esc(x.material)}"></td><td><input onclick="event.stopPropagation()" id="cb${x.id}" value="${esc(x.brand)}"></td><td><input onclick="event.stopPropagation()" id="cc${x.id}" value="${esc(x.color)}"></td><td><input onclick="event.stopPropagation()" id="ci${x.id}" type="number" min="0" step="0.1" value="${x.initial_g}"></td><td><input onclick="event.stopPropagation()" id="cr${x.id}" type="number" min="0" step="0.1" value="${x.remaining_g}"></td><td><select onclick="event.stopPropagation()" id="catalogSlot${x.id}"><option value="" ${!x.slot?'selected':''}>Hors AMS</option>${[1,2,3,4].map(slot=>`<option value="${slot}" ${String(x.slot)===String(slot)?'selected':''}>${esc(label(slot,x))}</option>`).join('')}</select></td><td class="actions"><button onclick="saveCatalogSpool(${x.id},event)">Enregistrer</button><button class="secondary" onclick="event.stopPropagation();selectSpool(${x.id})">Historique</button><button class="secondary" onclick="deleteSpool(${x.id},event)">Supprimer</button></td></tr>`).join(''):'<tr><td colspan="9" class="muted">Aucune bobine dans le catalogue.</td></tr>'}
function timelineLabel(type){return({migration:'Catalogue initialisé',create:'Bobine ajoutée',rfid:'RFID lu',assign:'Placée dans l’AMS',remove:'Retirée de l’AMS',archive:'Supprimée du catalogue',deduct:'Impression comptabilisée'})[type]||type}
function timelineDate(value){let date=new Date(value);return Number.isNaN(date.getTime())?esc(value):date.toLocaleString('fr-FR',{dateStyle:'medium',timeStyle:'short'})}
function renderTimeline(data){let spool=data.spool,events=data.events||[];$('timelineTitle').textContent='Historique · '+spool.name;$('timelineSummary').textContent=`${spool.remaining_g} g restants sur ${spool.initial_g} g${spool.slot?` · actuellement en A${spool.slot}`:' · hors AMS'}`;$('timeline').className='timeline';$('timeline').innerHTML=events.length?events.map(event=>`<article class="timeline-event ${esc(event.type)}"><span class="timeline-dot"></span><div class="when">${timelineDate(event.created_at)}</div><div class="what">${esc(timelineLabel(event.type))}${event.slot?` · A${esc(event.slot)}`:''}</div><div class="detail">${esc(event.detail||'')}</div></article>`).join(''):'<div class="timeline-empty">Aucun événement pour cette bobine.</div>'}
async function selectSpool(id){selectedSpoolId=id;if(S?.inventory&&!formDirty)renderCatalog(S.inventory);try{renderTimeline(await api('/api/inventory/spools/'+id+'/history'))}catch(e){msg(e.message,true)}}
async function refresh(){try{render(await api('/api/state'))}catch(e){msg(e.message,true)}}const refreshTimer=setInterval(refresh,3000);
async function saveConfig(){try{await api('/api/config',{method:'POST',body:JSON.stringify({ip:$('ip').value,serial:$('serial').value,access_code:$('code').value})});formDirty=false;msg('Configuration enregistrée.');refresh()}catch(e){msg(e.message,true)}}
async function saveBridge(){let m={};for(let i=1;i<=4;i++)m[i]=$('bm'+i).value;try{await api('/api/bridge',{method:'POST',body:JSON.stringify({enabled:$('autoEnabled').checked,fallback_enabled:$('fallbackEnabled').checked,default_mapping:m})});formDirty=false;msg('Passerelle enregistrée.');refresh()}catch(e){msg(e.message,true)}}
async function saveSpools(){let x={};for(let i=1;i<=4;i++)if(S.spools[i]?.spool_id)x[i]={name:$('n'+i).value,initial_g:+$('i'+i).value,remaining_g:+$('r'+i).value};try{await api('/api/spools',{method:'POST',body:JSON.stringify(x)});formDirty=false;msg('Poids enregistrés.');refresh()}catch(e){msg(e.message,true)}}
async function createSpool(){try{await api('/api/inventory/spools',{method:'POST',body:JSON.stringify({name:$('newSpoolName').value,material:$('newSpoolMaterial').value,brand:$('newSpoolBrand').value,color:$('newSpoolColor').value,initial_g:+$('newSpoolInitial').value,remaining_g:+$('newSpoolRemaining').value})});['newSpoolName','newSpoolMaterial','newSpoolBrand','newSpoolColor'].forEach(id=>$(id).value='');msg('Bobine ajoutée au catalogue. Choisis maintenant sa voie AMS.');refresh()}catch(e){msg(e.message,true)}}
async function saveCatalogSpool(id,event){event?.stopPropagation();let slot=$('catalogSlot'+id).value;try{await api('/api/inventory/spools/'+id,{method:'POST',body:JSON.stringify({name:$('cn'+id).value,material:$('cm'+id).value,brand:$('cb'+id).value,color:$('cc'+id).value,initial_g:+$('ci'+id).value,remaining_g:+$('cr'+id).value})});let placement=await api('/api/inventory/assign',{method:'POST',body:JSON.stringify({spool_id:id,slot})});formDirty=false;msg(placement.message||'Bobine enregistrée.');refresh()}catch(e){msg(e.message,true)}}
async function deleteSpool(id,event){event?.stopPropagation();if(!confirm('Supprimer cette bobine du catalogue ? Son historique reste conservé.'))return;try{let result=await api('/api/inventory/spools/'+id+'/archive',{method:'POST',body:'{}'});if(selectedSpoolId===id){selectedSpoolId=null;$('timelineTitle').textContent='Historique de la bobine';$('timelineSummary').textContent='Clique une ligne du catalogue pour afficher sa frise chronologique.';$('timeline').className='timeline-empty';$('timeline').textContent='Aucune bobine sélectionnée.'}formDirty=false;msg(result.message||'Bobine supprimée.');refresh()}catch(e){msg(e.message,true)}}
async function shutdownCompanion(){if(!confirm('Arrêter AMS Lite Companion ? Bambu Studio restera ouvert.'))return;try{await api('/api/shutdown',{method:'POST',body:'{}'});clearInterval(refreshTimer);document.body.innerHTML='<div class="wrap"><div class="card"><h1>Companion arrêté</h1><p>Les niveaux et l’historique sont enregistrés. Tu peux fermer cet onglet.</p></div></div>'}catch(e){msg(e.message,true)}}
async function confirmDetectedImport(){try{await api('/api/bridge/confirm',{method:'POST',body:'{}'});msg('Travail détecté confirmé. Lance l’impression dans Bambu Studio.');refresh()}catch(e){msg(e.message,true)}}
async function importFile(){let f=$('file').files[0];if(!f)return msg('Choisis un fichier .gcode.3mf.',true);try{imported=await api('/api/import?filename='+encodeURIComponent(f.name),{method:'POST',body:await f.arrayBuffer()});renderMappings();msg('Consommation extraite du fichier.')}catch(e){msg(e.message,true)}}
function renderMappings(){let plates=imported.plates;$('mapping').innerHTML=`<label>Plateau imprimé</label><select id="plate" onchange="renderMappings()">${plates.map(p=>`<option value="${p.id}" ${$('plate')&&$('plate').value==p.id?'selected':''}>Plateau ${p.id}</option>`).join('')}</select><div id="lines"></div><button onclick="arm()">Armer ce travail</button>`;let p=plates.find(x=>String(x.id)==$('plate').value)||plates[0];$('lines').innerHTML=p.filaments.map(f=>`<div class="line"><div><label>Filament ${esc(f.id)} ${esc(f.type)}</label><div>${f.used_g} g</div></div><div><label>Emplacement</label><select data-fid="${esc(f.id)}">${[1,2,3,4].map(i=>`<option value="${i}">A${i}</option>`).join('')}</select></div></div>`).join('')}
async function arm(){let mappings=[...$('lines').querySelectorAll('select')].map(x=>({filament_id:x.dataset.fid,slot:x.value}));try{await api('/api/arm',{method:'POST',body:JSON.stringify({plate:$('plate').value,mappings})});msg('Travail armé. Lance maintenant l’impression avec Bambu Studio officiel.');refresh()}catch(e){msg(e.message,true)}}refresh();
function markDirty(e){if(e.target.matches('#ip,#serial,#code,#spools input,#autoEnabled,#fallbackEnabled,#bridgeMap select,#catalog input,#catalog select'))formDirty=true}document.addEventListener('input',markDirty);document.addEventListener('change',markDirty);
</script></body></html>'''


def render_html(api_token: str) -> str:
    return HTML.replace("__API_TOKEN__", api_token)


def run_server(open_browser: bool = True, state_path: Path = STATE_FILE, api_token: str | None = None) -> None:
    app = Companion(state_path)
    app.mqtt.start()
    app.bridge.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.app = app  # type: ignore[attr-defined]
    server.api_token = api_token or secrets.token_urlsafe(32)  # type: ignore[attr-defined]
    log(f"Interface disponible sur http://{HOST}:{PORT}")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.bridge.stop()
        app.mqtt.stop()
        app.bridge.join(timeout=2)
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Compteur local AMS Lite")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--api-token", help="jeton d’API locale fourni par l’application macOS")
    parser.add_argument("--parse", metavar="FICHIER", help="analyse un .gcode.3mf puis quitte")
    args = parser.parse_args()
    if args.parse:
        path = Path(args.parse)
        print(json.dumps(parse_3mf(path.read_bytes(), path.name), ensure_ascii=False, indent=2))
        return
    run_server(not args.no_browser, api_token=args.api_token)


if __name__ == "__main__":
    main()

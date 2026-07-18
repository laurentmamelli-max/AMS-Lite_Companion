#!/usr/bin/env python3
"""AMS Lite Companion - local filament usage tracker for Bambu printers.

Uses only the Python standard library.  It reads per-filament ``used_g`` from
a sliced Bambu/Orca .gcode.3mf and observes RUNNING -> FINISH over the
printer's local MQTT endpoint.  It never sends print commands.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import queue
import re
import signal
import socket
import ssl
import struct
import sys
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
HOST, PORT = "127.0.0.1", 8765
__version__ = "1.1.0"
TERMINAL_OK = {"FINISH", "FINISHED", "COMPLETED", "COMPLETE"}
RUNNING = {"RUNNING", "PRINTING", "PREPARE", "PREPARING", "SLICING"}
TERMINAL_BAD = {"FAILED", "CANCEL", "CANCELLED", "CANCELED"}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def log(message: str) -> None:
    line = f"{now_iso()} {message}\n"
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as out:
            out.write(line)
    except OSError:
        # Tests and read-only recovery environments may not expose a writable
        # macOS home directory. Runtime state still uses its explicit path.
        pass
    print(line, end="", flush=True)


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "config": {"ip": "", "serial": "", "access_code": ""},
        "spools": {
            str(i): {"name": f"Bobine A{i}", "initial_g": 1000.0, "remaining_g": 1000.0}
            for i in range(1, 5)
        },
        "armed_job": None,
        "active_job": None,
        "accounted": [],
        "history": [],
        "printer": {"connected": False, "state": "INCONNU", "progress": 0, "job": ""},
    }


def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    state = default_state()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            for key in state:
                if key in loaded:
                    state[key] = loaded[key]
        except Exception as exc:
            log(f"État illisible, valeurs par défaut utilisées: {exc}")
    return state


def atomic_save(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


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


def parse_3mf(raw: bytes, filename: str = "travail.3mf") -> dict[str, Any]:
    digest = hashlib.sha256(raw).hexdigest()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        slice_names = [n for n in names if n.lower().endswith("metadata/slice_info.config")]
        plates: list[dict[str, Any]] = []
        if slice_names:
            plates = parse_slice_info(archive.read(slice_names[0]))
        if not plates:
            for name in sorted(n for n in names if re.search(r"metadata/plate_\d+\.gcode$", n, re.I)):
                text = archive.read(name).decode("utf-8", "replace")[:250000]
                filaments = parse_gcode_weights(text)
                if filaments:
                    number = re.search(r"plate_(\d+)", name, re.I).group(1)
                    plates.append({"id": number, "filaments": filaments})
    if not plates:
        raise ValueError("Aucune consommation used_g trouvée. Exportez d’abord le plateau tranché en .gcode.3mf.")
    return {"filename": Path(filename).name, "sha256": digest, "plates": plates}


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
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((cfg.ip, 8883), timeout=10)
        sock = context.wrap_socket(raw, server_hostname=cfg.ip)
        sock.settimeout(5)
        client_id = f"ams-companion-{os.getpid()}-{int(time.time())}"
        payload = mqtt_string(client_id) + mqtt_string("bblp") + mqtt_string(cfg.access_code)
        variable = mqtt_string("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 30)
        sock.sendall(bytes([0x10]) + encode_varint(len(variable) + len(payload)) + variable + payload)
        header = recv_exact(sock, 1)
        body = recv_exact(sock, read_varint(sock))
        if header[0] >> 4 != 2 or len(body) < 2 or body[1] != 0:
            raise ConnectionError(f"Authentification MQTT refusée ({body.hex()})")
        topic = f"device/{cfg.serial}/report"
        sub = struct.pack("!H", 1) + mqtt_string(topic) + b"\x00"
        sock.sendall(bytes([0x82]) + encode_varint(len(sub)) + sub)
        req_topic = f"device/{cfg.serial}/request"
        request = json.dumps({"pushing": {"sequence_id": "1", "command": "pushall"}}, separators=(",", ":")).encode()
        publish = mqtt_string(req_topic) + request
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
                        self.app.on_message(json.loads(packet[offset:].decode("utf-8")))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        pass
            except socket.timeout:
                pass
            if time.monotonic() - last_ping > 20:
                sock.sendall(b"\xC0\x00")
                last_ping = time.monotonic()
        self.restart_event.clear()
        sock.close()


class Companion:
    def __init__(self, state_path: Path = STATE_FILE) -> None:
        self.state_path = state_path
        self.lock = threading.RLock()
        self.state = load_state(state_path)
        self.last_import: dict[str, Any] | None = None
        self.mqtt = LocalMQTT(self)

    def save(self) -> None:
        atomic_save(self.state, self.state_path)

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            clean = json.loads(json.dumps(self.state))
            clean["config"]["access_code"] = "" if not self.state["config"].get("access_code") else "********"
            clean["imported"] = self.last_import
            return clean

    def mqtt_config(self) -> MQTTConfig:
        with self.lock:
            c = self.state["config"]
            return MQTTConfig(c.get("ip", ""), c.get("serial", ""), c.get("access_code", ""))

    def set_connected(self, connected: bool) -> None:
        with self.lock:
            self.state["printer"]["connected"] = connected

    def configure(self, data: dict[str, Any]) -> None:
        with self.lock:
            current = self.state["config"]
            current["ip"] = str(data.get("ip", current.get("ip", ""))).strip()
            current["serial"] = str(data.get("serial", current.get("serial", ""))).strip()
            code = str(data.get("access_code", "")).strip()
            if code and code != "********":
                current["access_code"] = code
            self.save()
        self.mqtt.restart()

    def update_spools(self, data: dict[str, Any]) -> None:
        with self.lock:
            for slot in map(str, range(1, 5)):
                incoming = data.get(slot, {})
                spool = self.state["spools"][slot]
                spool["name"] = str(incoming.get("name", spool["name"]))[:80]
                spool["initial_g"] = max(0.0, _float(incoming.get("initial_g", spool["initial_g"])))
                spool["remaining_g"] = max(0.0, _float(incoming.get("remaining_g", spool["remaining_g"])))
            self.save()

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
            printer = self.state["printer"]
            raw_state = report.get("gcode_state") or report.get("print_status") or printer.get("state", "INCONNU")
            state = str(raw_state).upper()
            printer["state"] = state
            printer["progress"] = int(_float(report.get("mc_percent", printer.get("progress", 0))))
            printer["job"] = str(report.get("subtask_name") or report.get("gcode_file") or printer.get("job", ""))
            task_id = str(report.get("subtask_id") or report.get("task_id") or "")
            if state in RUNNING and self.state.get("armed_job") and not self.state.get("active_job"):
                active = json.loads(json.dumps(self.state["armed_job"]))
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
                log(f"Travail {state}: aucune déduction")
                self.save()
            elif state in TERMINAL_OK and active.get("saw_running"):
                key = f"{self.state['config'].get('serial','')}:{active.get('task_id') or active['token']}"
                if key not in self.state["accounted"]:
                    deductions = []
                    for line in active["lines"]:
                        spool = self.state["spools"][line["slot"]]
                        before = _float(spool["remaining_g"])
                        after = max(0.0, before - _float(line["used_g"]))
                        spool["remaining_g"] = round(after, 3)
                        deductions.append({"slot": line["slot"], "used_g": line["used_g"], "before_g": before, "after_g": after})
                    self.state["accounted"].append(key)
                    self.state["accounted"] = self.state["accounted"][-1000:]
                    self.state["history"].insert(0, {**active, "result": state, "ended_at": now_iso(), "deducted": True, "deductions": deductions})
                    log(f"Travail terminé et débité: {key}")
                self.state["history"] = self.state["history"][:100]
                self.state["active_job"] = None
                self.save()


class Handler(BaseHTTPRequestHandler):
    server_version = "AMSLiteCompanion/1.0"

    @property
    def app(self) -> Companion:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_json(self, value: Any, status: int = 200) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 250 * 1024 * 1024:
            raise ValueError("Fichier trop volumineux (250 Mo maximum)")
        return self.rfile.read(length)

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            raw = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/api/state":
            self.send_json(self.app.public_state())
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        try:
            if self.path == "/api/config":
                self.app.configure(json.loads(self.body()))
                self.send_json({"ok": True})
            elif self.path == "/api/spools":
                self.app.update_spools(json.loads(self.body()))
                self.send_json({"ok": True})
            elif self.path.startswith("/api/import"):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                filename = query.get("filename", ["travail.3mf"])[0]
                self.send_json(self.app.import_3mf(self.body(), filename))
            elif self.path == "/api/arm":
                self.send_json(self.app.arm(json.loads(self.body())))
            elif self.path == "/api/shutdown":
                self.send_json({"ok": True, "message": "Companion arrêté proprement"})
                log("Arrêt demandé depuis le tableau de bord")
                # shutdown() must run outside the request-handling thread.
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_error(404)
        except Exception as exc:
            log(f"Erreur API {self.path}: {exc}")
            self.send_json({"error": str(exc)}, 400)


HTML = r'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AMS Lite Companion</title><style>
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#20242a;background:#f4f5f6}body{margin:0}.wrap{max-width:1050px;margin:auto;padding:24px}h1{margin:0 0 4px}.sub{color:#69717b;margin-bottom:20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}.card{background:white;border:1px solid #dfe3e7;border-radius:14px;padding:18px;box-shadow:0 2px 10px #0000000b}.wide{grid-column:1/-1}h2{font-size:17px;margin:0 0 14px}label{display:block;font-size:12px;color:#656d76;margin:9px 0 4px}input,select,button{box-sizing:border-box;border:1px solid #cbd1d7;border-radius:8px;padding:9px;font:inherit}input,select{width:100%}button{background:#00ae42;color:white;border:0;font-weight:600;cursor:pointer;margin-top:12px}button.secondary{background:#59636e}.status{display:inline-flex;gap:7px;align-items:center;font-weight:600}.dot{width:10px;height:10px;border-radius:50%;background:#d33}.on .dot{background:#00ae42}.spools{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.spool{padding:12px;border:1px solid #e1e4e7;border-radius:10px}.spool b{color:#00a23d}.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.notice{padding:10px;border-radius:8px;background:#eef8f1;margin:10px 0}.error{background:#ffecec;color:#a11}.line{display:grid;grid-template-columns:1fr 100px 90px;gap:8px;align-items:end}.history{font-size:13px;border-top:1px solid #eee;padding:8px 0}@media(max-width:700px){.spools{grid-template-columns:1fr 1fr}.line{grid-template-columns:1fr}.wrap{padding:12px}}</style></head><body><div class="wrap">
<h1>AMS Lite Companion</h1><div class="sub">Compteur local v1.1.0 — Bambu Studio officiel reste utilisé pour imprimer.</div><div id="msg"></div>
<div class="grid"><section class="card"><h2>Imprimante locale</h2><div id="conn" class="status"><span class="dot"></span><span>Déconnectée</span></div><div id="pstate"></div>
<label>Adresse IP</label><input id="ip" placeholder="192.168.1.50"><label>Numéro de série</label><input id="serial" placeholder="01S00A..."><label>Code d’accès LAN</label><input id="code" type="password" placeholder="8 chiffres"><button onclick="saveConfig()">Enregistrer et connecter</button></section>
<section class="card"><h2>Prochaine impression</h2><label>Fichier tranché .gcode.3mf</label><input id="file" type="file" accept=".3mf"><div id="imported"></div><button onclick="importFile()">Analyser le fichier</button><div id="mapping"></div></section>
<section class="card wide"><h2>Bobines AMS Lite</h2><div class="spools" id="spools"></div><button onclick="saveSpools()">Enregistrer les poids</button></section>
<section class="card wide"><h2>Historique</h2><div id="history">Aucun travail comptabilisé.</div></section>
<section class="card wide"><h2>Companion</h2><p>Utilise ce bouton après l’impression pour enregistrer et arrêter complètement Companion.</p><button class="secondary" onclick="shutdownCompanion()">Arrêter Companion</button></section></div></div>
<script>
let S=null, imported=null, formDirty=false;const $=id=>document.getElementById(id);function msg(t,e=false){$('msg').innerHTML=t?`<div class="notice ${e?'error':''}">${t}</div>`:''}
async function api(path,opt={}){let r=await fetch(path,opt),j=await r.json();if(!r.ok)throw Error(j.error||'Erreur');return j}
function render(s){S=s;$('conn').className='status '+(s.printer.connected?'on':'');$('conn').lastElementChild.textContent=s.printer.connected?'Connectée':'Déconnectée';$('pstate').textContent=`${s.printer.state||''} ${s.printer.progress||0}% ${s.printer.job||''}`;
if(!formDirty){$('ip').value=s.config.ip||'';$('serial').value=s.config.serial||'';$('code').placeholder=s.config.access_code?'Code enregistré':'8 chiffres';
$('spools').innerHTML=[1,2,3,4].map(i=>{let x=s.spools[i];return `<div class="spool"><b>A${i}</b><label>Nom</label><input id="n${i}" value="${esc(x.name)}"><div class="row"><div><label>Initial (g)</label><input id="i${i}" type="number" step="0.1" value="${x.initial_g}"></div><div><label>Restant (g)</label><input id="r${i}" type="number" step="0.1" value="${x.remaining_g}"></div></div></div>`}).join('');}
let active=s.active_job?`En cours : ${esc(s.active_job.file)} — plateau ${s.active_job.plate}`:s.armed_job?`Armé : ${esc(s.armed_job.file)} — en attente de RUNNING`:'Aucun travail armé';$('imported').innerHTML=`<div class="notice">${active}</div>`;
$('history').innerHTML=s.history.length?s.history.map(h=>`<div class="history"><b>${esc(h.file||'Travail')}</b> — ${esc(h.result)} — ${h.deducted?'déduction effectuée':'aucune déduction'}<br>${esc(h.ended_at||'')}</div>`).join(''):'Aucun travail comptabilisé.'}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function refresh(){try{render(await api('/api/state'))}catch(e){msg(e.message,true)}}const refreshTimer=setInterval(refresh,3000);
async function saveConfig(){try{await api('/api/config',{method:'POST',body:JSON.stringify({ip:$('ip').value,serial:$('serial').value,access_code:$('code').value})});formDirty=false;msg('Configuration enregistrée.');refresh()}catch(e){msg(e.message,true)}}
async function saveSpools(){let x={};for(let i=1;i<=4;i++)x[i]={name:$('n'+i).value,initial_g:+$('i'+i).value,remaining_g:+$('r'+i).value};try{await api('/api/spools',{method:'POST',body:JSON.stringify(x)});formDirty=false;msg('Poids enregistrés.');refresh()}catch(e){msg(e.message,true)}}
async function shutdownCompanion(){if(!confirm('Arrêter AMS Lite Companion ? Bambu Studio restera ouvert.'))return;try{await api('/api/shutdown',{method:'POST',body:'{}'});clearInterval(refreshTimer);document.body.innerHTML='<div class="wrap"><div class="card"><h1>Companion arrêté</h1><p>Les niveaux et l’historique sont enregistrés. Tu peux fermer cet onglet.</p></div></div>'}catch(e){msg(e.message,true)}}
async function importFile(){let f=$('file').files[0];if(!f)return msg('Choisis un fichier .gcode.3mf.',true);try{imported=await api('/api/import?filename='+encodeURIComponent(f.name),{method:'POST',body:await f.arrayBuffer()});renderMappings();msg('Consommation extraite du fichier.')}catch(e){msg(e.message,true)}}
function renderMappings(){let plates=imported.plates;$('mapping').innerHTML=`<label>Plateau imprimé</label><select id="plate" onchange="renderMappings()">${plates.map(p=>`<option value="${p.id}" ${$('plate')&&$('plate').value==p.id?'selected':''}>Plateau ${p.id}</option>`).join('')}</select><div id="lines"></div><button onclick="arm()">Armer ce travail</button>`;let p=plates.find(x=>String(x.id)==$('plate').value)||plates[0];$('lines').innerHTML=p.filaments.map(f=>`<div class="line"><div><label>Filament ${esc(f.id)} ${esc(f.type)}</label><div>${f.used_g} g</div></div><div><label>Emplacement</label><select data-fid="${esc(f.id)}">${[1,2,3,4].map(i=>`<option value="${i}">A${i}</option>`).join('')}</select></div></div>`).join('')}
async function arm(){let mappings=[...$('lines').querySelectorAll('select')].map(x=>({filament_id:x.dataset.fid,slot:x.value}));try{await api('/api/arm',{method:'POST',body:JSON.stringify({plate:$('plate').value,mappings})});msg('Travail armé. Lance maintenant l’impression avec Bambu Studio officiel.');refresh()}catch(e){msg(e.message,true)}}refresh();
document.addEventListener('input',e=>{if(e.target.matches('#ip,#serial,#code,#spools input'))formDirty=true});
</script></body></html>'''


def run_server(open_browser: bool = True, state_path: Path = STATE_FILE) -> None:
    app = Companion(state_path)
    app.mqtt.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.app = app  # type: ignore[attr-defined]
    log(f"Interface disponible sur http://{HOST}:{PORT}")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.mqtt.stop()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Compteur local AMS Lite")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--parse", metavar="FICHIER", help="analyse un .gcode.3mf puis quitte")
    args = parser.parse_args()
    if args.parse:
        path = Path(args.parse)
        print(json.dumps(parse_3mf(path.read_bytes(), path.name), ensure_ascii=False, indent=2))
        return
    run_server(not args.no_browser)


if __name__ == "__main__":
    main()

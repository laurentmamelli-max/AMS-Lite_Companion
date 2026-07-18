import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path

import ams_companion as ac


def sample_3mf(*weights):
    filaments = "".join(
        f'<filament id="{i+1}" type="PLA" color="#ffffff" used_g="{w}" />'
        for i, w in enumerate(weights)
    )
    xml = f'<config><plate id="1">{filaments}</plate></config>'.encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Metadata/slice_info.config", xml)
    return out.getvalue()


class CompanionTests(unittest.TestCase):
    def test_parse_per_filament(self):
        parsed = ac.parse_3mf(sample_3mf(18.2, 3.5), "test.gcode.3mf")
        self.assertEqual([18.2, 3.5], [x["used_g"] for x in parsed["plates"][0]["filaments"]])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.gcode.3mf"
            path.write_bytes(sample_3mf(18.2, 3.5))
            streamed = ac.parse_3mf_path(path)
            self.assertEqual(parsed["plates"], streamed["plates"])
            self.assertEqual(parsed["sha256"], streamed["sha256"])

    def test_finish_deducts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.last_import = ac.parse_3mf(sample_3mf(43), "job.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "1"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "42"}})
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "42"}})
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "42"}})
            self.assertEqual(957, app.state["spools"]["1"]["remaining_g"])
            self.assertEqual(1, len(app.state["accounted"]))

    def test_cancel_does_not_deduct(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.last_import = ac.parse_3mf(sample_3mf(12), "job.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "2"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "43"}})
            app.on_message({"print": {"gcode_state": "FAILED", "subtask_id": "43"}})
            self.assertEqual(1000, app.state["spools"]["2"]["remaining_g"])

    def test_never_below_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.state["spools"]["3"]["remaining_g"] = 2
            app.last_import = ac.parse_3mf(sample_3mf(9), "job.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "3"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "44"}})
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "44"}})
            self.assertEqual(0, app.state["spools"]["3"]["remaining_g"])

    def test_multifilament_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app = ac.Companion(path)
            app.last_import = ac.parse_3mf(sample_3mf(10.5, 4.25), "multi.gcode.3mf")
            app.arm({"plate": "1", "mappings": [
                {"filament_id": "1", "slot": "1"},
                {"filament_id": "2", "slot": "4"},
            ]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "45"}})
            # Simulate Companion being restarted while the printer is running.
            restarted = ac.Companion(path)
            restarted.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "45"}})
            self.assertEqual(989.5, restarted.state["spools"]["1"]["remaining_g"])
            self.assertEqual(995.75, restarted.state["spools"]["4"]["remaining_g"])
            # A repeated terminal frame after another restart remains idempotent.
            again = ac.Companion(path)
            again.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "45"}})
            self.assertEqual(989.5, again.state["spools"]["1"]["remaining_g"])

    def test_bridge_recovers_studio_archive_and_uses_saved_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bamboo_model"
            metadata = root / "job#123" / "Metadata"
            metadata.mkdir(parents=True)
            app = ac.Companion(Path(tmp) / "state.json", [root])
            app.bridge.stable_seconds = 0
            archive = metadata / ".123.0.3mf"
            archive.write_bytes(sample_3mf(10.5, 4.25))
            app.bridge.scan_once()
            app.bridge.scan_once()
            self.assertIsNotNone(app.auto_import)
            self.assertIsNone(app.state["armed_job"])
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "auto-1"}})
            self.assertEqual(["1", "2"], [line["slot"] for line in app.state["active_job"]["lines"]])
            self.assertEqual("Correspondance enregistrée", app.state["active_job"]["mapping_source"])
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "auto-1"}})
            self.assertEqual(989.5, app.state["spools"]["1"]["remaining_g"])
            self.assertEqual(995.75, app.state["spools"]["2"]["remaining_g"])

    def test_bridge_uses_ams_mapping_from_studio_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json", [Path(tmp) / "watch"])
            parsed = ac.parse_3mf(sample_3mf(12, 3), "automatic.gcode.3mf")
            app.on_studio_archive(Path(tmp) / "automatic.3mf", parsed)
            app.on_mqtt_message("device/SERIAL/request", {"print": {
                "command": "project_file",
                "ams_mapping": [2, 0],
                "param": "Metadata/plate_1.gcode",
                "subtask_name": "Bicolore",
            }})
            armed = app.state["armed_job"]
            self.assertEqual(["3", "1"], [line["slot"] for line in armed["lines"]])
            self.assertEqual("Commande Bambu Studio", armed["mapping_source"])
            self.assertEqual("Bicolore", armed["file"])
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "mapped-1"}})
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "mapped-1"}})
            self.assertEqual(988, app.state["spools"]["3"]["remaining_g"])
            self.assertEqual(997, app.state["spools"]["1"]["remaining_g"])

    def test_bridge_request_can_arrive_before_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json", [Path(tmp) / "watch"])
            app.on_mqtt_message("device/SERIAL/request", {"print": {
                "ams_mapping": "[3,1]", "param": "Metadata/plate_1.gcode"
            }})
            parsed = ac.parse_3mf(sample_3mf(8, 2), "later.gcode.3mf")
            app.on_studio_archive(Path(tmp) / "later.3mf", parsed)
            self.assertEqual(["4", "2"], [line["slot"] for line in app.state["armed_job"]["lines"]])

    def test_bridge_does_not_replace_manual_job_or_choose_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bamboo_model"
            root.mkdir()
            app = ac.Companion(Path(tmp) / "state.json", [root])
            app.bridge.stable_seconds = 0
            app.last_import = ac.parse_3mf(sample_3mf(7), "manual.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "4"}]})
            old = root / "old.3mf"
            old.write_bytes(sample_3mf(99))
            old_time = app.bridge.started_at - 60
            os.utime(old, (old_time, old_time))
            newest = root / "new.3mf"
            newest.write_bytes(sample_3mf(2))
            app.bridge.scan_once()
            app.bridge.scan_once()
            self.assertEqual("manual.gcode.3mf", app.state["armed_job"]["file"])
            self.assertEqual("new.3mf", app.auto_import["filename"])
            self.assertEqual("Fichier détecté, travail manuel conservé", app.state["bridge"]["status"])

    def test_bridge_waits_for_complete_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bamboo_model"
            root.mkdir()
            app = ac.Companion(Path(tmp) / "state.json", [root])
            app.bridge.stable_seconds = 0
            archive = root / "writing.3mf"
            archive.write_bytes(b"not complete")
            app.bridge.scan_once()
            app.bridge.scan_once()
            self.assertIsNone(app.auto_import)
            time.sleep(0.002)
            archive.write_bytes(sample_3mf(6))
            app.bridge.scan_once()
            app.bridge.scan_once()
            self.assertEqual("writing.3mf", app.auto_import["filename"])

    def test_bridge_never_falls_back_to_an_older_recent_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bamboo_model"
            root.mkdir()
            app = ac.Companion(Path(tmp) / "state.json", [root])
            app.bridge.stable_seconds = 0
            older = root / "older.3mf"
            older.write_bytes(sample_3mf(90))
            time.sleep(0.002)
            newest = root / "newest.3mf"
            newest.write_bytes(sample_3mf(5))
            app.bridge.scan_once()
            app.bridge.scan_once()
            app.bridge.scan_once()
            self.assertEqual("newest.3mf", app.auto_import["filename"])
            self.assertEqual(5, app.auto_import["plates"][0]["filaments"][0]["used_g"])

    def test_http_interface_and_state_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            server = ac.ThreadingHTTPServer(("127.0.0.1", 0), ac.Handler)
            server.app = app
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                html = urllib.request.urlopen(base + "/", timeout=2).read().decode()
                state = json.loads(urllib.request.urlopen(base + "/api/state", timeout=2).read())
                self.assertIn("AMS Lite Companion", html)
                self.assertIn("Arrêter Companion", html)
                self.assertIn("Passerelle Bambu Studio", html)
                self.assertEqual(1000, state["spools"]["1"]["remaining_g"])
                bridge_request = urllib.request.Request(
                    base + "/api/bridge",
                    data=json.dumps({"enabled": True, "fallback_enabled": True,
                                     "default_mapping": {"1": "3"}}).encode(),
                    method="POST",
                )
                bridge_result = json.loads(urllib.request.urlopen(bridge_request, timeout=2).read())
                self.assertTrue(bridge_result["ok"])
                self.assertEqual("3", app.state["bridge"]["default_mapping"]["1"])
                request = urllib.request.Request(base + "/api/shutdown", data=b"{}", method="POST")
                result = json.loads(urllib.request.urlopen(request, timeout=2).read())
                self.assertTrue(result["ok"])
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()

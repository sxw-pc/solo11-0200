import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.shift_support import FIXTURES, OPEN, ROOT


class CliTests(unittest.TestCase):
    def test_public_cli_demo_init_ingest_queries_and_rebuild(self):
        with tempfile.TemporaryDirectory(prefix="caveflow-test-", dir=ROOT) as folder:
            db = Path(folder) / "shift.sqlite"

            def call(*args, code=0):
                result = subprocess.run([sys.executable, "-B", "-m", "caveflow", "--db", str(db), *map(str, args)],
                                        cwd=ROOT, text=True, encoding="utf-8", capture_output=True, check=False)
                self.assertEqual(result.returncode, code, result.stderr)
                return json.loads(result.stdout) if code == 0 else result.stderr

            self.assertEqual(call("init", FIXTURES / "shift.json")["decision"], "initialized")
            self.assertEqual(call("init", FIXTURES / "shift.json")["decision"], "already_initialized")
            self.assertEqual(call("load", FIXTURES / "01-opening.json")[0]["decision"], "anchor_recorded")
            self.assertIn("bad-packet-invalid-tail", call("load", FIXTURES / "bad-packet.json", code=2))
            self.assertEqual(call("roster", "sheet-a", "--at", 6)["unmarked"], ["a2"])
            self.assertEqual(call("zone", "echo", "--at", 6)["capacity"], 2)
            packet = Path(folder) / "single.json"
            packet.write_text(json.dumps(OPEN[0]), encoding="utf-8")
            self.assertTrue(call("record", packet)["duplicate"])
            self.assertEqual(call("demo")["seal"], "seal_disputed")
            self.assertEqual(call("gate", "--at", 20)["through_step"], 3)
            self.assertEqual(call("receipt", "dispatch-amber-early")["reason"], "roster_not_ready")
            baseline = call("status", "--at", 26)
            self.assertEqual(call("rebuild")["events"], 51)
            self.assertEqual(call("status", "--at", 26), baseline)
            self.assertEqual(len(call("log")), 51)
            packet.write_text('{"id":"one","id":"two"}', encoding="utf-8")
            self.assertIn("duplicate JSON object key", call("record", packet, code=2))

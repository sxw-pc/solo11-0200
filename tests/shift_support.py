import unittest
from pathlib import Path

from caveflow import journal
from caveflow.cli import load
from caveflow.store import connect, initialize, transaction

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
OPEN, DISPATCH, HANDOFF, CLOSE, LATE = [load(path) for path in sorted(FIXTURES.glob("[0-9][0-9]-*.json"))]
CFG = load(FIXTURES / "shift.json")


def event(identifier, kind, at, **payload):
    return {"id": identifier, "shift": CFG["shift"], "kind": kind, "at": at, "data": payload}


class ShiftCase(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)
        initialize(self.db, CFG)

    def append(self, *events):
        return journal.record_many(self.db, list(events))

    def report(self, at=26):
        with transaction(self.db):
            return journal.status(self.db, CFG, at)

    def entered(self):
        self.append(*OPEN, *DISPATCH)

    def closed(self):
        self.append(*OPEN, *DISPATCH, *HANDOFF, *CLOSE)

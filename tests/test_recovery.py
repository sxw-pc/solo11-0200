from caveflow import journal, roster
from caveflow.cli import load
from caveflow.model import DomainError, encode
from caveflow.store import one
from tests.shift_support import CLOSE, DISPATCH, FIXTURES, LATE, OPEN, event, ShiftCase


class RecoveryTests(ShiftCase):
    def test_rollback_batch_leaves_no_valid_head_or_receipt(self):
        self.append(*OPEN)
        before = journal.receipts(self.db)
        with self.assertRaisesRegex(DomainError, "bad-packet-invalid-tail"):
            self.append(*load(FIXTURES / "bad-packet.json"))
        self.assertEqual(before, journal.receipts(self.db))
        self.assertEqual(roster.presence(self.db, "sheet-a", 6)["unmarked"], ["a2"])
        self.assertEqual(self.append(DISPATCH[1])[0]["decision"], "mark_recorded")

    def test_retry_preserves_original_hold_and_collision_is_atomic(self):
        self.append(*OPEN, *DISPATCH[:3])
        old = self.append(DISPATCH[0])[0]
        self.assertEqual((old["duplicate"], old["reason"]), (True, "roster_not_ready"))
        changed = {**DISPATCH[0], "at": 9}
        with self.assertRaisesRegex(DomainError, "event identifier collision"):
            self.append(*LATE, changed)
        self.assertEqual(self.report(9)["active_groups"][0]["roster"]["added"], [])
        self.assertEqual(len(journal.receipts(self.db)), len(OPEN) + 3)

    def test_older_operations_rejected_while_late_evidence_is_accepted(self):
        self.entered()
        with self.assertRaisesRegex(DomainError, "advance in effective time"):
            self.append(event("past-op", "close.seal", 8, desk="desk-day"))
        self.assertIsNone(one(self.db, "SELECT id FROM events WHERE id = 'past-op'"))
        self.assertEqual(self.append(*LATE)[-1]["decision"], "mark_revised")
        with self.assertRaisesRegex(DomainError, "precedes latest operation"):
            self.report(8)

    def test_rebuild_restores_receipts_and_material_state_from_raw_events(self):
        self.closed()
        self.append(*LATE)
        before, receipts = self.report(), journal.receipts(self.db)
        self.db.execute("DELETE FROM receipts WHERE event_id = 'seal-final'")
        self.db.execute("UPDATE groups SET custodian = 'g-spare' WHERE id = 'amber'")
        with self.assertRaisesRegex(DomainError, "rebuild before retry"):
            self.append(CLOSE[-1])
        summary = journal.rebuild(self.db)
        self.assertEqual(summary["events"], 51)
        self.assertEqual(self.report(), before)
        self.assertEqual(journal.receipts(self.db), receipts)
        self.assertEqual(one(self.db, "SELECT custodian FROM groups WHERE id = 'amber'")["custodian"], "g-relay")

    def test_rebuild_failure_restores_existing_projection_transaction(self):
        self.closed()
        before, receipts = self.report(), journal.receipts(self.db)
        bad = event("broken-log", "escort.exit", 1, group="amber", guide="g-relay")
        self.db.execute("INSERT INTO events(id, body) VALUES (?, ?)", (bad["id"], encode(bad)))
        with self.assertRaisesRegex(DomainError, "advance in effective time"):
            journal.rebuild(self.db)
        self.assertEqual(self.report(), before)
        self.assertEqual(journal.receipts(self.db), receipts)

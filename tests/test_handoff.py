from caveflow import journal
from caveflow.store import connect
from tests.shift_support import CLOSE, DISPATCH, HANDOFF, LATE, OPEN, event, ShiftCase


class HandoffTests(ShiftCase):
    def test_dispatch_freezes_presence_and_rejects_duplicate_live_members(self):
        self.append(*OPEN)
        results = self.append(*DISPATCH[:3])
        self.assertEqual(results[0]["reason"], "roster_not_ready")
        self.assertEqual(results[2]["members"], ["a1", "a2", "g-amber"])
        self.append(event("overlap-sheet", "roster.sheet", 4, sheet="other", guide="g-spare", members=["a1"]),
                    event("overlap-mark", "roster.mark", 5, sheet="other", person="a1", version=1, mark="present"))
        collision = self.append(event("overlap-dispatch", "escort.dispatch", 9, group="other", sheet="other", guide="g-spare"))[0]
        self.assertEqual((collision["reason"], collision["people"]), ("already_under_custody", ["a1"]))
        self.append(*LATE)
        active = self.report(9)["active_groups"][0]
        self.assertEqual(active["members"], ["a1", "a2", "g-amber"])
        self.assertEqual((active["roster"]["added"], active["roster"]["removed"]), (["a3"], ["a2"]))

    def test_two_valid_landings_change_following_group_admission(self):
        self.append(*OPEN, *DISPATCH[:3], DISPATCH[5])
        for destination, expected in (("echo", "moved"), ("pearl", "held")):
            with self.subTest(destination=destination):
                # Fork the storage in memory at the same business checkpoint.
                db = connect(":memory:")
                try:
                    self.db.backup(db)
                    result = journal.record_many(db, [
                        event("blue-branch", "escort.move", 9, group="blue", guide="g-blue", destination=destination),
                        event("amber-follow", "escort.move", 9, group="amber", guide="g-amber", destination="pearl"),
                    ])
                    self.assertEqual(result[0]["decision"], "moved")
                    self.assertEqual(result[1]["decision"], expected)
                    self.assertEqual(result[1]["landing"]["occupied"], 0 if destination == "echo" else 2)
                finally:
                    db.close()

    def test_custody_handoff_freezes_move_and_requires_correct_signer(self):
        self.entered()
        offer, frozen, invalid, accepted = self.append(*HANDOFF[:4])
        self.assertEqual((offer["decision"], frozen["reason"], invalid["reason"], accepted["decision"]),
                         ("transfer_offered", "transfer_pending", "wrong_transfer_signer", "transfer_accepted"))
        old, current = self.append(*CLOSE[2:4])
        self.assertEqual((old["reason"], current["decision"]), ("wrong_custodian", "moved"))
        self.assertEqual(self.report(18)["active_groups"][0]["custodian"], "g-relay")

    def test_custody_accept_rechecks_receiver_exclusivity_then_withdraws(self):
        self.entered()
        self.append(HANDOFF[0])
        self.append(event("relay-sheet", "roster.sheet", 4, sheet="relay-sheet", guide="g-relay", members=["x"]),
                    event("relay-mark", "roster.mark", 5, sheet="relay-sheet", person="x", version=1, mark="present"),
                    event("relay-launch", "escort.dispatch", 10, group="relay", sheet="relay-sheet", guide="g-relay"))
        held = self.append(HANDOFF[3])[0]
        self.assertEqual(held["reason"], "receiver_busy")
        cancel = event("withdraw-amber", "escort.resolve", 12, offer="give-amber", guide="g-amber", response="withdraw")
        self.assertEqual(self.append(cancel)[0]["decision"], "transfer_withdrawn")
        self.assertEqual(self.report(12)["active_groups"][0]["custodian"], "g-amber")
        self.assertEqual(self.append(event("bad-exit", "escort.exit", 12, group="amber", guide="g-amber"))[0]["reason"],
                         "exit_requires_foyer")

    def test_accept_and_decline_give_different_valid_return_signers(self):
        self.entered()
        self.append(HANDOFF[0])
        for response, signer, other in (("accept", "g-relay", "g-amber"), ("decline", "g-amber", "g-relay")):
            with self.subTest(response=response):
                db = connect(":memory:")
                try:
                    self.db.backup(db)
                    resolution, denied, moved, exited = journal.record_many(db, [
                        event("decide-relay", "escort.resolve", 11, offer="give-amber", guide="g-relay", response=response),
                        event("wrong-return", "escort.move", 12, group="amber", guide=other, destination="foyer"),
                        event("return", "escort.move", 12, group="amber", guide=signer, destination="foyer"),
                        event("exit", "escort.exit", 13, group="amber", guide=signer),
                    ])
                    self.assertEqual((resolution["custodian"], denied["reason"], moved["decision"], exited["custodian"]),
                                     (signer, "wrong_custodian", "moved", signer))
                    self.assertEqual(exited["members"], ["a1", "a2", "g-amber"])
                finally:
                    db.close()

    def test_incomplete_lighting_invalidates_desk_handoff_with_live_group(self):
        self.entered()
        results = self.append(*HANDOFF)
        old = results[4]["manifest"]["groups"][0]
        held = results[7]
        now = held["current"]["groups"][0]
        self.assertEqual((old["condition"]["capacity"], now["condition"]["capacity"]), (4, 0))
        self.assertTrue(now["over_capacity"])
        self.assertEqual((held["reason"], held["changed"], held["owner"]), ("desk_manifest_changed", ["groups"], "desk-day"))
        self.assertEqual(results[-1]["decision"], "desk_accepted")
        self.assertEqual(self.report(14)["desk"], "desk-night")

    def test_late_gate_cut_invalidates_desk_accept_without_moving_groups(self):
        self.entered()
        offered = self.append(HANDOFF[4])[0]
        self.assertEqual(offered["manifest"]["gate"]["issue"]["reason"], "cutoff_not_sampled")
        self.append(event("meter-to-handoff", "gate.read", 11, step=2, version=1, entries=105, exits=80, mode="normal"))
        accept = self.append(event("desk-first-try", "desk.accept", 12, offer="desk-initial", desk="desk-night"))[0]
        self.assertEqual((accept["reason"], accept["changed"]), ("desk_manifest_changed", ["gate"]))
        self.assertEqual(accept["current"]["gate"]["decision"], "covered")
        self.assertEqual(accept["current"]["groups"], offered["manifest"]["groups"])

    def test_desk_rejects_signer_and_allows_owner_to_withdraw(self):
        self.entered()
        self.append(HANDOFF[4])
        wrong = event("wrong-desk", "desk.accept", 11, offer="desk-initial", desk="desk-day")
        self.assertEqual(self.append(wrong)[0]["reason"], "wrong_desk_offer_or_signer")
        held = self.append(event("still-open", "close.seal", 11, desk="desk-day"))[0]
        self.assertIn("desk_handoff_pending", held["evidence"]["reasons"])
        self.assertEqual(self.append(event("drop-desk", "desk.withdraw", 12, offer="desk-initial", desk="desk-day"))[0]["decision"],
                         "desk_withdrawn")
        self.assertIsNone(self.report(12)["pending_desk"])


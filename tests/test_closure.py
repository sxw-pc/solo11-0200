from tests.shift_support import CLOSE, DISPATCH, HANDOFF, LATE, OPEN, event, ShiftCase


class ClosureTests(ShiftCase):
    def test_gate_coverage_and_correct_desk_control_close(self):
        self.append(*OPEN, *DISPATCH, *HANDOFF)
        results = dict((row["event"], row) for row in self.append(*CLOSE))
        first, second, final = (results[key] for key in ("seal-first", "seal-unwitnessed", "seal-final"))
        self.assertEqual(first["evidence"]["gate"]["issue"]["reason"], "step_gap")
        self.assertEqual(second["evidence"]["gate"]["issue"]["reason"], "unwitnessed_reset")
        self.assertEqual(results["seal-old-desk"]["reason"], "wrong_desk_owner")
        self.assertEqual((final["decision"], final["evidence"]["escort_totals"]), ("sealed", {"entries": 5, "exits": 5}))
        blocked = self.append(event("dispatch-after", "escort.dispatch", 21, group="again", sheet="sheet-a", guide="g-amber"))[0]
        self.assertEqual(blocked["reason"], "shift_sealed")

    def test_inside_and_pending_transfer_block_even_if_meter_net_is_zero(self):
        self.entered()
        self.append(HANDOFF[0])
        self.append(event("net-zero", "gate.read", 10, step=2, version=1, entries=105, exits=85, mode="normal"))
        result = self.append(event("no-seal", "close.seal", 10, desk="desk-day"))[0]
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["evidence"]["reasons"], ["groups_inside", "custody_unsettled", "gate_escort_disagreement"])

    def test_equal_net_after_exit_does_not_reconcile_directional_totals(self):
        self.append(*OPEN, *DISPATCH, *HANDOFF, *CLOSE[:5])
        self.append(event("both-more", "gate.read", 20, step=2, version=1, entries=106, exits=86, mode="normal"))
        result = self.append(event("wrong-total", "close.seal", 20, desk="desk-night"))[0]
        self.assertEqual(result["evidence"]["reasons"], ["gate_escort_disagreement"])
        self.assertEqual(result["evidence"]["active"], [])

    def test_equal_count_identity_swap_disputes_but_does_not_erase_seal(self):
        self.closed()
        original = self.report()["closing"]
        self.assertEqual(original["status"], "seal_supported")
        self.append(*LATE)
        disputed = self.report()["closing"]
        self.assertEqual(disputed["status"], "seal_disputed")
        self.assertEqual(disputed["current"]["reasons"], ["roster_discrepancy"])
        self.assertEqual(disputed["current"]["escort_totals"], original["frozen"]["escort_totals"])
        change = disputed["current"]["roster_discrepancies"][0]
        self.assertEqual((change["added"], change["removed"]), (["a3"], ["a2"]))
        self.assertEqual(disputed["frozen"], original["frozen"])
        self.append(event("restore-a2", "roster.mark", 5, sheet="sheet-a", person="a2", version=3, mark="present"),
                    event("restore-a3", "roster.mark", 5, sheet="sheet-a", person="a3", version=3, mark="excused"))
        self.assertEqual(self.report()["closing"]["status"], "seal_supported")

    def test_after_seal_gate_revision_disputes_until_new_bridge(self):
        self.closed()
        self.append(event("reset-v2", "gate.read", 20, step=3, version=2, entries=4, exits=9, mode="reset"))
        status = self.report()["closing"]
        self.assertEqual(status["current"]["reasons"], ["gate_unproven"])
        self.assertEqual(status["frozen"]["gate"]["bridges"][0]["event"], "bridge-three")
        fresh = {**CLOSE[9], "id": "bridge-v2", "data": {**CLOSE[9]["data"], "read": "reset-v2"}}
        self.append(fresh)
        self.assertEqual(self.report()["closing"]["status"], "seal_supported")

from caveflow import escort, gate, journal, roster
from caveflow.conditions import zone_state
from caveflow.model import DomainError
from caveflow.store import connect, one
from tests.shift_support import CLOSE, DISPATCH, HANDOFF, OPEN, CFG, event, ShiftCase


class FieldEvidenceTests(ShiftCase):
    def test_roster_unknown_partial_ready_and_revision_cut(self):
        self.assertEqual(roster.presence(self.db, "sheet-a", 5)["decision"], "unknown_sheet")
        self.append(*OPEN)
        before = roster.presence(self.db, "sheet-a", 6)
        self.assertEqual((before["decision"], before["unmarked"]), ("unmarked_visitors", ["a2"]))
        self.append(DISPATCH[1])
        self.assertEqual(roster.presence(self.db, "sheet-a", 6)["present"], ["a1", "a2"])
        revised = event("roll-next", "roster.mark", 7, sheet="sheet-a", person="a2", version=2, mark="absent")
        self.assertEqual(self.append(revised)[0]["decision"], "mark_revised")
        self.assertEqual(roster.presence(self.db, "sheet-a", 6)["present"], ["a1", "a2"])
        self.assertEqual(roster.presence(self.db, "sheet-a", 7)["present"], ["a1"])
        with self.assertRaisesRegex(DomainError, "revision must advance"):
            self.append(event("skip", "roster.mark", 7, sheet="sheet-a", person="a2", version=4, mark="present"))
        with self.assertRaisesRegex(DomainError, "name a member"):
            self.append(event("outsider", "roster.mark", 7, sheet="sheet-a", person="x", version=1, mark="present"))

    def test_roster_rejects_duplicate_and_staff_on_sheet(self):
        for people in (["x", "x"], ["desk-day"], []):
            with self.subTest(people=people), self.assertRaises(DomainError):
                self.append(event("new-sheet", "roster.sheet", 4, sheet="other", guide="g-amber", members=people))
        result = self.append(event("new-sheet", "roster.sheet", 4, sheet="other", guide="g-amber", members=["x"]))
        self.assertEqual(result[0]["decision"], "sheet_opened")
        self.append(event("no-show", "roster.mark", 4, sheet="other", person="x", version=1, mark="absent"))
        self.assertEqual(roster.presence(self.db, "other", 4)["decision"], "no_visitors_present")

    def test_patrol_revision_and_expiry_change_local_capacity(self):
        self.append(*OPEN)
        self.assertEqual(zone_state(self.db, CFG, "pearl", 4)["capacity"], 4)
        limit = event("pearl-limit", "patrol.note", 2, zone="pearl", version=2, state="restricted", capacity=3,
                      inspector="g-relay", trace="rope-line")
        self.append(limit)
        view = zone_state(self.db, CFG, "pearl", 4)
        self.assertEqual((view["decision"], view["capacity"], view["patrol"]["trace"]), ("limited", 3, "rope-line"))
        self.assertEqual(zone_state(self.db, CFG, "pearl", 33)["patrol"]["decision"], "stale_patrol")
        with self.assertRaisesRegex(DomainError, "state and capacity disagree"):
            self.append(event("bad-patrol", "patrol.note", 5, zone="pearl", version=1, state="closed", capacity=4,
                              inspector="g-relay", trace="bad-trace"))

    def test_patrol_closed_prevents_landing_with_passing_lighting(self):
        self.append(*OPEN, *DISPATCH[:3])
        self.append(event("pearl-closed", "patrol.note", 6, zone="pearl", version=1, state="closed", capacity=0,
                          inspector="g-relay", trace="pearl-cones"))
        result = self.append(DISPATCH[4])[0]
        self.assertEqual(result["reason"], "destination_admission")
        self.assertEqual(result["landing"]["condition"]["lighting"]["decision"], "light_passed")
        self.assertEqual(self.report(7)["active_groups"][0]["zone"], "foyer")

    def test_light_round_cannot_borrow_channel_and_expires(self):
        self.append(*OPEN)
        self.assertEqual(zone_state(self.db, CFG, "pearl", 9)["lighting"]["decision"], "light_passed")
        result = self.append(*HANDOFF[5:7])[-1]
        self.assertEqual(result["decision"], "light_unproven")
        self.assertEqual(result["evidence"], ["l-pearl-new", "lp-new-marker"])
        self.assertEqual(self.append(HANDOFF[8])[0]["decision"], "light_passed")
        self.assertEqual(zone_state(self.db, CFG, "pearl", 30)["capacity"], 4)
        self.assertEqual(zone_state(self.db, CFG, "pearl", 31)["lighting"]["decision"], "stale_light_round")
        result = self.append(event("fail-marker", "light.check", 10, round="l-pearl-new", channel="marker",
                                   version=2, reading="weak"))[0]
        self.assertEqual(result["decision"], "light_failed")
        with self.assertRaisesRegex(DomainError, "same effective minute"):
            self.append(event("wrong-cut", "light.check", 11, round="l-pearl-new", channel="reserve", version=2, reading="pass"))

    def test_lighting_and_frozen_load_change_route_decision(self):
        self.append(*OPEN, *DISPATCH[:3])
        denied, alternate = self.append(*DISPATCH[3:5])
        self.assertEqual((denied["landing"]["condition"]["capacity"], denied["landing"]["requested"]), (2, 3))
        self.assertFalse(denied["landing"]["fits"])
        self.assertEqual(alternate["decision"], "moved")
        admitted = self.append(*DISPATCH[5:])[-1]
        self.assertEqual((admitted["decision"], admitted["landing"]["requested"]), ("moved", 2))
        non_neighbor = self.append(event("shortcut", "escort.move", 9, group="blue", guide="g-blue", destination="pearl"))[0]
        self.assertEqual(non_neighbor["reason"], "not_adjacent")

    def test_gate_gap_reset_and_exact_cut_are_independent(self):
        self.assertEqual(gate.reconcile(self.db, 0)["issue"]["reason"], "missing_anchor")
        self.append(*OPEN, DISPATCH[6])
        self.assertEqual(gate.reconcile(self.db, 8)["decision"], "covered")
        self.assertEqual(gate.reconcile(self.db, 9)["issue"]["reason"], "cutoff_not_sampled")
        gap = self.append(CLOSE[5])[0]["gate"]
        self.assertEqual((gap["issue"]["reason"], gap["through_step"]), ("step_gap", 1))
        self.append(CLOSE[7])
        cut = gate.reconcile(self.db, 20)
        self.assertEqual((cut["issue"]["reason"], cut["through_step"]), ("unwitnessed_reset", 2))
        bridge = self.append(CLOSE[9])[0]["gate"]
        self.assertEqual((bridge["decision"], bridge["entries"], bridge["exits"]), ("covered", 5, 5))
        self.assertEqual(bridge["bridges"][0]["event"], "bridge-three")

    def test_reset_bridge_must_match_revision_with_two_witnesses(self):
        self.append(*OPEN, DISPATCH[6], CLOSE[5], CLOSE[7])
        bad = {**CLOSE[9], "data": {**CLOSE[9]["data"], "witnesses": ["g-spare", "g-spare"]}}
        with self.assertRaisesRegex(DomainError, "must be unique"):
            self.append(bad)
        self.append(CLOSE[9])
        revision = event("reset-2", "gate.read", 20, step=3, version=2, entries=4, exits=9, mode="reset")
        self.assertEqual(self.append(revision)[0]["gate"]["issue"]["reason"], "unwitnessed_reset")
        duplicate_old = self.append(CLOSE[9])[0]
        self.assertTrue(duplicate_old["duplicate"])
        self.assertEqual(duplicate_old["gate"]["decision"], "covered")
        fresh = {**CLOSE[9], "id": "bridge-v2", "data": {**CLOSE[9]["data"], "read": "reset-2"}}
        self.assertEqual(self.append(fresh)[0]["gate"]["decision"], "covered")

    def test_gate_counter_and_capture_order_fail_closed(self):
        self.append(*OPEN, DISPATCH[6])
        read = event("decreasing", "gate.read", 9, step=2, version=1, entries=104, exits=80, mode="normal")
        self.assertEqual(self.append(read)[0]["gate"]["issue"]["reason"], "counter_regression")
        corrected = event("corrected", "gate.read", 9, step=2, version=2, entries=105, exits=80, mode="normal")
        self.assertEqual(self.append(corrected)[0]["gate"]["decision"], "covered")
        out_of_order = event("order", "gate.read", 8, step=3, version=1, entries=105, exits=80, mode="normal")
        self.assertEqual(self.append(out_of_order)[0]["gate"]["issue"]["reason"], "step_gap")
        cut = gate.reconcile(self.db, 9)
        self.assertEqual((cut["issue"]["reason"], cut["through_step"]), ("capture_order", 2))

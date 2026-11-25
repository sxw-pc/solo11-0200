import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from caveflow import bag, conditions, journal
from caveflow.model import DomainError
from tests.shift_support import CFG, FIXTURES, OPEN, ROOT, ShiftCase, event

MINUTE = 10


def core(kind, at, data):
    return {"kind": kind, "at": at, "data": data}


def cited(receipt_author, external_id, digest_value, relation=None):
    ref = {"author": receipt_author, "id": external_id, "digest": digest_value}
    if relation:
        ref["relation"] = relation
    return ref


def echo_receipts(author, reserve_reading):
    round_core = core("light.round", MINUTE, {
        "round": "echo-round", "zone": "echo", "tester": "g-spare",
    })
    marker_core = core("light.check", MINUTE, {
        "round": "echo-round", "channel": "marker", "version": 1, "reading": "pass",
    })
    reserve_core = core("light.check", MINUTE, {
        "round": "echo-round", "channel": "reserve", "version": 1,
        "reading": reserve_reading,
    })
    round_digest = bag.digest(round_core)
    reserve_digest = bag.digest(reserve_core)
    marker = {
        "id": "marker", **marker_core, "digest": bag.digest(marker_core),
        "references": [cited(author, "echo-round", round_digest, "round")],
    }
    reserve = {
        "id": "reserve", **reserve_core, "digest": reserve_digest,
        "references": [cited(author, "echo-round", round_digest, "round")],
    }
    round_receipt = {"id": "echo-round", **round_core, "digest": round_digest}
    return round_receipt, marker, reserve, reserve_digest


def make_echo_bags():
    pass_round, pass_marker, pass_reserve, pass_reserve_digest = echo_receipts("rhea", "pass")
    weak_round, weak_marker, weak_reserve, weak_reserve_digest = echo_receipts("ivo", "weak")

    supplement_patrol = core("patrol.note", MINUTE, {
        "zone": "pearl", "version": 1, "state": "restricted", "capacity": 2,
        "inspector": "g-relay", "trace": "pearl-backwater",
    })
    supplement = {
        "id": "pearl-supplement",
        "author": "jun",
        "receipts": [{
            "id": "pearl-note", **supplement_patrol,
            "digest": bag.digest(supplement_patrol),
            "references": [cited("rhea", "reserve", pass_reserve_digest, "dependency")],
        }],
    }
    strong = {
        "id": "pass-bag", "author": "rhea",
        "receipts": [pass_round, pass_marker, pass_reserve],
    }
    weak = {
        "id": "weak-bag", "author": "ivo",
        "receipts": [weak_round, weak_marker, weak_reserve],
    }
    return {
        "strong": strong, "weak": weak, "supplement": supplement,
        "pass_reserve_digest": pass_reserve_digest,
        "weak_reserve_digest": weak_reserve_digest,
    }


def ballot():
    return {
        "decision_id": "echo-minute-10-choice",
        "desk": "desk-day",
        "at": MINUTE,
        "plans": [
            {"name": "pass", "bags": ["pass-bag", "pearl-supplement"], "at": MINUTE},
            {"name": "weak", "bags": ["weak-bag"], "at": MINUTE},
        ],
    }


class WitnessBagTests(ShiftCase):
    def setUp(self):
        super().setUp()
        self.bags = make_echo_bags()
        self.append(*OPEN)

    def receive_echo(self):
        return bag.receive(self.db, [self.bags["strong"], self.bags["weak"], self.bags["supplement"]])

    def compare_echo(self):
        self.receive_echo()
        payload = ballot()
        return bag.compare_plans(self.db, payload)

    def test_two_complete_round_arrangements_are_independently_replayable(self):
        comparison = self.compare_echo()
        self.assertTrue(comparison["all_valid"], [
            (p["name"], p["issues"]) for p in comparison["plans"] if not p["valid"]
        ])
        pass_plan, weak_plan = comparison["plans"]
        self.assertEqual((pass_plan["capacity"]["echo"], pass_plan["capacity"]["pearl"]), (4, 2))
        self.assertEqual(pass_plan["decisions"]["echo"], "open")
        self.assertEqual((weak_plan["capacity"]["echo"], weak_plan["decisions"]["echo"]),
                         (2, "limited"))
        weak_echo = next(zone for zone in weak_plan["zones"] if zone["zone"] == "echo")
        self.assertEqual(weak_echo["lighting"]["decision"], "light_reduced")
        self.assertIn("echo", [z["zone"] for z in weak_plan["stopped_zones"]])

    def test_weak_choice_holds_entire_supplement_and_never_writes_pearl_alone(self):
        self.receive_echo()
        with self.assertRaisesRegex(DomainError, "not replayable"):
            bag.sign_selection(self.db, {
                "decision_id": "mixed-impossible", "desk": "desk-day", "at": MINUTE,
                "bags": ["weak-bag", "pearl-supplement"],
            })
        self.assertIsNone(self.db.execute("SELECT 1 FROM events WHERE id LIKE 'bag-%'").fetchone())
        self.assertEqual(conditions.zone_state(self.db, CFG, "pearl", MINUTE)["capacity"], 4)

        payload = ballot()
        payload["choice"] = "weak"
        signed = bag.sign_selection(self.db, payload)
        self.assertEqual(signed["decision"], "bags_signed")
        self.assertEqual((signed["capacity"]["echo"], signed["capacity"]["pearl"]), (2, 4))
        self.assertEqual(signed["lighting_hold_zones"][0]["zone"], "echo")
        held = {item["bag_id"]: item for item in signed["held_bags"]}
        self.assertIn("pearl-supplement", held)
        self.assertTrue(any(i["code"] == "unsatisfied_reference"
                            for i in held["pearl-supplement"]["issues"]))

        raw_supplement = bag.get_bag(self.db, "pearl-supplement")
        self.assertFalse(raw_supplement["mergeable"])
        self.assertTrue(any(i["code"] == "unsatisfied_reference" for i in raw_supplement["conflicts"]))
        self.assertEqual(raw_supplement["raw"]["author"], "jun")

        raw_pass = bag.get_bag(self.db, "pass-bag")
        self.assertFalse(raw_pass["mergeable"])
        self.assertTrue(any(i["code"] == "replay_failed" for i in raw_pass["conflicts"]))

        with self.assertRaisesRegex(DomainError, "not replayable"):
            bag.sign_selection(self.db, {"desk": "desk-night", "at": MINUTE,
                                         "bags": ["pearl-supplement"]})
        self.assertEqual(conditions.zone_state(self.db, CFG, "pearl", MINUTE)["capacity"], 4)
        pearl_events = self.db.execute(
            "SELECT COUNT(*) n FROM events e JOIN bag_receipts r ON r.event_id = e.id "
            "WHERE r.bag_id = 'pearl-supplement'").fetchone()["n"]
        self.assertEqual(pearl_events, 0)

    def test_passing_choice_merges_supplement_and_rebuild_keeps_signed_trace(self):
        self.receive_echo()
        payload = ballot()
        payload["choice"] = "pass"
        signed = bag.sign_selection(self.db, payload)
        decision_id = signed["decision_id"]

        echo = conditions.zone_state(self.db, CFG, "echo", MINUTE)
        pearl = conditions.zone_state(self.db, CFG, "pearl", MINUTE)
        self.assertEqual((echo["capacity"], echo["lighting"]["decision"]), (4, "light_passed"))
        self.assertEqual((pearl["capacity"], pearl["patrol"]["decision"]), (2, "restricted"))
        self.assertTrue(any(d["target_author"] == "rhea" and d["satisfied"]
                            for d in signed["satisfied_dependencies"]))
        self.assertEqual({lock["bag_id"] for lock in self.db.execute(
            "SELECT DISTINCT bag_id FROM bag_locks WHERE decision_id = ?", (decision_id,))},
            {"pass-bag", "pearl-supplement"})

        rebuilt = journal.rebuild(self.db)
        self.assertGreaterEqual(rebuilt["events"], 18)
        verified = bag.verify_decision(self.db, decision_id)
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["originals"], 4)
        self.assertEqual(verified["zones"]["echo"]["capacity"], 4)
        self.assertEqual(verified["zones"]["pearl"]["capacity"], 2)
        self.assertEqual(conditions.zone_state(self.db, CFG, "echo", MINUTE)["capacity"], 4)
        self.assertEqual(conditions.zone_state(self.db, CFG, "pearl", MINUTE)["capacity"], 2)

        again = bag.sign_selection(self.db, payload)
        self.assertTrue(again["duplicate"])
        bag.receive_bags(self.db, [self.bags["strong"]])
        event_count = self.db.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
        journal.rebuild(self.db)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM events").fetchone()["n"], event_count)

    def test_reference_can_arrive_before_its_original(self):
        bag.receive_bags(self.db, [self.bags["supplement"]])
        early = bag.get_bag(self.db, "pearl-supplement")
        self.assertTrue(any(i["code"] == "missing_reference" for i in early["conflicts"]))

        bag.receive_bags(self.db, [self.bags["strong"]])
        waiting = bag.plan_selection(self.db, ["pearl-supplement"])
        self.assertFalse(waiting["valid"])
        self.assertTrue(any(i["code"] == "unsatisfied_reference" for i in waiting["issues"]))

        together = bag.plan_selection(self.db, ["pass-bag", "pearl-supplement"], at=MINUTE)
        self.assertTrue(together["valid"], together["issues"])

        reordered = bag.receive_bags(self.db, [self.bags["weak"], self.bags["strong"]])
        self.assertFalse(reordered[0]["duplicate"])
        self.assertTrue(reordered[1]["duplicate"])

    def test_higher_existing_version_is_a_conflict_reason(self):
        self.append(
            event("pearl-h1", "patrol.note", 20, zone="pearl", version=1, state="restricted",
                  capacity=3, inspector="g-relay", trace="first"),
            event("pearl-h2", "patrol.note", 20, zone="pearl", version=2, state="open",
                  capacity=4, inspector="g-relay", trace="second"),
        )
        old_core = core("patrol.note", 20, {
            "zone": "pearl", "version": 1, "state": "closed", "capacity": 0,
            "inspector": "g-relay", "trace": "late-old",
        })
        late_bag = {
            "id": "old-version-bag", "author": "trace",
            "receipts": [{"id": "pearl-old", **old_core, "digest": bag.digest(old_core)}],
        }
        bag.receive_bags(self.db, [late_bag])
        preview = bag.plan_selection(self.db, ["old-version-bag"])
        self.assertFalse(preview["valid"])
        self.assertTrue(any(i["code"] == "higher_version_exists" for i in preview["issues"]))
        self.assertTrue(any("revision must advance" in i["message"] or "already has version" in i["message"]
                            for i in preview["issues"]))

    def test_cross_author_same_external_id_does_not_match_by_version(self):
        self.assertNotEqual(bag.original_event_id("rhea", "reserve"),
                            bag.original_event_id("ivo", "reserve"))
        alpha_core = core("patrol.note", 30, {
            "zone": "echo", "version": 1, "state": "open", "capacity": 4,
            "inspector": "g-spare", "trace": "alpha",
        })
        beta_core = core("patrol.note", 30, {
            "zone": "pearl", "version": 1, "state": "restricted", "capacity": 2,
            "inspector": "g-relay", "trace": "beta",
        })
        alpha = {"id": "p", **alpha_core, "digest": bag.digest(alpha_core)}
        beta = {"id": "p", **beta_core, "digest": bag.digest(beta_core)}
        alpha_bag = {"id": "alpha-bag", "author": "alpha", "receipts": [alpha]}
        beta_bag = {"id": "beta-bag", "author": "beta", "receipts": [beta]}

        wrong_revision = core("patrol.note", 30, {
            "zone": "echo", "version": 2, "state": "open", "capacity": 4,
            "inspector": "g-spare", "trace": "alpha-two",
        })
        wrong_bag = {
            "id": "wrong-revision-bag", "author": "alpha",
            "receipts": [{
                "id": "p2", **wrong_revision, "digest": bag.digest(wrong_revision),
                "references": [cited("beta", "p", bag.digest(beta_core), "revision")],
            }],
        }
        bag.receive_bags(self.db, [alpha_bag, beta_bag, wrong_bag])
        wrong_plan = bag.plan_selection(self.db, ["alpha-bag", "beta-bag", "wrong-revision-bag"])
        self.assertFalse(wrong_plan["valid"])
        self.assertTrue(any(i["code"] == "missing_same_domain_previous_reference"
                            for i in wrong_plan["issues"]))

        right_bag = {
            "id": "right-revision-bag", "author": "alpha",
            "receipts": [{
                "id": "p3", **wrong_revision, "digest": bag.digest(wrong_revision),
                "references": [cited("alpha", "p", bag.digest(alpha_core), "revision")],
            }],
        }
        bag.receive_bags(self.db, [right_bag])
        right_plan = bag.plan_selection(self.db, ["alpha-bag", "right-revision-bag"], at=30)
        self.assertTrue(right_plan["valid"], right_plan["issues"])

    def test_channels_from_different_round_originals_cannot_be_stitched(self):
        minute = 40
        round_core = core("light.round", minute, {
            "round": "shared-label", "zone": "echo", "tester": "g-spare",
        })
        marker_core = core("light.check", minute, {
            "round": "shared-label", "channel": "marker", "version": 1, "reading": "pass",
        })
        reserve_core = core("light.check", minute, {
            "round": "shared-label", "channel": "reserve", "version": 1, "reading": "pass",
        })
        marker_bag = {"id": "marker-only-bag", "author": "alice", "receipts": [
            {"id": "round-a", **round_core, "digest": bag.digest(round_core)},
            {"id": "marker-a", **marker_core, "digest": bag.digest(marker_core)},
        ]}
        reserve_bag = {"id": "reserve-only-bag", "author": "bob", "receipts": [
            {"id": "reserve-b", **reserve_core, "digest": bag.digest(reserve_core)},
        ]}
        bag.receive_bags(self.db, [marker_bag, reserve_bag])
        preview = bag.plan_selection(self.db, ["marker-only-bag", "reserve-only-bag"], at=minute)
        # Bob's check is not remapped onto Alice's exact round original, so the
        # pair cannot replay as one complete qualified round.
        self.assertFalse(preview["valid"])
        mapped_rounds = {
            event["id"]: event["data"].get("round")
            for event in preview["events"] if event["kind"] == "light.check"
        }
        self.assertEqual(len(set(mapped_rounds.values())), 2)
        with self.assertRaises(DomainError):
            bag.sign_selection(self.db, {"desk": "desk-day", "at": minute,
                                         "bags": ["marker-only-bag", "reserve-only-bag"]})

    def test_supplemental_channel_revision_must_follow_exact_passing_reserve(self):
        self.receive_echo()
        round_digest = self.bags["strong"]["receipts"][0]["digest"]
        v2_core = core("light.check", MINUTE, {
            "round": "echo-round", "channel": "reserve", "version": 2, "reading": "pass",
        })
        patrol_core = core("patrol.note", MINUTE, {
            "zone": "pearl", "version": 1, "state": "restricted", "capacity": 2,
            "inspector": "g-relay", "trace": "pearl-late",
        })
        revision_bag = {"id": "reserve-v2-bag", "author": "jun", "receipts": [
            {"id": "reserve-v2", **v2_core, "digest": bag.digest(v2_core), "references": [
                cited("rhea", "echo-round", round_digest, "round"),
                cited("rhea", "reserve", self.bags["pass_reserve_digest"], "revision"),
            ]},
            {"id": "pearl-note-v2bag", **patrol_core, "digest": bag.digest(patrol_core)},
        ]}
        bag.receive_bags(self.db, [revision_bag])

        weak_v2 = bag.plan_selection(self.db, ["weak-bag", "reserve-v2-bag"], at=MINUTE)
        self.assertFalse(weak_v2["valid"])
        self.assertTrue(any(i["code"] == "unsatisfied_reference" for i in weak_v2["issues"]))

        with_pass = bag.plan_selection(self.db, ["pass-bag", "reserve-v2-bag"], at=MINUTE)
        self.assertTrue(with_pass["valid"], with_pass["issues"])
        bag.sign_selection(self.db, {"decision_id": "with-v2", "desk": "desk-day", "at": MINUTE,
                                     "bags": ["pass-bag", "reserve-v2-bag"]})
        echo = conditions.zone_state(self.db, CFG, "echo", MINUTE)
        self.assertEqual((echo["capacity"], echo["lighting"]["readings"]["reserve"]), (4, "pass"))
        self.assertEqual(conditions.zone_state(self.db, CFG, "pearl", MINUTE)["capacity"], 2)

    def test_arrangement_enumeration_finds_pass_with_supplement_and_weak_alone(self):
        self.receive_echo()
        arrangements = bag.arrange(self.db, ["pass-bag", "weak-bag", "pearl-supplement"], at=MINUTE)
        bag_sets = {tuple(plan["bags"]) for plan in arrangements["plans"]}
        self.assertIn(("pass-bag", "pearl-supplement"), bag_sets)
        self.assertIn(("weak-bag",), bag_sets)
        self.assertNotIn(("weak-bag", "pearl-supplement"), bag_sets)

    def test_at_most_eight_bags(self):
        one_bag = self.bags["strong"]
        with self.assertRaisesRegex(DomainError, "witness bags"):
            bag.receive_bags(self.db, [one_bag] * 9)


class BagCliTests(unittest.TestCase):
    def test_cli_receive_compare_sign_rebuild_and_verify(self):
        with tempfile.TemporaryDirectory(prefix="caveflow-bag-", dir=ROOT) as folder:
            root = Path(folder)
            db = root / "shift.sqlite"
            bags_path = root / "bags.json"
            choice_path = root / "choice.json"
            open_path = root / "opening.json"
            open_path.write_text(json.dumps(OPEN), encoding="utf-8")
            bags = make_echo_bags()
            bags_path.write_text(json.dumps([bags["strong"], bags["weak"], bags["supplement"]]),
                                 encoding="utf-8")
            choice = ballot()
            choice["choice"] = "pass"
            choice_path.write_text(json.dumps(choice), encoding="utf-8")

            def call(*args, code=0):
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "caveflow", "--db", str(db), *map(str, args)],
                    cwd=ROOT, text=True, encoding="utf-8", capture_output=True, check=False)
                self.assertEqual(result.returncode, code, result.stderr)
                return json.loads(result.stdout) if code == 0 else result.stderr

            self.assertEqual(call("init", FIXTURES / "shift.json")["decision"], "initialized")
            self.assertEqual(len(call("load", open_path)), len(OPEN))
            received = call("bags", bags_path)
            self.assertEqual(len(received), 3)
            self.assertEqual(len(call("bag", "list")), 3)
            signed = call("bag", "sign", choice_path)
            self.assertEqual(signed["decision_id"], choice["decision_id"])
            self.assertEqual(signed["capacity"]["echo"], 4)
            self.assertTrue(call("merge", choice_path)["duplicate"])

            rebuilt = call("rebuild")
            self.assertTrue(rebuilt["bag_verification"]["verified"])
            self.assertEqual(rebuilt["bag_verification"]["decisions"], 1)
            verified = call("bag", "verify", choice["decision_id"])
            self.assertTrue(verified["verified"])
            self.assertEqual(verified["originals"], 4)
            shown = call("bag", "show", "pearl-supplement")
            self.assertEqual(shown["author"], "jun")
            self.assertTrue(shown["mergeable"])


if __name__ == "__main__":
    unittest.main()

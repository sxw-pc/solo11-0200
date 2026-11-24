from copy import deepcopy

from caveflow.air_samples import review
from caveflow.cli import load
from caveflow.model import DomainError
from tests.shift_support import CFG, FIXTURES, OPEN, ShiftCase, event


class AirSampleTests(ShiftCase):
    def packet(self):
        self.append(*OPEN)
        return load(FIXTURES / "air-samples.json")

    def test_probe_readings_apply_the_referenced_zero(self):
        samples = review(self.db, CFG, self.packet())["samples"]
        self.assertEqual([(row["state"], row["value"]) for row in samples], [("MEASURED", 60), ("MEASURED", 25)])
        self.assertEqual(samples[0]["access"]["patrol"]["evidence"], "p-pearl")

    def test_evidence_revision_changes_admissibility(self):
        packet = self.packet()
        self.append(event("closed-p", "patrol.note", 2, zone="pearl", version=2, state="closed",
                          capacity=0, inspector="g-relay", trace="closed-pearl"))
        self.assertEqual(review(self.db, CFG, packet)["samples"][0]["state"], "ZONE_UNAVAILABLE")
        packet["samples"][1]["at"] = 13
        self.assertEqual(review(self.db, CFG, packet)["samples"][1]["state"], "UNCALIBRATED")

    def test_repeated_original_and_conflicting_reading(self):
        packet = self.packet()
        packet["samples"].append(deepcopy(packet["samples"][0]))
        self.assertEqual(len(review(self.db, CFG, packet)["samples"]), 2)
        packet["samples"][-1]["raw"] += 1
        with self.assertRaisesRegex(DomainError, "sample identifier conflict"):
            review(self.db, CFG, packet)

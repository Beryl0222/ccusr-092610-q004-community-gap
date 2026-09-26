import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from community_gap.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_fields_are_stable(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(x.field for x in issues), [x.field for x in issues])

    def test_time_and_version_boundaries(self) -> None:
        event = dict(self.sample, occurred_at="2026-09-25T10:00:00", version=0)
        codes = {(x.field, x.code) for x in validate_event(event, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_event_payload_is_required(self) -> None:
        event = dict(self.sample, event_type="NEED_AGGREGATED", payload={})
        self.assertIn(("payload.community_revision", "required"), [(x.field, x.code) for x in validate_event(event, self.schema)])

    def test_unknown_event_is_rejected(self) -> None:
        issues = validate_event(dict(self.sample, event_type="UNKNOWN"), self.schema)
        self.assertIn(("event_type", "unsupported_value"), [(x.field, x.code) for x in issues])


class ExtendedContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        cls.base = {
            "event_id": "evt-ext-1",
            "aggregate_type": "community_revision",
            "aggregate_id": "c-1",
            "occurred_at": "2026-09-26T08:00:00+08:00",
            "version": 1,
        }

    def test_boundary_payload_requires_zones(self) -> None:
        event = dict(self.base, event_type="BOUNDARY_PUBLISHED", payload={"community_id": "c-1", "revision": 1})
        self.assertIn(("payload.zones", "required"), [(x.field, x.code) for x in validate_event(event, self.schema)])

    def test_coverage_review_payload_contract(self) -> None:
        event = dict(self.base, aggregate_type="coverage_decision", aggregate_id="d-1",
                     event_type="COVERAGE_REVIEWED", payload={"community_id": "c-1", "outcome": "gap"})
        fields = [x.field for x in validate_event(event, self.schema)]
        self.assertIn("payload.reviewer", fields)
        self.assertIn("payload.safety_review_passed", fields)

    def test_funding_round_aggregate_is_registered(self) -> None:
        event = dict(self.base, aggregate_type="funding_round", aggregate_id="round-1",
                     event_type="FUND_ROUND_OPENED", payload={"total_budget": 100})
        self.assertEqual([], validate_event(event, self.schema))

    def test_consent_withdrawn_requires_resident_ref(self) -> None:
        event = dict(self.base, aggregate_type="service_need", aggregate_id="n-1",
                     event_type="CONSENT_WITHDRAWN", payload={})
        self.assertIn(("payload.resident_ref", "required"), [(x.field, x.code) for x in validate_event(event, self.schema)])


if __name__ == "__main__":
    unittest.main()

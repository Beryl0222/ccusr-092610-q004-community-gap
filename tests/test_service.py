import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from community_gap import DomainError, GapAdjudicationService


def make_service(**kw) -> GapAdjudicationService:
    return GapAdjudicationService(**kw)


def covered_setup(svc, community="c-1", category="维修"):
    svc.publish_boundary(community, 1, ["z-1", "z-2"])
    svc.aggregate_need(community, category, zone="z-1", time_window="daytime", support_total=15)
    svc.propose_site(
        "s-1",
        applicant_id="m-1",
        categories=[category],
        community_id=community,
        zone="z-2",
        service_area={community: None},
        capacity=100,
        opens_at="08:00",
        closes_at="20:00",
    )
    svc.change_site_status("s-1", "open")
    svc.verify_site("s-1", ["ev-1"], verified_by="insp-1")


class WorkflowTests(unittest.TestCase):
    def test_happy_path_review_and_verify(self) -> None:
        svc = make_service()
        covered_setup(svc)
        svc.review_coverage("c-1", "维修", outcome="covered", reviewer="rev-1", safety_review_passed=True)
        svc.verify_decision("decision:c-1:维修", ["accept-1"], verified_by="qa-1")
        decision = svc.decisions["decision:c-1:维修"]
        self.assertTrue(decision.verified)
        self.assertEqual(1, decision.revision)
        self.assertEqual(["s-1"], decision.counted_site_ids)
        self.assertEqual(15, decision.demand)

    def test_applicant_cannot_review_own_coverage(self) -> None:
        svc = make_service()
        covered_setup(svc)
        with self.assertRaises(DomainError) as ctx:
            svc.review_coverage("c-1", "维修", outcome="covered", reviewer="m-1", safety_review_passed=True)
        self.assertEqual("SELF_REVIEW_FORBIDDEN", ctx.exception.code)

    def test_resident_support_cannot_replace_safety_review(self) -> None:
        svc = make_service()
        covered_setup(svc)
        with self.assertRaises(DomainError) as ctx:
            svc.review_coverage("c-1", "维修", outcome="covered", reviewer="rev-1", safety_review_passed=False)
        self.assertEqual("SAFETY_REVIEW_REQUIRED", ctx.exception.code)

    def test_covered_requires_system_checks(self) -> None:
        svc = make_service()
        svc.publish_boundary("c-1", 1, ["z-1"])
        svc.aggregate_need("c-1", "夜间购药", zone="z-1", time_window="night", support_total=20)
        with self.assertRaises(DomainError) as ctx:
            svc.review_coverage("c-1", "夜间购药", outcome="covered", reviewer="rev-1", safety_review_passed=True)
        self.assertEqual("ASSESSMENT_NOT_SATISFIED", ctx.exception.code)
        svc.review_coverage("c-1", "夜间购药", outcome="gap", reviewer="rev-1", safety_review_passed=False)
        self.assertEqual("gap", svc.decisions["decision:c-1:夜间购药"].outcome)

    def test_resident_opinion_triggers_review_but_keeps_decision(self) -> None:
        svc = make_service()
        covered_setup(svc)
        svc.review_coverage("c-1", "维修", outcome="covered", reviewer="rev-1", safety_review_passed=True)
        decision = svc.decisions["decision:c-1:维修"]
        self.assertFalse(decision.review_pending)
        # 居民集中反映：推动复核，但结论不被直接改写
        svc.aggregate_need("c-1", "维修", zone="z-1", time_window="daytime", support_total=80)
        self.assertTrue(decision.review_pending)
        self.assertEqual("covered", decision.outcome)
        self.assertTrue(svc.queue.has_open("coverage_review:c-1:维修"))

    def test_boundary_change_stales_unverified_but_keeps_verified(self) -> None:
        svc = make_service()
        covered_setup(svc)
        svc.aggregate_need("c-1", "托老", zone="z-1", time_window="daytime", support_total=20)
        svc.review_coverage("c-1", "维修", outcome="covered", reviewer="rev-1", safety_review_passed=True)
        svc.verify_decision("decision:c-1:维修", ["accept-1"], verified_by="qa-1")
        svc.review_coverage("c-1", "托老", outcome="gap", reviewer="rev-2", safety_review_passed=False)
        svc.publish_boundary("c-1", 2, ["z-1", "z-2", "z-3"])
        verified = svc.decisions["decision:c-1:维修"]
        pending = svc.decisions["decision:c-1:托老"]
        self.assertFalse(verified.stale)  # 已验收：保留当时边界与服务依据
        self.assertEqual(1, verified.revision)
        self.assertTrue(pending.stale)  # 未验收：受规划变更影响
        with self.assertRaises(DomainError) as ctx:
            svc.verify_decision("decision:c-1:托老", ["accept-2"], verified_by="qa-1")
        self.assertEqual("DECISION_STALE", ctx.exception.code)
        self.assertTrue(svc.queue.has_open("coverage_review:c-1:托老"))

    def test_ingest_is_idempotent_and_versions_are_strict(self) -> None:
        svc = make_service()
        event = {
            "event_id": "evt-fixed-1",
            "event_type": "BOUNDARY_PUBLISHED",
            "aggregate_type": "community_revision",
            "aggregate_id": "c-9",
            "occurred_at": "2026-09-26T08:00:00+08:00",
            "version": 1,
            "payload": {"community_id": "c-9", "revision": 1, "zones": ["z-1"]},
        }
        first = svc.ingest(event)
        second = svc.ingest(dict(event))
        self.assertTrue(first.applied)
        self.assertTrue(second.duplicate)
        conflict = dict(event, event_id="evt-fixed-2")
        with self.assertRaises(DomainError) as ctx:
            svc.ingest(conflict)
        self.assertEqual("VERSION_CONFLICT", ctx.exception.code)

    def test_contract_violation_is_raised_with_details(self) -> None:
        svc = make_service()
        with self.assertRaises(DomainError) as ctx:
            svc.ingest({"event_id": "e-1", "event_type": "NEED_AGGREGATED"})
        self.assertEqual("CONTRACT_VIOLATION", ctx.exception.code)
        self.assertTrue(any(d["field"] == "occurred_at" for d in ctx.exception.details))


if __name__ == "__main__":
    unittest.main()

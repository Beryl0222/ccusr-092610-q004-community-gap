import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from community_gap import GapAdjudicationService


def make_service(**kw) -> GapAdjudicationService:
    return GapAdjudicationService(**kw)


def add_need(svc, community="c-1", category="维修", zone="z-1", window="daytime", support=15, group="general"):
    return svc.aggregate_need(community, category, zone=zone, time_window=window, support_total=support, vulnerable_group=group)


def add_site(svc, site_id="s-1", applicant="m-1", categories=("维修",), community="c-1", zone="z-1",
             area=None, capacity=100, opens="08:00", closes="20:00"):
    return svc.propose_site(
        site_id,
        applicant_id=applicant,
        categories=list(categories),
        community_id=community,
        zone=zone,
        service_area=area if area is not None else {community: None},
        capacity=capacity,
        opens_at=opens,
        closes_at=closes,
    )


def open_and_verify(svc, site_id="s-1", inspector="insp-1"):
    svc.change_site_status(site_id, "open")
    svc.verify_site(site_id, ["evidence-1"], verified_by=inspector)


class AdjudicationTests(unittest.TestCase):
    def test_night_window_requires_night_hours(self) -> None:
        svc = make_service()
        svc.publish_boundary("c-1", 1, ["z-1"])
        add_need(svc, category="夜间购药", window="night", support=12)
        add_site(svc, categories=("夜间购药",), opens="08:00", closes="18:00")
        open_and_verify(svc)
        result = svc.assess("c-1", "夜间购药")
        self.assertEqual("gap", result.outcome)
        self.assertIn("HOURS_MISMATCH", [r.code for r in result.reasons])
        # 延长营业到 24:00 后覆盖夜间需求
        add_site(svc, categories=("夜间购药",), opens="08:00", closes="24:00")
        self.assertEqual("covered", svc.assess("c-1", "夜间购药").outcome)

    def test_planned_and_suspended_sites_do_not_count(self) -> None:
        svc = make_service()
        svc.publish_boundary("c-1", 1, ["z-1"])
        add_need(svc)
        add_site(svc, "s-1")
        add_site(svc, "s-2", applicant="m-2")
        svc.verify_site("s-1", ["ev"], verified_by="insp-1")  # 验收了但仍在计划中
        svc.change_site_status("s-2", "suspended")
        svc.verify_site("s-2", ["ev"], verified_by="insp-1")
        result = svc.assess("c-1", "维修")
        self.assertEqual("gap", result.outcome)
        codes = {item["code"] for item in result.excluded_supply}
        self.assertEqual({"SITE_NOT_OPEN", "SITE_SUSPENDED"}, codes)
        labels = {row["site_id"]: row["status_label"] for row in svc.service_directory("c-1")}
        self.assertEqual("计划中", labels["s-1"])
        self.assertEqual("暂时停业", labels["s-2"])

    def test_unverified_site_is_not_supply(self) -> None:
        svc = make_service()
        svc.publish_boundary("c-1", 1, ["z-1"])
        add_need(svc)
        add_site(svc)
        svc.change_site_status("s-1", "open")
        result = svc.assess("c-1", "维修")
        self.assertEqual("gap", result.outcome)
        self.assertIn("SITE_UNVERIFIED", [r.code for r in result.reasons])

    def test_walk_barrier_blocks_elderly_but_not_general(self) -> None:
        svc = make_service()
        svc.publish_boundary(
            "c-1", 1, ["z-1", "z-2"],
            barriers=[{"barrier_id": "b-1", "zones": ["z-1", "z-2"], "blocks": ["elderly"], "note": "无信号灯主干道"}],
        )
        add_need(svc, category="托老", zone="z-1", group="elderly", support=20)
        add_site(svc, categories=("托老",), zone="z-2")
        open_and_verify(svc)
        result = svc.assess("c-1", "托老")
        self.assertEqual("gap", result.outcome)
        barrier_reasons = [r for r in result.reasons if r.code == "WALK_BARRIER"]
        self.assertEqual(1, len(barrier_reasons))
        self.assertEqual("b-1", barrier_reasons[0].details["barrier_id"])
        # 一般人群不受该障碍阻断
        add_need(svc, category="托老", zone="z-1", group="general", support=20)
        self.assertEqual("covered", svc.assess("c-1", "托老").outcome)

    def test_shared_site_capacity_is_not_double_counted(self) -> None:
        svc = make_service()
        svc.publish_boundary("c-1", 1, ["z-1"])
        svc.publish_boundary("c-2", 1, ["z-1"])
        add_need(svc, "c-1", support=60)
        add_need(svc, "c-2", support=60)
        add_site(svc, area={"c-1": None, "c-2": None}, capacity=100)
        open_and_verify(svc)
        svc.review_coverage("c-1", "维修", outcome="covered", reviewer="rev-1", safety_review_passed=True)
        result = svc.assess("c-2", "维修")
        self.assertEqual("gap", result.outcome)
        self.assertIn("SHARED_CAPACITY_EXHAUSTED", [r.code for r in result.reasons])
        self.assertEqual(40, result.reasons[-1].details["remaining"])
        # 汇总统计中该网点只计一次
        summary = svc.supply_summary()
        self.assertEqual(1, summary["total_sites"])
        self.assertEqual(100, summary["total_capacity"])
        self.assertEqual([{"community_id": "c-1", "category": "维修"}], summary["covered_pairs"])

    def test_no_need_and_low_support_outcomes(self) -> None:
        svc = make_service()
        svc.publish_boundary("c-1", 1, ["z-1"])
        self.assertEqual("no_demand", svc.assess("c-1", "维修").outcome)
        add_need(svc, support=3)
        result = svc.assess("c-1", "维修")
        self.assertEqual("observation", result.outcome)
        self.assertIn("SUPPORT_BELOW_THRESHOLD", [r.code for r in result.reasons])

    def test_explain_gap_reports_reasons_and_queue_state(self) -> None:
        svc = make_service()
        svc.publish_boundary("c-1", 1, ["z-1"])
        add_need(svc, category="夜间购药", window="night", support=30)
        explanation = svc.explain_gap("c-1", "夜间购药")
        self.assertEqual("gap", explanation["assessment"]["outcome"])
        self.assertEqual("缺口", explanation["assessment"]["outcome_label"])
        self.assertIn("NO_SITE_IN_RADIUS", [r["code"] for r in explanation["assessment"]["reasons"]])
        self.assertTrue(explanation["queued"])  # 支持度达阈值后已入核查队列
        self.assertIsNone(explanation["decision"])


if __name__ == "__main__":
    unittest.main()

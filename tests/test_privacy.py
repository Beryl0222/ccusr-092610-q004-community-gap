import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from community_gap import DomainError, GapAdjudicationService


def make_service() -> GapAdjudicationService:
    svc = GapAdjudicationService(anonymity_threshold=5)
    svc.publish_boundary("c-1", 1, ["z-1"])
    return svc


class PrivacyTests(unittest.TestCase):
    def test_small_cohort_is_suppressed_in_public_view(self) -> None:
        svc = make_service()
        svc.aggregate_need("c-1", "维修", zone="z-1", time_window="daytime", support_total=3)
        view = svc.public_need_view("c-1", "维修")
        self.assertTrue(view["suppressed"])
        self.assertIsNone(view["support"])
        svc.aggregate_need("c-1", "维修", zone="z-1", time_window="daytime", support_total=12)
        view = svc.public_need_view("c-1", "维修")
        self.assertFalse(view["suppressed"])
        self.assertEqual(12, view["support"])

    def test_withdrawal_keeps_aggregate_but_never_identifies(self) -> None:
        svc = make_service()
        svc.aggregate_need("c-1", "托老", zone="z-1", time_window="daytime", support_total=12)
        svc.withdraw_consent("c-1", "托老", "resident-pseudo-001")
        # 汇总统计保留
        self.assertEqual(12, svc.needs["need:c-1:托老"].support_total)
        self.assertEqual(12, svc.public_need_view("c-1", "托老")["support"])
        # 公开视图不包含任何可反向识别个人的字段
        dumped = json.dumps(svc.public_need_view("c-1", "托老"), ensure_ascii=False)
        self.assertNotIn("resident-pseudo-001", dumped)
        self.assertNotIn("withdrawn", dumped)

    def test_withdrawal_is_idempotent_by_event_id(self) -> None:
        svc = make_service()
        svc.aggregate_need("c-1", "托老", zone="z-1", time_window="daytime", support_total=12)
        first = svc.withdraw_consent("c-1", "托老", "resident-pseudo-002", event_id="evt-wd-1")
        second = svc.withdraw_consent("c-1", "托老", "resident-pseudo-002", event_id="evt-wd-1")
        self.assertTrue(first.applied)
        self.assertTrue(second.duplicate)
        self.assertEqual({"resident-pseudo-002"}, svc.needs["need:c-1:托老"].withdrawn_refs)

    def test_withdrawal_requires_existing_need(self) -> None:
        svc = make_service()
        with self.assertRaises(DomainError) as ctx:
            svc.withdraw_consent("c-1", "维修", "resident-pseudo-003")
        self.assertEqual("UNKNOWN_NEED", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()

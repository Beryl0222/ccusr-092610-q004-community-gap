"""缺口裁决引擎与服务规则测试。"""

from __future__ import annotations

import threading
import tempfile
import unittest
from pathlib import Path

from community_gap.adjudication import Verdict, adjudicate
from community_gap.domain import GapReason
from community_gap.events import ConcurrencyError, DuplicateEventError
from community_gap.funding import FundingError
from community_gap.service import GapAdjudicationService, ServiceError
from community_gap.state import fold

from tests._scenarios import build_service


def seed_basic_world(service: GapAdjudicationService) -> None:
    """县城社区 C1：半径 0.5km；托老、维修、夜间购药三条需求。"""
    service.revise_community(
        "rev1", "C1", 1, 0.5,
        center_km=(0.0, 0.0), boundary_ref="C1-边界-2026Q1",
    )
    service.submit_need(
        "need-elderly", "rev1", "elderly_care",
        support_count=42, elderly_focus=True, location_km=(0.0, 0.0),
        resident_ref="R-1001",
    )
    service.submit_need(
        "need-repair", "rev1", "repair",
        support_count=18, location_km=(0.0, 0.0),
    )
    service.submit_need(
        "need-night-pharmacy", "rev1", "pharmacy",
        support_count=33, location_km=(0.0, 0.0),
        required_moments=("mon 23:30", "thu 23:30"),
    )


class AdjudicationRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self._tmp.name))
        seed_basic_world(self.service)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def verdict(self, need_id: str):
        return next(
            v for v in self.service.adjudicate().verdicts if v.need_id == need_id
        )

    def test_no_site_is_gap_with_explicit_reason(self) -> None:
        v = self.verdict("need-repair")
        self.assertEqual(v.verdict, Verdict.GAP)
        self.assertEqual(v.reasons, [])
        self.assertIn("没有任何登记网点", "\n".join(v.explain_lines()))

    def test_planned_site_does_not_count_as_covered(self) -> None:
        # 开店意向落在半径内，但尚未营业
        self.service.propose_site(
            "site-repair", "A-1", ["repair"], (0.2, 0.0), ["C1"],
        )
        v = self.verdict("need-repair")
        self.assertEqual(v.verdict, Verdict.GAP)
        self.assertIn(GapReason.NOT_OPEN_YET, v.reasons)
        self.assertIn(GapReason.SAFETY_REVIEW_PENDING, v.reasons)
        self.assertIn(GapReason.FUNDING_NOT_RESERVED, v.reasons)

    def test_out_of_radius_intent_does_not_cover(self) -> None:
        # 商户意向与居民需求不在同一服务半径
        self.service.propose_site(
            "site-repair-far", "A-1", ["repair"], (2.0, 0.0), ["C1"],
            status="open",
            opening_hours=[{"start": "mon 08:00", "end": "sun 20:00"}],
        )
        v = self.verdict("need-repair")
        self.assertEqual(v.verdict, Verdict.GAP)
        self.assertIn(GapReason.OUT_OF_RADIUS, v.reasons)

    def test_elderly_street_crossing_blocks_straight_line_coverage(self) -> None:
        # 网点直线距离够近，但过街无信号灯：不能用覆盖数量掩盖
        self.service.propose_site(
            "site-elderly", "A-2", ["elderly_care"], (0.3, 0.0), ["C1"],
            status="open",
            opening_hours=[{"start": "mon 00:00", "end": "sun 23:59"}],
            approaches=[{
                "community_id": "C1",
                "distance_km": 0.32,
                "walk_minutes": 8,
                "barriers": [{"kind": "arterial", "signalized": False}],
            }],
        )
        v = self.verdict("need-elderly")
        self.assertEqual(v.verdict, Verdict.GAP)
        self.assertIn(GapReason.STREET_CROSSING_BLOCKED, v.reasons)

        # 增设信号灯后老年友好可达（还需安全审查）
        self.service.propose_site(
            "site-elderly-2", "A-3", ["elderly_care"], (0.3, 0.0), ["C1"],
            status="open",
            opening_hours=[{"start": "mon 00:00", "end": "sun 23:59"}],
            approaches=[{
                "community_id": "C1",
                "distance_km": 0.32,
                "walk_minutes": 8,
                "barriers": [{"kind": "arterial", "signalized": True}],
            }],
        )
        v2 = self.verdict("need-elderly")
        # 两个候选都在，但只有第二个可达；安全未过，仍缺口
        self.assertIn(GapReason.SAFETY_REVIEW_PENDING, v2.reasons)
        self.assertNotIn(GapReason.STREET_CROSSING_BLOCKED, v2.reasons)

    def test_night_pharmacy_requires_night_hours(self) -> None:
        # 白班药店不能满足夜间购药（逐天登记营业窗口）
        day_hours = [
            {"start": f"{d} 08:00", "end": f"{d} 21:00"}
            for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        ]
        self.service.propose_site(
            "site-pharm-day", "A-4", ["pharmacy"], (0.1, 0.0), ["C1"],
            status="open", opening_hours=day_hours,
            approaches=[{
                "community_id": "C1", "distance_km": 0.1, "walk_minutes": 2,
                "barriers": [],
            }],
        )
        v = self.verdict("need-night-pharmacy")
        self.assertEqual(v.verdict, Verdict.GAP)
        self.assertIn(GapReason.CLOSED_AT_TIME, v.reasons)

        # 24 小时药店 + 安全通过后覆盖
        self.service.propose_site(
            "site-pharm-24", "A-5", ["pharmacy"], (0.15, 0.0), ["C1"],
            status="open",
            opening_hours=[{"start": "mon 00:00", "end": "sun 23:59"}],
            approaches=[{
                "community_id": "C1", "distance_km": 0.15, "walk_minutes": 3,
                "barriers": [],
            }],
        )
        self.service.review_coverage(
            "dec-pharm", "rev1", "need-night-pharmacy", "reviewer-X",
            site_id="site-pharm-24", safety_passed=True,
        )
        v2 = self.verdict("need-night-pharmacy")
        self.assertEqual(v2.verdict, Verdict.COVERED)
        self.assertEqual(v2.covered_by, "site-pharm-24")

    def test_suspended_site_is_not_supply(self) -> None:
        self.service.propose_site(
            "site-repair", "A-1", ["repair"], (0.2, 0.0), ["C1"],
            status="open",
            opening_hours=[{"start": "mon 08:00", "end": "sun 20:00"}],
            approaches=[{"community_id": "C1", "distance_km": 0.2, "barriers": []}],
        )
        self.service.review_coverage(
            "dec-repair", "rev1", "need-repair", "rev-X",
            site_id="site-repair", safety_passed=True,
        )
        self.assertEqual(self.verdict("need-repair").verdict, Verdict.COVERED)
        self.service.change_site_status("site-repair", "suspended")
        v = self.verdict("need-repair")
        self.assertEqual(v.verdict, Verdict.GAP)
        self.assertIn(GapReason.SUSPENDED, v.reasons)

    def test_cross_community_site_not_double_counted(self) -> None:
        # 一个网点跨 C1/C2 服务，容量只有 1：不能重复计作独立供给
        self.service.revise_community("rev2", "C2", 1, 0.5, center_km=(0.1, 0.0))
        self.service.submit_need(
            "need-repair-c2", "rev2", "repair",
            support_count=5, location_km=(0.1, 0.0),
        )
        self.service.propose_site(
            "site-shared", "A-9", ["repair"], (0.05, 0.0), ["C1", "C2"],
            capacity=1, status="open",
            opening_hours=[{"start": "mon 00:00", "end": "sun 23:59"}],
            approaches=[
                {"community_id": "C1", "distance_km": 0.05, "barriers": []},
                {"community_id": "C2", "distance_km": 0.05, "barriers": []},
            ],
        )
        self.service.review_coverage(
            "dec-shared-c1", "rev1", "need-repair", "rev-1",
            site_id="site-shared", safety_passed=True,
        )
        self.service.review_coverage(
            "dec-shared-c2", "rev2", "need-repair-c2", "rev-1",
            site_id="site-shared", safety_passed=True,
        )
        result = self.service.adjudicate()
        covered = {v.need_id: v.verdict for v in result.verdicts}
        # 老年需求优先，但两条维修需求中只有一条能占用唯一名额
        statuses = [covered["need-repair"], covered["need-repair-c2"]]
        self.assertEqual(statuses.count(Verdict.COVERED), 1)
        gap_verdict = next(
            v for nid, v in ((n, next(x for x in result.verdicts if x.need_id == n))
                             for n in ("need-repair", "need-repair-c2"))
            if v.verdict is Verdict.GAP
        )
        self.assertIn(GapReason.NO_DEDUP_CAPACITY, gap_verdict.reasons)


class GovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self._tmp.name))
        seed_basic_world(self.service)
        self.service.propose_site(
            "site-pharm", "applicant-7", ["pharmacy"], (0.1, 0.0), ["C1"],
            status="open",
            opening_hours=[{"start": "mon 00:00", "end": "sun 23:59"}],
            approaches=[{"community_id": "C1", "distance_km": 0.1, "barriers": []}],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_applicant_cannot_review_own_coverage(self) -> None:
        with self.assertRaises(ServiceError):
            self.service.review_coverage(
                "dec-self", "rev1", "need-night-pharmacy", "applicant-7",
                site_id="site-pharm", safety_passed=True,
            )

    def test_resident_opinion_cannot_set_safety_passed(self) -> None:
        # 居民联名推动复核
        for i in range(3):
            self.service.request_resident_review(
                "need-night-pharmacy", f"老年人夜间买药困难-{i}"
            )
        # 复核人未给安全结论：即使有 3 条意见，仍判缺口
        outcome = self.service.review_coverage(
            "dec-1", "rev1", "need-night-pharmacy", "reviewer-neutral",
            site_id="site-pharm", safety_passed=False,
        )
        self.assertFalse(outcome.safety_passed)
        self.assertTrue(outcome.requeued)
        self.assertEqual(outcome.result, "gap")

    def test_consent_withdrawal_keeps_stats_but_removes_identity(self) -> None:
        self.service.withdraw_consent("need-elderly")
        snap = self.service.snapshot()
        need = snap.needs["need-elderly"]
        self.assertFalse(need.public_consent)
        self.assertIsNone(need.resident_ref)  # 不可反向识别个人
        summary = self.service.public_summary()
        bucket = summary["communities"]["C1"]
        self.assertGreaterEqual(bucket["support_total"], 42)  # 支持度仍保留
        self.assertEqual(bucket["consent_withdrawn"], 1)
        # 汇总中不出现任何居民标识
        import json
        self.assertNotIn("R-1001", json.dumps(summary, ensure_ascii=False))

    def test_withdrawn_need_is_not_a_gap(self) -> None:
        self.service.withdraw_need("need-repair")
        result = self.service.adjudicate()
        v = next(x for x in result.verdicts if x.need_id == "need-repair")
        self.assertEqual(v.verdict, Verdict.WITHDRAWN)
        self.assertNotIn("need-repair", [g.need_id for g in result.gaps()])

    def test_boundary_revision_only_affects_unaccepted(self) -> None:
        self.service.review_coverage(
            "dec-acc", "rev1", "need-night-pharmacy", "rev-1",
            site_id="site-pharm", safety_passed=True,
        )
        self.assertTrue(
            self.service.submit_verification(
                "dec-acc", "city-inspector", ["photo-1", "hours-sign-1"], passed=True
            )
        )
        # 规划变更：rev1 -> rev3。已验收后仍按旧版本提报的方案受影响
        self.service.revise_community(
            "rev3", "C1", 2, 0.4, supersedes="rev1", boundary_ref="C1-边界-2026Q3"
        )
        self.service.submit_need(
            "need-stale", "rev1", "repair", location_km=(0.0, 0.0),
        )
        v_stale = next(
            x for x in self.service.adjudicate().verdicts if x.need_id == "need-stale"
        )
        self.assertEqual(v_stale.verdict, Verdict.GAP)
        self.assertIn(GapReason.SUPERSEDED_BOUNDARY, v_stale.reasons)

        # 已验收需求保留验收当时结论：即使网点事后停业也不改判
        v = next(
            x for x in self.service.adjudicate().verdicts
            if x.need_id == "need-night-pharmacy"
        )
        self.assertTrue(v.frozen)
        self.assertEqual(v.verdict, Verdict.COVERED)
        self.assertEqual(v.covered_by, "site-pharm")
        self.service.change_site_status("site-pharm", "suspended")
        v_after = next(
            x for x in self.service.adjudicate().verdicts
            if x.need_id == "need-night-pharmacy"
        )
        self.assertTrue(v_after.frozen)
        self.assertEqual(v_after.verdict, Verdict.COVERED)


class FundingAndQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self._tmp.name))
        self.service.register_funding_round("BATCH-2026-A", 100_000)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _site(self, site_id: str) -> None:
        self.service.revise_community("r1", "C9", 1, 1.0)
        self.service.propose_site(site_id, "A", ["repair"], (0.0, 0.0), ["C9"])

    def test_concurrent_commitments_cannot_exceed_budget(self) -> None:
        self.service.propose_site("s1", "A", ["repair"], (0.0, 0.0), ["C9"])
        self.service.propose_site("s2", "A", ["repair"], (0.0, 0.0), ["C9"])
        outcomes: list[bool] = []

        def reserve(site_id: str, amount: int) -> None:
            try:
                self.service.reserve_funding(site_id, "BATCH-2026-A", amount)
                outcomes.append(True)
            except FundingError:
                outcomes.append(False)

        threads = [
            threading.Thread(target=reserve, args=("s1", 60_000)),
            threading.Thread(target=reserve, args=("s2", 60_000)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(self.service.ledger.remaining("BATCH-2026-A"), 40_000)

    def test_ledger_recovers_after_restart(self) -> None:
        self._site("s1")
        self.service.reserve_funding("s1", "BATCH-2026-A", 30_000)
        # 模拟重启：新账本只登记预算，从事件流恢复承诺
        new_service = GapAdjudicationService(
            self.service.store, queue=self.service.queue
        )
        new_service.register_funding_round("BATCH-2026-A", 100_000)
        new_service.refresh_funding_from_events()
        self.assertEqual(new_service.ledger.remaining("BATCH-2026-A"), 70_000)

    def test_verification_queue_resumes_after_interruption(self) -> None:
        self.service.revise_community("r2", "C8", 1, 1.0)
        self.service.submit_need("n1", "r2", "repair")
        self.service.propose_site(
            "x1", "A", ["repair"], (0.1, 0.0), ["C8"], status="open",
            opening_hours=[{"start": "mon 00:00", "end": "sun 23:59"}],
            approaches=[{"community_id": "C8", "distance_km": 0.1, "barriers": []}],
        )
        for dec_id, need in (("d1", "n1"),):
            self.service.review_coverage(
                dec_id, "r2", need, "rev-9", site_id="x1", safety_passed=True
            )

        queue = self.service.rebuild_queue()
        self.assertEqual(queue.stats()["pending"], 1)
        claimed = queue.claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.decision_id, "d1")

        # 验收中断（证据不足，未通过）：回到队尾，尝试次数保留
        self.service.submit_verification(
            "d1", "insp", ["blurry-photo"], passed=False
        )
        queue2 = self.service.rebuild_queue()
        self.assertEqual(queue2.stats()["pending"], 1)

        # 模拟进程崩溃：侧车显示核查中，重开服务后任务自动回到队列
        item = queue2.claim()
        self.assertIsNotNone(item)
        restarted = GapAdjudicationService(self.service.store)
        restarted_queue = restarted.rebuild_queue() if restarted.queue else None
        self.assertIsNone(restarted_queue)  # 未配侧车时显式不可用
        from community_gap.queue import VerificationQueue
        recovered = VerificationQueue(self.service.queue._path).rebuild(
            fold(self.service.store.read_all())
        )
        self.assertEqual(recovered.stats()["pending"], 1)
        self.assertEqual(recovered.stats()["in_progress"], 0)

        # 补齐证据后通过，队列清空
        self.service.submit_verification(
            "d1", "insp", ["photo", "hours-sign", "gps"], passed=True
        )
        final_q = self.service.rebuild_queue()
        self.assertEqual(final_q.stats()["pending"], 0)

    def test_empty_evidence_rejected(self) -> None:
        self.service.revise_community("r3", "C7", 1, 1.0)
        self.service.submit_need("n7", "r3", "repair")
        self.service.propose_site(
            "x7", "A", ["repair"], (0.1, 0.0), ["C7"], status="open",
            opening_hours=[{"start": "mon 00:00", "end": "sun 23:59"}],
            approaches=[{"community_id": "C7", "distance_km": 0.1, "barriers": []}],
        )
        self.service.review_coverage(
            "d7", "r3", "n7", "rev", site_id="x7", safety_passed=True
        )
        with self.assertRaises(ServiceError):
            self.service.submit_verification("d7", "insp", [], passed=True)


class EventStoreRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_duplicate_event_is_idempotent_rejection(self) -> None:
        self.service.revise_community("rr", "Cx", 1, 0.5)
        # 直接用相同 event_id 再写一次
        store = self.service.store
        raw = store.read_all()[-1].to_dict()
        with self.assertRaises(DuplicateEventError):
            store.append(raw, expected_version=0)

    def test_version_conflict_detected(self) -> None:
        self.service.revise_community("rr2", "Cy", 1, 0.5)
        store = self.service.store
        raw = dict(store.read_all()[-1].to_dict(), event_id="other", version=5)
        with self.assertRaises(ConcurrencyError):
            store.append(raw, expected_version=0)


class ListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_final_listing_separates_three_statuses(self) -> None:
        self.service.revise_community("r", "Cz", 1, 1.0)
        self.service.propose_site("plan-1", "A", ["repair"], (0, 0), ["Cz"])
        self.service.propose_site(
            "open-1", "B", ["pharmacy"], (0, 0), ["Cz"], status="open",
        )
        self.service.propose_site(
            "susp-1", "C", ["elderly_care"], (0, 0), ["Cz"], status="suspended",
        )
        listing = self.service.adjudicate().final_listing()["Cz"]
        self.assertEqual([x["site_id"] for x in listing["planned"]], ["plan-1"])
        self.assertEqual([x["site_id"] for x in listing["open"]], ["open-1"])
        self.assertEqual([x["site_id"] for x in listing["suspended"]], ["susp-1"])


if __name__ == "__main__":
    unittest.main()

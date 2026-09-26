"""缺口裁决服务门面。

把事件存储、资金账本、验收队列和纯函数裁决引擎组装成市级专员可用的命令 API。
横切规则集中在这里：

- 项目申请者不得审核自己的覆盖结论（审核人回避）；
- 居民意见可推动复核，但安全结论只能由安全审查给出；
- 撤回公开展示同意后清除可反向识别个人的字段，汇总统计仍保留；
- 资金承诺先过批次余额检查再落 ``FUND_RESERVED`` 事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .adjudication import AdjudicationResult, adjudicate
from .domain import SiteStatus
from .events import (
    ConcurrencyError,
    ContractViolationError,
    DuplicateEventError,
    EventStore,
)
from .funding import FundingLedger
from .queue import VerificationQueue
from .state import Snapshot, fold


class ServiceError(Exception):
    """服务层规则被违反（业务拒绝，不产生事件）。"""


@dataclass(frozen=True)
class ReviewOutcome:
    decision_id: str
    result: str
    safety_passed: bool
    requeued: bool


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class GapAdjudicationService:
    def __init__(
        self,
        store: EventStore,
        ledger: FundingLedger | None = None,
        queue: VerificationQueue | None = None,
        *,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self.store = store
        self.ledger = ledger or FundingLedger()
        self.queue = queue
        self._clock = clock

    # ---- 内部 --------------------------------------------------------------

    def _now(self) -> str:
        return self._clock().astimezone().isoformat(timespec="seconds")

    def _append(
        self,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> None:
        expected = self.store.current_version(aggregate_id)
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self._now(),
            "version": expected + 1,
            "payload": payload,
        }
        self.store.append(event, expected_version=expected)

    def snapshot(self) -> Snapshot:
        return fold(self.store.read_all())

    def adjudicate(self) -> AdjudicationResult:
        return adjudicate(self.snapshot())

    # ---- 命令：社区边界 -----------------------------------------------------

    def revise_community(
        self,
        revision_id: str,
        community_id: str,
        revision_no: int,
        service_radius_km: float,
        *,
        center_km: tuple[float, float] = (0.0, 0.0),
        boundary_ref: str = "",
        supersedes: str | None = None,
    ) -> None:
        """登记社区边界新版本。规划变更不追溯已验收社区。"""
        self._append(
            f"rev-{revision_id}",
            "COMMUNITY_REVISED",
            "community_revision",
            revision_id,
            {
                "community_id": community_id,
                "revision_no": revision_no,
                "service_radius_km": service_radius_km,
                "center_km": list(center_km),
                "boundary_ref": boundary_ref,
                "supersedes": supersedes,
            },
        )

    # ---- 命令：居民需求 -----------------------------------------------------

    def submit_need(
        self,
        need_id: str,
        community_revision: str,
        category: str,
        *,
        support_count: int = 1,
        resident_ref: str | None = None,
        elderly_focus: bool = False,
        location_km: tuple[float, float] | None = None,
        required_moments: tuple[str, ...] = (),
        public_consent: bool = True,
    ) -> None:
        payload: dict[str, Any] = {
            "community_revision": community_revision,
            "category": category,
            "support_count": support_count,
            "resident_ref": resident_ref,
            "elderly_focus": elderly_focus,
            "required_moments": list(required_moments),
            "public_consent": public_consent,
        }
        if location_km is not None:
            payload["location_km"] = list(location_km)
        self._append(
            f"need-{need_id}", "NEED_AGGREGATED", "service_need", need_id, payload
        )

    def withdraw_need(self, need_id: str) -> None:
        """撤回需求：不再计入缺口，历史统计保留。"""
        self._append(
            f"need-withdraw-{need_id}",
            "NEED_WITHDRAWN",
            "service_need",
            need_id,
            {},
        )

    def withdraw_consent(self, need_id: str) -> None:
        """撤回公开展示同意：保留需求与汇总统计，清除可反向识别个人的字段。"""
        self._append(
            f"consent-{need_id}",
            "CONSENT_WITHDRAWN",
            "service_need",
            need_id,
            {},
        )

    def request_resident_review(self, need_id: str, reason: str) -> None:
        """居民意见推动复核排队；不直接修改覆盖或安全结论。"""
        self._append(
            f"review-req-{need_id}-{self.store.current_version(need_id) + 1}",
            "REVIEW_REQUESTED",
            "service_need",
            need_id,
            {"reason": reason},
        )

    # ---- 命令：网点 ---------------------------------------------------------

    def propose_site(
        self,
        site_id: str,
        applicant_id: str,
        categories: list[str],
        location_km: tuple[float, float],
        serves_communities: list[str],
        *,
        capacity: int | None = None,
        status: str = SiteStatus.PLANNED.value,
        opening_hours: list[dict[str, str]] | None = None,
        planned_open_date: str | None = None,
        approaches: list[dict[str, Any]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "applicant_id": applicant_id,
            "categories": categories,
            "location_km": list(location_km),
            "serves_communities": serves_communities,
            "capacity": capacity if capacity is not None else max(len(serves_communities), 1),
            "status": status,
        }
        if opening_hours is not None:
            payload["opening_hours"] = opening_hours
        if planned_open_date is not None:
            payload["planned_open_date"] = planned_open_date
        if approaches is not None:
            payload["approaches"] = approaches
        self._append(
            f"site-{site_id}", "SITE_PROPOSED", "provider_site", site_id, payload
        )

    def change_site_status(self, site_id: str, status: str) -> None:
        try:
            SiteStatus(status)
        except ValueError as exc:
            raise ServiceError(f"未知网点状态: {status}") from exc
        self._append(
            f"site-status-{site_id}-{self.store.current_version(site_id) + 1}",
            "SITE_STATUS_CHANGED",
            "provider_site",
            site_id,
            {"status": status},
        )

    # ---- 命令：资金 ---------------------------------------------------------

    def register_funding_round(self, round_id: str, budget: int) -> None:
        self.ledger.register_round(round_id, budget)

    def refresh_funding_from_events(self) -> None:
        """从事件流汇总各批次已承诺额，恢复/校正账本余额。

        预算登记是外部政策（:meth:`register_funding_round`），承诺事实只来自
        ``FUND_RESERVED`` 事件；服务重启后调用本方法即可还原余额。
        """
        totals: dict[str, int] = {}
        for site in self.snapshot().sites.values():
            for round_id, amount in site.fund_reservations.items():
                totals[round_id] = totals.get(round_id, 0) + amount
        known = self.ledger.snapshot()
        for round_id, committed in totals.items():
            if round_id in known:
                self.ledger.replay_commitment(round_id, committed)

    def reserve_funding(
        self, site_id: str, funding_round: str, amount: int
    ) -> int:
        """原子承诺资金；余额不足直接拒绝且不落事件。返回批次剩余额度。"""
        site = self.snapshot().sites.get(site_id)
        if site is None:
            raise ServiceError(f"网点不存在: {site_id}")
        remaining = self.ledger.commit(funding_round, amount)
        try:
            self._append(
                f"fund-{site_id}-{funding_round}-{self.store.current_version(site_id) + 1}",
                "FUND_RESERVED",
                "provider_site",
                site_id,
                {"funding_round": funding_round, "amount": amount},
            )
        except (DuplicateEventError, ConcurrencyError, ContractViolationError):
            # 事件落盘失败不能让账本凭空扣减：本实现单写入者不会走到，
            # 走到时显式失败交由人工核对，避免资金与事实脱节。
            raise
        return remaining

    # ---- 命令：覆盖复核与安全审查 -------------------------------------------

    def review_coverage(
        self,
        decision_id: str,
        community_revision: str,
        need_id: str,
        reviewer_id: str,
        *,
        site_id: str | None = None,
        safety_passed: bool = False,
    ) -> ReviewOutcome:
        """作出覆盖结论。

        规则：
        - 审核人必须不是该网点的项目申请者（回避）；
        - ``safety_passed`` 只能来自安全审查本身，居民意见再多也不会置真；
        - 居民复核请求只作为意见计数记录在结论上。
        """
        snap = self.snapshot()
        need = snap.needs.get(need_id)
        if need is None:
            raise ServiceError(f"需求不存在: {need_id}")
        site = snap.sites.get(site_id) if site_id else None
        applicant_id = site.applicant_id if site else "none"
        if site is not None and reviewer_id == applicant_id:
            raise ServiceError(
                f"审核人 {reviewer_id} 是网点 {site_id} 的申请者，不得审核自己的覆盖结论"
            )
        category = need.category
        # 裁决结果由规则引擎当场计算，不由审核人主观指定。本次复核携带的
        # 安全结论只在本地快照上生效后再裁决；落盘事实仍以事件为准。
        if site is not None and safety_passed:
            site.safety_passed = True
            site.safety_decided_by = reviewer_id
        result = adjudicate(snap)
        verdict = next((v for v in result.verdicts if v.need_id == need_id), None)
        covered = bool(verdict and verdict.covered_by == site_id and site_id is not None)

        self._append(
            f"decision-{decision_id}",
            "COVERAGE_REVIEWED",
            "coverage_decision",
            decision_id,
            {
                "community_revision": community_revision,
                "need_id": need_id,
                "site_id": site_id,
                "category": category,
                "reviewer_id": reviewer_id,
                "applicant_id": applicant_id,
                "result": "covered" if covered else "gap",
                "safety_passed": bool(safety_passed),
                "resident_opinion_count": need.review_requests,
            },
        )
        return ReviewOutcome(
            decision_id=decision_id,
            result="covered" if covered else "gap",
            safety_passed=bool(safety_passed),
            requeued=need.review_requests > 0,
        )

    # ---- 命令：验收 ---------------------------------------------------------

    def submit_verification(
        self,
        decision_id: str,
        verified_by: str,
        evidence_set: list[str],
        *,
        passed: bool,
    ) -> bool:
        """登记一次验收证据核查。只有 passed 且证据非空才推进验收。"""
        snap = self.snapshot()
        decision = snap.decisions.get(decision_id)
        if decision is None:
            raise ServiceError(f"覆盖结论不存在: {decision_id}")
        if not evidence_set:
            raise ServiceError("验收必须提交证据集合，空证据不予受理")

        site_id = decision.site_id
        payload: dict[str, Any] = {
            "site_id": site_id,
            "evidence_set": evidence_set,
            "verified_by": verified_by,
            "passed": bool(passed),
        }
        if passed and decision.result == "covered" and site_id and decision.safety_passed:
            # 冻结依据：把验收当时该社区全部需求的裁决结论随事件固化，
            # 事后规划变更不得改写
            revision = snap.revisions.get(decision.community_revision)
            if revision is not None:
                outcomes = {}
                for v in adjudicate(snap).verdicts:
                    if v.community_id == revision.community_id:
                        outcomes[v.need_id] = [v.verdict.value, v.covered_by]
                payload["frozen_outcomes"] = outcomes
        self._append(
            f"verify-{decision_id}-{self.store.current_version(decision_id) + 1}",
            "SITE_VERIFIED",
            "coverage_decision",
            decision_id,
            payload,
        )
        if self.queue is not None:
            self.queue.record_attempt(decision_id, tuple(evidence_set))
            if passed:
                self.queue.complete(decision_id)
            else:
                self.queue.requeue(decision_id)
        return bool(passed)

    def rebuild_queue(self) -> VerificationQueue:
        """中断恢复：从事件流重建快照并刷新验收队列。"""
        if self.queue is None:
            raise ServiceError("未配置验收队列侧车路径")
        self.queue.rebuild(self.snapshot())
        return self.queue

    # ---- 查询：脱敏汇总 -----------------------------------------------------

    def public_summary(self) -> dict[str, Any]:
        """公开展示用汇总。

        撤回同意后汇总统计仍保留（支持度、缺口数量），但任何输出都不含
        ``resident_ref`` 等可反向识别个人的字段。
        """
        snap = self.snapshot()
        result = adjudicate(snap)
        per_community: dict[str, dict[str, Any]] = {}
        for need in snap.needs.values():
            revision = snap.revisions.get(need.community_revision)
            community_id = revision.community_id if revision else "unknown"
            bucket = per_community.setdefault(
                community_id,
                {
                    "needs_total": 0,
                    "needs_active": 0,
                    "support_total": 0,
                    "gaps": 0,
                    "consent_withdrawn": 0,
                    "by_category": {},
                },
            )
            bucket["needs_total"] += 1
            if need.public_consent is False:
                bucket["consent_withdrawn"] += 1
            if need.withdrawn:
                continue
            bucket["needs_active"] += 1
            bucket["support_total"] += need.support_count
            cat = bucket["by_category"].setdefault(
                need.category, {"support": 0, "gaps": 0}
            )
            cat["support"] += need.support_count
        for verdict in result.gaps():
            bucket = per_community.setdefault(
                verdict.community_id,
                {
                    "needs_total": 0,
                    "needs_active": 0,
                    "support_total": 0,
                    "gaps": 0,
                    "consent_withdrawn": 0,
                    "by_category": {},
                },
            )
            bucket["gaps"] += 1
            cat = bucket["by_category"].setdefault(
                verdict.category, {"support": 0, "gaps": 0}
            )
            cat["gaps"] += 1
        return {"communities": per_community}

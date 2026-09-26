"""缺口裁决服务：在领域契约之上实现业务规则。

规则要点：
- 居民意见只推动复核，覆盖结论必须由审核人完成安全审查后给出；
- 项目申请者不得审核涉及自己网点的覆盖结论，也不得自行验收网点；
- 规划变更只影响尚未验收的方案，已验收社区保留当时边界与服务依据；
- 跨社区网点按主供网点扣减共享容量，不得重复计作独立供给；
- 改造资金承诺在批次余额内串行扣减，并发不得超支；
- 撤回公开展示同意后汇总统计保留，公开视图只做匿名汇总；
- 核查任务入持久化队列，验收中断后可恢复。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_event
from .errors import DomainError
from .models import (
    OUTCOME_LABELS,
    SITE_STATUSES,
    STATUS_LABELS,
    TIME_WINDOW_HOURS,
    Assessment,
    Barrier,
    CommunityRevision,
    CoverageDecision,
    FundingRound,
    ProviderSite,
    Reason,
    ServiceNeed,
)
from .queue import VerificationQueue
from .store import EventStore, Receipt

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def need_key(community_id: str, category: str) -> str:
    return f"need:{community_id}:{category}"


def decision_key(community_id: str, category: str) -> str:
    return f"decision:{community_id}:{category}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_minutes(hhmm: str) -> int:
    parts = str(hhmm).split(":")
    if len(parts) != 2:
        raise DomainError("INVALID_TIME", "时间格式应为 HH:MM", {"value": hhmm})
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        raise DomainError("INVALID_TIME", "时间格式应为 HH:MM", {"value": hhmm}) from None
    if not (0 <= hours <= 24 and 0 <= minutes < 60 and (hours < 24 or minutes == 0)):
        raise DomainError("INVALID_TIME", "时间超出 00:00-24:00 范围", {"value": hhmm})
    return hours * 60 + minutes


def _covers_window(opens_at: str, closes_at: str, window: str) -> bool:
    """网点营业时段是否完整覆盖需求时段；支持跨零点营业。"""
    start = _to_minutes(opens_at)
    end = _to_minutes(closes_at)
    if end <= start:
        end += 24 * 60
    win_start, win_end = TIME_WINDOW_HOURS[window]
    return start <= win_start and end >= win_end


class GapAdjudicationService:
    """缺口裁决服务：事件摄入、状态投影与裁决查询的一体化入口。"""

    def __init__(
        self,
        *,
        queue_path: str | None = None,
        support_threshold: int = 10,
        anonymity_threshold: int = 5,
        schema_path: str | Path | None = None,
    ) -> None:
        self.support_threshold = support_threshold
        self.anonymity_threshold = anonymity_threshold
        schema_file = Path(schema_path) if schema_path else DEFAULT_SCHEMA_PATH
        self.schema = json.loads(schema_file.read_text(encoding="utf-8"))
        self.store = EventStore()
        self.queue = VerificationQueue(queue_path)
        self._lock = threading.RLock()
        self.revisions: dict[str, CommunityRevision] = {}
        self.needs: dict[str, ServiceNeed] = {}
        self.sites: dict[str, ProviderSite] = {}
        self.decisions: dict[str, CoverageDecision] = {}
        self.funding_rounds: dict[str, FundingRound] = {}

    # ------------------------------------------------------------------
    # 事件摄入
    # ------------------------------------------------------------------

    def ingest(self, event: Mapping[str, Any]) -> Receipt:
        """校验契约后追加事件并推进投影；相同 event_id 幂等。"""
        issues = validate_event(event, self.schema)
        if issues:
            raise DomainError(
                "CONTRACT_VIOLATION",
                "事件未通过契约校验",
                [{"field": i.field, "code": i.code, "message": i.message} for i in issues],
            )
        with self._lock:
            receipt = self.store.append(event)
            if receipt.applied:
                self._project(event)
                self._side_effects(event)
            return receipt

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> Receipt:
        event = {
            "event_id": event_id or f"evt-{uuid.uuid4().hex[:12]}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at or _now(),
            "version": self.store.next_version(aggregate_type, aggregate_id),
            "payload": payload,
        }
        return self.ingest(event)

    # ------------------------------------------------------------------
    # 命令：社区边界与居民需求
    # ------------------------------------------------------------------

    def publish_boundary(
        self,
        community_id: str,
        revision: int,
        zones: list[str],
        barriers: list[dict[str, Any]] | None = None,
        **kw: Any,
    ) -> Receipt:
        """发布社区边界版本；版本号必须从 1 开始逐次递增。"""
        with self._lock:
            current = self.revisions.get(community_id)
            expected = (current.revision + 1) if current else 1
            if revision != expected:
                raise DomainError(
                    "REVISION_SEQUENCE",
                    "边界版本必须逐次递增",
                    {"community_id": community_id, "expected": expected, "got": revision},
                )
            payload = {
                "community_id": community_id,
                "revision": revision,
                "zones": list(zones),
                "barriers": list(barriers or []),
            }
            return self._emit("BOUNDARY_PUBLISHED", "community_revision", community_id, payload, **kw)

    def aggregate_need(
        self,
        community_id: str,
        category: str,
        *,
        zone: str,
        time_window: str,
        support_total: int,
        vulnerable_group: str = "general",
        **kw: Any,
    ) -> Receipt:
        """登记/更新居民需求及匿名支持度；超过阈值会推动复核。"""
        with self._lock:
            revision = self.revisions.get(community_id)
            if revision is None:
                raise DomainError("NO_BOUNDARY", "社区尚未发布边界版本", {"community_id": community_id})
            if time_window not in TIME_WINDOW_HOURS:
                raise DomainError("INVALID_TIME_WINDOW", "需求时段未登记", {"time_window": time_window})
            if isinstance(support_total, bool) or not isinstance(support_total, int) or support_total < 0:
                raise DomainError("INVALID_SUPPORT", "匿名支持度必须是非负整数", {"support_total": support_total})
            payload = {
                "community_id": community_id,
                "community_revision": revision.revision,
                "category": category,
                "zone": zone,
                "time_window": time_window,
                "vulnerable_group": vulnerable_group,
                "support_total": support_total,
            }
            return self._emit("NEED_AGGREGATED", "service_need", need_key(community_id, category), payload, **kw)

    def withdraw_consent(self, community_id: str, category: str, resident_ref: str, **kw: Any) -> Receipt:
        """居民撤回公开展示同意；汇总统计保留，公开视图不反向识别个人。"""
        with self._lock:
            key = need_key(community_id, category)
            if key not in self.needs:
                raise DomainError("UNKNOWN_NEED", "未找到对应的需求记录", {"community_id": community_id, "category": category})
            return self._emit("CONSENT_WITHDRAWN", "service_need", key, {"resident_ref": resident_ref}, **kw)

    # ------------------------------------------------------------------
    # 命令：网点生命周期
    # ------------------------------------------------------------------

    def propose_site(
        self,
        site_id: str,
        *,
        applicant_id: str,
        categories: list[str],
        community_id: str,
        zone: str,
        service_area: dict[str, list[str] | None],
        capacity: int,
        opens_at: str,
        closes_at: str,
        **kw: Any,
    ) -> Receipt:
        """提交开店意向；重复提交视为更新意向，保留营业与验收状态。"""
        with self._lock:
            if not categories:
                raise DomainError("INVALID_SITE", "服务类别不能为空")
            if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
                raise DomainError("INVALID_SITE", "服务能力必须是非负整数", {"capacity": capacity})
            _to_minutes(opens_at)
            _to_minutes(closes_at)
            payload = {
                "applicant_id": applicant_id,
                "categories": list(categories),
                "community_id": community_id,
                "zone": zone,
                "service_area": {k: (list(v) if v is not None else None) for k, v in service_area.items()},
                "capacity": capacity,
                "opens_at": opens_at,
                "closes_at": closes_at,
            }
            return self._emit("SITE_PROPOSED", "provider_site", site_id, payload, **kw)

    def change_site_status(self, site_id: str, status: str, **kw: Any) -> Receipt:
        """切换营业状态：planned/open/suspended。"""
        with self._lock:
            if site_id not in self.sites:
                raise DomainError("UNKNOWN_SITE", "未找到网点", {"site_id": site_id})
            if status not in SITE_STATUSES:
                raise DomainError("INVALID_STATUS", "网点状态未登记", {"status": status})
            return self._emit("SITE_STATUS_CHANGED", "provider_site", site_id, {"status": status}, **kw)

    def verify_site(self, site_id: str, evidence_set: list[str], verified_by: str, **kw: Any) -> Receipt:
        """网点验收：验收人不得是申请者本人。"""
        with self._lock:
            site = self.sites.get(site_id)
            if site is None:
                raise DomainError("UNKNOWN_SITE", "未找到网点", {"site_id": site_id})
            if not evidence_set:
                raise DomainError("EVIDENCE_REQUIRED", "验收证据不能为空")
            if verified_by == site.applicant_id:
                raise DomainError("SELF_REVIEW_FORBIDDEN", "网点申请者不得自行验收")
            payload = {"evidence_set": list(evidence_set), "verified_by": verified_by}
            return self._emit("SITE_VERIFIED", "provider_site", site_id, payload, **kw)

    # ------------------------------------------------------------------
    # 命令：覆盖结论与验收
    # ------------------------------------------------------------------

    def review_coverage(
        self,
        community_id: str,
        category: str,
        *,
        outcome: str,
        reviewer: str,
        safety_review_passed: bool,
        **kw: Any,
    ) -> Receipt:
        """审核覆盖结论。

        - 涉及自有网点的申请者不得审核；
        - 记为已覆盖必须通过安全审查，居民意见不能替代；
        - 记为已覆盖还要求系统核查（可达、营业、验收、容量）通过。
        """
        with self._lock:
            revision = self.revisions.get(community_id)
            if revision is None:
                raise DomainError("NO_BOUNDARY", "社区尚未发布边界版本", {"community_id": community_id})
            if outcome not in ("gap", "covered"):
                raise DomainError("INVALID_OUTCOME", "覆盖结论只能是 gap 或 covered", {"outcome": outcome})
            involved_applicants = {
                site.applicant_id
                for site in self.sites.values()
                if category in site.categories and community_id in site.service_area
            }
            if reviewer in involved_applicants:
                raise DomainError("SELF_REVIEW_FORBIDDEN", "项目申请者不得审核自己的覆盖结论")
            assessment = self.assess(community_id, category)
            counted: list[str] = []
            primary = None
            demand = 0
            if outcome == "covered":
                if not safety_review_passed:
                    raise DomainError("SAFETY_REVIEW_REQUIRED", "居民意见不能替代安全审查")
                if assessment.outcome != "covered":
                    raise DomainError(
                        "ASSESSMENT_NOT_SATISFIED",
                        "系统核查未通过，不能记为已覆盖",
                        {"reasons": [r.to_dict() for r in assessment.reasons]},
                    )
                counted = assessment.qualifying_site_ids
                primary = assessment.primary_site_id
                demand = int(assessment.demand.get("support_total", 0))
            payload = {
                "community_id": community_id,
                "community_revision": revision.revision,
                "category": category,
                "outcome": outcome,
                "reviewer": reviewer,
                "safety_review_passed": bool(safety_review_passed),
                "counted_site_ids": counted,
                "primary_site_id": primary,
                "demand": demand,
            }
            return self._emit("COVERAGE_REVIEWED", "coverage_decision", decision_key(community_id, category), payload, **kw)

    def verify_decision(self, decision_id: str, evidence_set: list[str], verified_by: str, **kw: Any) -> Receipt:
        """验收覆盖方案；过期（待复核）的方案必须先复核。"""
        with self._lock:
            decision = self.decisions.get(decision_id)
            if decision is None:
                raise DomainError("UNKNOWN_DECISION", "未找到覆盖结论", {"decision_id": decision_id})
            if decision.stale:
                raise DomainError("DECISION_STALE", "规划已变更，方案需先复核再验收", {"decision_id": decision_id})
            if not evidence_set:
                raise DomainError("EVIDENCE_REQUIRED", "验收证据不能为空")
            payload = {"evidence_set": list(evidence_set), "verified_by": verified_by}
            return self._emit("DECISION_VERIFIED", "coverage_decision", decision_id, payload, **kw)

    # ------------------------------------------------------------------
    # 命令：改造资金
    # ------------------------------------------------------------------

    def open_funding_round(self, round_id: str, total_budget: int, label: str = "", **kw: Any) -> Receipt:
        with self._lock:
            if isinstance(total_budget, bool) or not isinstance(total_budget, int) or total_budget <= 0:
                raise DomainError("INVALID_BUDGET", "批次预算必须是正整数", {"total_budget": total_budget})
            payload = {"total_budget": total_budget, "label": label}
            return self._emit("FUND_ROUND_OPENED", "funding_round", round_id, payload, **kw)

    def reserve_funds(self, round_id: str, amount: int, site_id: str | None = None, **kw: Any) -> Receipt:
        """承诺改造资金；并发承诺串行化，不得超出批次余额。"""
        with self._lock:
            funding = self.funding_rounds.get(round_id)
            if funding is None:
                raise DomainError("UNKNOWN_FUNDING_ROUND", "未找到资金批次", {"round_id": round_id})
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise DomainError("INVALID_AMOUNT", "承诺金额必须是正整数", {"amount": amount})
            if funding.reserved + amount > funding.total_budget:
                raise DomainError(
                    "INSUFFICIENT_FUNDS",
                    "改造资金批次余额不足",
                    {"round_id": round_id, "remaining": funding.remaining, "requested": amount},
                )
            payload: dict[str, Any] = {"funding_round": round_id, "amount": amount}
            if site_id is not None:
                payload["site_id"] = site_id
            return self._emit("FUND_RESERVED", "funding_round", round_id, payload, **kw)

    # ------------------------------------------------------------------
    # 投影
    # ------------------------------------------------------------------

    def _project(self, event: Mapping[str, Any]) -> None:
        event_type = event["event_type"]
        payload = event["payload"]
        aggregate_id = event["aggregate_id"]
        if event_type == "BOUNDARY_PUBLISHED":
            self.revisions[payload["community_id"]] = CommunityRevision(
                community_id=payload["community_id"],
                revision=payload["revision"],
                zones=list(payload["zones"]),
                barriers=[
                    Barrier(
                        barrier_id=b["barrier_id"],
                        zones=tuple(b["zones"]),
                        blocks=tuple(b.get("blocks", ["all"])),
                        note=b.get("note", ""),
                    )
                    for b in payload.get("barriers", [])
                ],
                published_at=event["occurred_at"],
            )
        elif event_type == "NEED_AGGREGATED":
            existing = self.needs.get(aggregate_id)
            self.needs[aggregate_id] = ServiceNeed(
                need_id=aggregate_id,
                community_id=payload["community_id"],
                category=payload["category"],
                zone=payload["zone"],
                time_window=payload["time_window"],
                support_total=payload["support_total"],
                revision=payload["community_revision"],
                vulnerable_group=payload.get("vulnerable_group", "general"),
                withdrawn_refs=set(existing.withdrawn_refs) if existing else set(),
            )
        elif event_type == "CONSENT_WITHDRAWN":
            need = self.needs.get(aggregate_id)
            if need is not None:
                need.withdrawn_refs.add(payload["resident_ref"])
        elif event_type == "SITE_PROPOSED":
            existing = self.sites.get(aggregate_id)
            self.sites[aggregate_id] = ProviderSite(
                site_id=aggregate_id,
                applicant_id=payload["applicant_id"],
                categories=list(payload["categories"]),
                community_id=payload["community_id"],
                zone=payload["zone"],
                service_area={k: (list(v) if v is not None else None) for k, v in payload["service_area"].items()},
                capacity=payload["capacity"],
                opens_at=payload["opens_at"],
                closes_at=payload["closes_at"],
                status=existing.status if existing else "planned",
                verified=existing.verified if existing else False,
                evidence_set=list(existing.evidence_set) if existing else [],
                verified_by=existing.verified_by if existing else None,
            )
        elif event_type == "SITE_STATUS_CHANGED":
            site = self.sites.get(aggregate_id)
            if site is not None:
                site.status = payload["status"]
        elif event_type == "SITE_VERIFIED":
            site = self.sites.get(aggregate_id)
            if site is not None:
                site.verified = True
                site.evidence_set = list(payload["evidence_set"])
                site.verified_by = payload["verified_by"]
        elif event_type == "COVERAGE_REVIEWED":
            self.decisions[aggregate_id] = CoverageDecision(
                decision_id=aggregate_id,
                community_id=payload["community_id"],
                category=payload["category"],
                revision=payload["community_revision"],
                outcome=payload["outcome"],
                reviewer=payload["reviewer"],
                safety_review_passed=payload["safety_review_passed"],
                counted_site_ids=list(payload.get("counted_site_ids", [])),
                primary_site_id=payload.get("primary_site_id"),
                demand=payload.get("demand", 0),
            )
        elif event_type == "DECISION_VERIFIED":
            decision = self.decisions.get(aggregate_id)
            if decision is not None:
                decision.verified = True
                decision.verified_by = payload["verified_by"]
                decision.evidence_set = list(payload["evidence_set"])
        elif event_type == "FUND_ROUND_OPENED":
            self.funding_rounds[aggregate_id] = FundingRound(
                round_id=aggregate_id,
                total_budget=payload["total_budget"],
                label=payload.get("label", ""),
            )
        elif event_type == "FUND_RESERVED":
            funding = self.funding_rounds.get(payload["funding_round"])
            if funding is not None:
                funding.reserved += payload["amount"]

    # ------------------------------------------------------------------
    # 副作用：推动复核与核查队列
    # ------------------------------------------------------------------

    def _flag_review(self, community_id: str, category: str) -> None:
        decision = self.decisions.get(decision_key(community_id, category))
        if decision is not None and not decision.stale:
            decision.review_pending = True
        self.queue.enqueue("coverage_review", community_id, category)

    def _side_effects(self, event: Mapping[str, Any]) -> None:
        event_type = event["event_type"]
        payload = event["payload"]
        if event_type == "NEED_AGGREGATED":
            # 居民意见达到阈值：推动复核，但不直接改写结论
            if payload["support_total"] >= self.support_threshold:
                self._flag_review(payload["community_id"], payload["category"])
        elif event_type == "BOUNDARY_PUBLISHED":
            community_id = payload["community_id"]
            # 规划变更只影响尚未验收的方案；已验收的保留当时依据
            for decision in self.decisions.values():
                if decision.community_id == community_id and not decision.verified:
                    decision.stale = True
                    decision.review_pending = False
            pairs = {n.category for n in self.needs.values() if n.community_id == community_id}
            pairs |= {d.category for d in self.decisions.values() if d.community_id == community_id}
            for category in sorted(pairs):
                self.queue.enqueue("coverage_review", community_id, category)
        elif event_type in ("SITE_STATUS_CHANGED", "SITE_VERIFIED"):
            site = self.sites.get(event["aggregate_id"])
            if site is not None:
                for need in self.needs.values():
                    if need.category in site.categories and need.community_id in site.service_area:
                        self._flag_review(need.community_id, need.category)

    # ------------------------------------------------------------------
    # 裁决
    # ------------------------------------------------------------------

    def _reserved_capacity(self, site_id: str) -> int:
        """网点已被有效覆盖结论承诺的共享容量。"""
        return sum(
            d.demand
            for d in self.decisions.values()
            if not d.stale and d.outcome == "covered" and d.primary_site_id == site_id
        )

    def _site_block_reason(
        self,
        site: ProviderSite,
        need: ServiceNeed,
        revision: CommunityRevision,
    ) -> dict[str, Any] | None:
        if site.status == "planned":
            return {"code": "SITE_NOT_OPEN", "message": "网点尚未营业（计划中）"}
        if site.status == "suspended":
            return {"code": "SITE_SUSPENDED", "message": "网点暂时停业"}
        if not site.verified:
            return {"code": "SITE_UNVERIFIED", "message": "网点未通过验收"}
        zones = site.service_area.get(need.community_id)
        if zones is not None and need.zone not in zones:
            return {"code": "ZONE_NOT_SERVED", "message": "网点服务范围未覆盖该片区"}
        if site.community_id == need.community_id and site.zone != need.zone:
            for barrier in revision.barriers:
                if set(barrier.zones) == {need.zone, site.zone} and barrier.blocks_group(need.vulnerable_group):
                    return {
                        "code": "WALK_BARRIER",
                        "message": "步行路径受阻",
                        "details": {"barrier_id": barrier.barrier_id, "note": barrier.note},
                    }
        if not _covers_window(site.opens_at, site.closes_at, need.time_window):
            return {
                "code": "HOURS_MISMATCH",
                "message": "营业时段不覆盖需求时段",
                "details": {"opens_at": site.opens_at, "closes_at": site.closes_at, "time_window": need.time_window},
            }
        return None

    def _pick_primary(self, qualifying: list[str]) -> tuple[str, int]:
        """主供网点：剩余共享容量最大，并列时取标识较小者。"""
        remaining = {sid: self.sites[sid].capacity - self._reserved_capacity(sid) for sid in qualifying}
        primary = min(qualifying, key=lambda sid: (-remaining[sid], sid))
        return primary, remaining[primary]

    def assess(self, community_id: str, category: str) -> Assessment:
        """按最新边界版本裁决某社区某类别是否仍为缺口，并给出原因。"""
        with self._lock:
            revision = self.revisions.get(community_id)
            if revision is None:
                return Assessment(
                    community_id=community_id,
                    category=category,
                    revision=None,
                    outcome="undecided",
                    reasons=[Reason("NO_BOUNDARY", "社区尚未发布边界版本，无法裁决")],
                )
            need = self.needs.get(need_key(community_id, category))
            if need is None:
                return Assessment(
                    community_id=community_id,
                    category=category,
                    revision=revision.revision,
                    outcome="no_demand",
                    reasons=[Reason("NO_NEED_RECORD", "该类别暂无居民需求记录")],
                )
            demand = {
                "support_total": need.support_total,
                "threshold": self.support_threshold,
                "met": need.support_total >= self.support_threshold,
            }
            if not demand["met"]:
                return Assessment(
                    community_id=community_id,
                    category=category,
                    revision=revision.revision,
                    outcome="observation",
                    reasons=[
                        Reason(
                            "SUPPORT_BELOW_THRESHOLD",
                            "匿名支持度未达阈值，继续观察",
                            {"support_total": need.support_total, "threshold": self.support_threshold},
                        )
                    ],
                    demand=demand,
                )
            candidates = sorted(
                (
                    site
                    for site in self.sites.values()
                    if category in site.categories and community_id in site.service_area
                ),
                key=lambda site: site.site_id,
            )
            reasons: list[Reason] = []
            excluded: list[dict[str, Any]] = []
            qualifying: list[str] = []
            if not candidates:
                reasons.append(Reason("NO_SITE_IN_RADIUS", "服务半径内无候选网点", {"category": category}))
            for site in candidates:
                block = self._site_block_reason(site, need, revision)
                if block is None:
                    qualifying.append(site.site_id)
                else:
                    excluded.append({"site_id": site.site_id, **block})
                    reasons.append(Reason(block["code"], block["message"], {"site_id": site.site_id, **block.get("details", {})}))
            if not qualifying:
                return Assessment(
                    community_id=community_id,
                    category=category,
                    revision=revision.revision,
                    outcome="gap",
                    reasons=reasons,
                    demand=demand,
                    excluded_supply=excluded,
                )
            primary, remaining = self._pick_primary(qualifying)
            if remaining >= need.support_total:
                return Assessment(
                    community_id=community_id,
                    category=category,
                    revision=revision.revision,
                    outcome="covered",
                    reasons=[Reason("COVERED_BY", "由网点覆盖", {"primary_site_id": primary, "sites": qualifying})],
                    demand=demand,
                    qualifying_site_ids=qualifying,
                    excluded_supply=excluded,
                    primary_site_id=primary,
                )
            reasons.append(
                Reason(
                    "SHARED_CAPACITY_EXHAUSTED",
                    "跨社区共享网点容量不足",
                    {"primary_site_id": primary, "demand": need.support_total, "remaining": remaining},
                )
            )
            return Assessment(
                community_id=community_id,
                category=category,
                revision=revision.revision,
                outcome="gap",
                reasons=reasons,
                demand=demand,
                qualifying_site_ids=qualifying,
                excluded_supply=excluded,
                primary_site_id=primary,
            )

    # ------------------------------------------------------------------
    # 查询：解释、隐私视图与清单
    # ------------------------------------------------------------------

    def explain_gap(self, community_id: str, category: str) -> dict[str, Any]:
        """解释某片区当前结论：新鲜裁决 + 生效决策 + 队列状态。"""
        with self._lock:
            assessment = self.assess(community_id, category)
            decision = self.decisions.get(decision_key(community_id, category))
            return {
                "community_id": community_id,
                "category": category,
                "assessment": assessment.to_dict(),
                "decision": None
                if decision is None
                else {
                    "outcome": decision.outcome,
                    "outcome_label": OUTCOME_LABELS.get(decision.outcome, decision.outcome),
                    "revision": decision.revision,
                    "reviewer": decision.reviewer,
                    "verified": decision.verified,
                    "stale": decision.stale,
                    "review_pending": decision.review_pending,
                    "counted_site_ids": list(decision.counted_site_ids),
                    "demand": decision.demand,
                },
                "queued": self.queue.has_open(VerificationQueue.task_key("coverage_review", community_id, category)),
            }

    def public_need_view(self, community_id: str, category: str) -> dict[str, Any]:
        """公开视图：只给匿名汇总，低于阈值做抑制，绝不暴露个人标识。"""
        with self._lock:
            need = self.needs.get(need_key(community_id, category))
            if need is None:
                raise DomainError("UNKNOWN_NEED", "未找到对应的需求记录", {"community_id": community_id, "category": category})
            suppressed = need.support_total < self.anonymity_threshold
            return {
                "community_id": community_id,
                "category": category,
                "support": None if suppressed else need.support_total,
                "suppressed": suppressed,
                "aggregate_only": True,
            }

    def service_directory(self, community_id: str | None = None) -> list[dict[str, Any]]:
        """最终清单：区分计划中、已开放、暂时停业的服务。"""
        with self._lock:
            rows = []
            for site in sorted(self.sites.values(), key=lambda s: s.site_id):
                if community_id is not None and community_id not in site.service_area:
                    continue
                rows.append(
                    {
                        "site_id": site.site_id,
                        "categories": list(site.categories),
                        "status": site.status,
                        "status_label": STATUS_LABELS[site.status],
                        "verified": site.verified,
                        "serves": sorted(site.service_area),
                        "capacity": site.capacity,
                    }
                )
            return rows

    def supply_summary(self) -> dict[str, Any]:
        """供给汇总：跨社区网点只计一次，不重复计作独立供给。"""
        with self._lock:
            sites = list(self.sites.values())
            return {
                "total_sites": len(sites),
                "by_status": {status: sum(1 for s in sites if s.status == status) for status in SITE_STATUSES},
                "open_verified_sites": sum(1 for s in sites if s.status == "open" and s.verified),
                "total_capacity": sum(s.capacity for s in sites),
                "communities_served": sorted({c for s in sites for c in s.service_area}),
                "covered_pairs": [
                    {"community_id": d.community_id, "category": d.category}
                    for d in sorted(self.decisions.values(), key=lambda d: d.decision_id)
                    if not d.stale and d.outcome == "covered"
                ],
            }

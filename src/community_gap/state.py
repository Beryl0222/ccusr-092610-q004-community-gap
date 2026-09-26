"""状态回放：把只追加事件流折叠成当前快照。

所有裁决所需事实都从事件派生，回放是确定性的——同一事件流必然得到同一快照，
验收队列中断后可以随时从日志重建。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .domain import (
    SiteStatus,
    parse_opening_hours,
)
from .events import StoredEvent


@dataclass
class RevisionState:
    """社区边界版本。"""

    revision_id: str
    community_id: str
    revision_no: int
    center: tuple[float, float]
    service_radius_km: float
    boundary_ref: str
    supersedes: str | None = None
    superseded_by: str | None = None


@dataclass
class NeedState:
    """居民提报的服务缺口需求（含匿名支持度）。"""

    need_id: str
    community_revision: str
    category: str
    support_count: int
    resident_ref: str | None
    elderly_focus: bool
    location: tuple[float, float] | None
    required_moments: tuple[str, ...]
    public_consent: bool = True
    withdrawn: bool = False
    review_requests: int = 0
    last_review_reason: str | None = None


@dataclass
class Approach:
    """网点相对某社区的步行可达事实。"""

    community_id: str
    distance_km: float
    walk_minutes: int
    barriers_signalized: tuple[bool, ...] = ()


@dataclass
class SiteState:
    """商户提报的网点/开店意向。"""

    site_id: str
    applicant_id: str
    categories: tuple[str, ...]
    location: tuple[float, float]
    serves_communities: tuple[str, ...]
    capacity: int
    status: SiteStatus
    opening_hours: tuple = ()
    planned_open_date: str | None = None
    approaches: dict[str, Approach] = field(default_factory=dict)
    safety_passed: bool = False
    safety_decided_by: str | None = None
    fund_reservations: dict[str, int] = field(default_factory=dict)
    verified: bool = False
    verified_by: str | None = None
    evidence_set: tuple[str, ...] = ()


@dataclass
class DecisionState:
    """覆盖结论复核记录。"""

    decision_id: str
    community_revision: str
    need_id: str
    site_id: str | None
    category: str
    reviewer_id: str
    applicant_id: str
    result: str  # covered / gap
    safety_passed: bool
    resident_opinion_count: int
    basis_superseded: bool = False
    verified: bool = False


@dataclass
class AcceptanceRecord:
    """社区通过验收时冻结的依据。"""

    community_id: str
    revision_id: str
    decision_ids: frozenset[str]
    need_ids: frozenset[str]
    verified_by: str
    accepted_at: str  # 验收事件发生时间，冻结回放的截止点
    # 验收当时每条需求的结论（covered/gap/withdrawn）与覆盖网点，事后不改写
    frozen_outcomes: dict[str, tuple[str, str | None]] = field(default_factory=dict)


@dataclass
class Snapshot:
    revisions: dict[str, RevisionState] = field(default_factory=dict)
    community_current_revision: dict[str, str] = field(default_factory=dict)
    needs: dict[str, NeedState] = field(default_factory=dict)
    sites: dict[str, SiteState] = field(default_factory=dict)
    decisions: dict[str, DecisionState] = field(default_factory=dict)
    # 已验收社区：冻结在验收时的边界版本与服务依据
    acceptances: dict[str, AcceptanceRecord] = field(default_factory=dict)
    # 验收未通过/中断的证据登记：decision_id -> 最近一次证据集合
    evidence_attempts: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def revision_of_community(self, community_id: str) -> RevisionState | None:
        rev_id = self.community_current_revision.get(community_id)
        return self.revisions.get(rev_id) if rev_id else None

    def sites_for(self, community_id: str) -> list[SiteState]:
        return [s for s in self.sites.values() if community_id in s.serves_communities]


def _as_xy(value: Any) -> tuple[float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return float(value[0]), float(value[1])
    return None


def _site_status(raw: Any) -> SiteStatus:
    if isinstance(raw, str):
        try:
            return SiteStatus(raw)
        except ValueError:
            pass
    return SiteStatus.PLANNED


def fold(events: list[StoredEvent]) -> Snapshot:
    """按事件流顺序折叠为当前快照。"""
    snap = Snapshot()

    for event in events:
        p = dict(event.payload)
        et = event.event_type

        if et == "COMMUNITY_REVISED":
            rev = RevisionState(
                revision_id=event.aggregate_id,
                community_id=p["community_id"],
                revision_no=int(p["revision_no"]),
                center=_as_xy(p.get("center_km")) or (0.0, 0.0),
                service_radius_km=float(p["service_radius_km"]),
                boundary_ref=p.get("boundary_ref", ""),
                supersedes=p.get("supersedes"),
            )
            snap.revisions[rev.revision_id] = rev
            snap.community_current_revision[rev.community_id] = rev.revision_id
            if rev.supersedes and rev.supersedes in snap.revisions:
                snap.revisions[rev.supersedes].superseded_by = rev.revision_id
                # 规划变更影响尚未验收的方案：旧版本上未冻结的结论标记失效
                old_community = snap.revisions[rev.supersedes].community_id
                if old_community not in snap.acceptances:
                    for decision in snap.decisions.values():
                        if decision.community_revision == rev.supersedes:
                            decision.basis_superseded = True

        elif et == "NEED_AGGREGATED":
            need = NeedState(
                need_id=event.aggregate_id,
                community_revision=p["community_revision"],
                category=p["category"],
                support_count=int(p.get("support_count", 1)),
                resident_ref=p.get("resident_ref"),
                elderly_focus=bool(p.get("elderly_focus", False)),
                location=_as_xy(p.get("location_km")),
                required_moments=tuple(p.get("required_moments", ())),
                public_consent=bool(p.get("public_consent", True)),
            )
            snap.needs[need.need_id] = need

        elif et == "NEED_WITHDRAWN":
            need = snap.needs.get(event.aggregate_id)
            if need:
                need.withdrawn = True

        elif et == "CONSENT_WITHDRAWN":
            # 居民撤回公开展示同意：需求事实与汇总统计保留，仅关闭公开标识
            need = snap.needs.get(event.aggregate_id)
            if need:
                need.public_consent = False
                need.resident_ref = None

        elif et == "REVIEW_REQUESTED":
            # 居民意见可推动复核，但不直接改变安全结论
            need = snap.needs.get(event.aggregate_id)
            if need:
                need.review_requests += 1
                need.last_review_reason = p.get("reason")

        elif et == "SITE_PROPOSED":
            approaches: dict[str, Approach] = {}
            for item in p.get("approaches", []):
                approaches[item["community_id"]] = Approach(
                    community_id=item["community_id"],
                    distance_km=float(item["distance_km"]),
                    walk_minutes=int(item.get("walk_minutes", 0)),
                    barriers_signalized=tuple(
                        bool(b.get("signalized", True))
                        for b in item.get("barriers", [])
                    ),
                )
            site = SiteState(
                site_id=event.aggregate_id,
                applicant_id=p["applicant_id"],
                categories=tuple(p.get("categories", [])),
                location=_as_xy(p.get("location_km")) or (0.0, 0.0),
                serves_communities=tuple(p.get("serves_communities", [])),
                capacity=int(p.get("capacity", len(p.get("serves_communities", [])) or 1)),
                status=_site_status(p.get("status")),
                opening_hours=parse_opening_hours(p.get("opening_hours")),
                planned_open_date=p.get("planned_open_date"),
                approaches=approaches,
            )
            snap.sites[site.site_id] = site

        elif et == "SITE_STATUS_CHANGED":
            site = snap.sites.get(event.aggregate_id)
            if site:
                site.status = _site_status(p.get("status"))

        elif et == "FUND_RESERVED":
            site = snap.sites.get(event.aggregate_id)
            if site:
                round_id = p["funding_round"]
                site.fund_reservations[round_id] = (
                    site.fund_reservations.get(round_id, 0) + int(p["amount"])
                )

        elif et == "COVERAGE_REVIEWED":
            decision = DecisionState(
                decision_id=event.aggregate_id,
                community_revision=p["community_revision"],
                need_id=p["need_id"],
                site_id=p.get("site_id"),
                category=p["category"],
                reviewer_id=p["reviewer_id"],
                applicant_id=p["applicant_id"],
                result=p.get("result", "gap"),
                safety_passed=bool(p.get("safety_passed", False)),
                resident_opinion_count=int(p.get("resident_opinion_count", 0)),
            )
            snap.decisions[decision.decision_id] = decision
            if decision.site_id and decision.safety_passed:
                reviewed_site = snap.sites.get(decision.site_id)
                if reviewed_site and reviewed_site.applicant_id == decision.applicant_id:
                    reviewed_site.safety_passed = True
                    reviewed_site.safety_decided_by = decision.reviewer_id

        elif et == "SITE_VERIFIED":
            snap.evidence_attempts[event.aggregate_id] = tuple(p.get("evidence_set", []))
            decision = snap.decisions.get(event.aggregate_id)
            site = snap.sites.get(p.get("site_id")) if p.get("site_id") else None
            if not p.get("passed", False):
                continue
            if decision:
                decision.verified = True
            # 只有绑定网点、安全审查已通过的覆盖结论通过证据核查，才构成
            # 社区验收并冻结依据；缺口结论的核查不落验收。
            if (
                site is not None
                and decision is not None
                and decision.safety_passed
                and decision.result == "covered"
            ):
                site.verified = True
                site.verified_by = p["verified_by"]
                site.evidence_set = tuple(p.get("evidence_set", ()))
                revision = snap.revisions.get(decision.community_revision)
                if revision is not None:
                    frozen_decisions = frozenset(
                        d.decision_id
                        for d in snap.decisions.values()
                        if d.community_revision == revision.revision_id
                    ) | ({decision.decision_id} if decision else set())
                    frozen_needs = frozenset(
                        n.need_id
                        for n in snap.needs.values()
                        if n.community_revision == revision.revision_id
                    )
                    snap.acceptances[revision.community_id] = AcceptanceRecord(
                        community_id=revision.community_id,
                        revision_id=revision.revision_id,
                        decision_ids=frozen_decisions,
                        need_ids=frozen_needs,
                        verified_by=p["verified_by"],
                        accepted_at=event.occurred_at,
                        frozen_outcomes={
                            str(k): (str(v[0]), v[1])
                            for k, v in p.get("frozen_outcomes", {}).items()
                        },
                    )

    return snap

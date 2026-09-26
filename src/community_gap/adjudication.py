"""缺口裁决引擎（纯函数）。

输入事件回放快照，输出每条需求的覆盖结论与**结构化原因**。只看覆盖数量
会被以下事实纠正，因此每条候选网点都要逐项过闸：

1. 服务半径（以社区公示边界版本为准）；
2. 老年友好步行可达（无信号灯过街障碍不可达，不用直线距离代替）；
3. 服务类别匹配；
4. 网点状态：计划中不计已覆盖、暂时停业不供给；
5. 营业时段必须覆盖需求时段（夜间购药需求不会被白班药店满足）；
6. 安全审查结论独立于居民意见；
7. 跨社区网点按容量去重，不重复计作独立供给；
8. 已验收社区冻结在当时边界版本与服务依据上；规划变更只影响未验收方案。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .domain import GapReason, SiteStatus, WeekTime
from .state import NeedState, RevisionState, SiteState, Snapshot


class Verdict(str, Enum):
    COVERED = "covered"
    GAP = "gap"
    WITHDRAWN = "withdrawn"


@dataclass
class CandidateAssessment:
    """单个候选网点相对某条需求的逐项核查结果。"""

    site_id: str
    applicable: bool
    blockers: list[GapReason] = field(default_factory=list)
    detail: dict[str, object] = field(default_factory=dict)


@dataclass
class NeedVerdict:
    need_id: str
    community_id: str
    revision_id: str
    category: str
    verdict: Verdict
    reasons: list[GapReason] = field(default_factory=list)
    candidates: list[CandidateAssessment] = field(default_factory=list)
    covered_by: str | None = None
    frozen: bool = False

    def explain_lines(self) -> list[str]:
        """面向市级专员/居民接口的中文解释。"""
        if self.frozen:
            basis = (
                f"已由网点 {self.covered_by} 覆盖"
                if self.covered_by
                else "判定为缺口"
            )
            return [
                f"所在社区已通过验收，需求 {self.need_id} 沿用验收时结论（{basis}）、"
                f"边界版本 {self.revision_id} 与服务依据，不随后续规划变更重判。"
            ]
        if self.verdict is Verdict.COVERED and self.covered_by:
            return [f"需求 {self.need_id} 已由网点 {self.covered_by} 覆盖。"]
        if self.verdict is Verdict.WITHDRAWN:
            return [f"需求 {self.need_id} 已撤回，不再纳入缺口裁决。"]
        lines = [f"片区仍被判为缺口（需求 {self.need_id}，类别 {self.category}）的原因："]
        if not self.candidates:
            lines.append(f"- {GapReason.NO_SITE.explain}")
        seen: set[GapReason] = set()
        for reason in self.reasons:
            if reason not in seen:
                seen.add(reason)
                lines.append(f"- {reason.explain}")
        return lines


@dataclass
class AdjudicationResult:
    verdicts: list[NeedVerdict]
    listing: dict[str, dict[str, list[dict[str, object]]]] = field(default_factory=dict)

    def gaps(self) -> list[NeedVerdict]:
        return [v for v in self.verdicts if v.verdict is Verdict.GAP]

    def explain(self, need_id: str) -> list[str]:
        for verdict in self.verdicts:
            if verdict.need_id == need_id:
                return verdict.explain_lines()
        return [f"未找到需求 {need_id}，可能已按新边界版本重新提报。"]

    def final_listing(self) -> dict[str, dict[str, list[dict[str, object]]]]:
        """最终清单：按社区、状态（计划中/已开放/暂时停业）分组的网点。

        一个网点跨多社区服务时，在每个社区清单里以同一 ``site_id`` 呈现，
        但供给去重发生在裁决阶段（容量占用），清单本身不制造额外供给。
        """
        return self.listing


def _site_in_radius(
    site: SiteState, revision: RevisionState, need: NeedState
) -> tuple[bool, float]:
    """优先采用登记的步勘接近事实；没有时用平面几何距离兜底。"""
    approach = site.approaches.get(revision.community_id)
    if approach is not None:
        return approach.distance_km <= revision.service_radius_km, approach.distance_km
    if need.location is not None:
        dx = site.location[0] - need.location[0]
        dy = site.location[1] - need.location[1]
        distance = (dx * dx + dy * dy) ** 0.5
        return distance <= revision.service_radius_km, distance
    return False, float("inf")


def _elderly_blocked(site: SiteState, revision: RevisionState, need: NeedState) -> bool:
    if not need.elderly_focus:
        return False
    approach = site.approaches.get(revision.community_id)
    if approach is None:
        return False
    return any(not signalized for signalized in approach.barriers_signalized)


def _hours_cover(site: SiteState, need: NeedState) -> bool:
    """需求未声明时段时不做时段约束；每个需求时段至少被一个营业窗口覆盖。"""
    if not need.required_moments:
        return True
    if not site.opening_hours:
        return False
    moments = []
    for raw in need.required_moments:
        try:
            moments.append(WeekTime.parse(raw))
        except ValueError:
            moments.append(None)
    for moment in moments:
        if moment is None:
            return False
        if not any(window.covers(moment) for window in site.opening_hours):
            return False
    return True


def assess_candidate(site: SiteState, revision: RevisionState, need: NeedState) -> CandidateAssessment:
    """对单个候选网点逐项核查，记录全部阻断原因（不短路，便于解释）。"""
    blockers: list[GapReason] = []
    detail: dict[str, object] = {}

    if need.category not in site.categories:
        blockers.append(GapReason.CATEGORY_MISMATCH)

    in_radius, distance = _site_in_radius(site, revision, need)
    detail["distance_km"] = None if distance == float("inf") else round(distance, 3)
    detail["radius_km"] = revision.service_radius_km
    if not in_radius:
        blockers.append(GapReason.OUT_OF_RADIUS)

    if _elderly_blocked(site, revision, need):
        blockers.append(GapReason.STREET_CROSSING_BLOCKED)

    if site.status is SiteStatus.PLANNED:
        blockers.append(GapReason.NOT_OPEN_YET)
        if not site.fund_reservations:
            blockers.append(GapReason.FUNDING_NOT_RESERVED)
    elif site.status is SiteStatus.SUSPENDED:
        blockers.append(GapReason.SUSPENDED)
    elif site.status is SiteStatus.OPEN and not _hours_cover(site, need):
        blockers.append(GapReason.CLOSED_AT_TIME)

    if not site.safety_passed:
        blockers.append(GapReason.SAFETY_REVIEW_PENDING)

    return CandidateAssessment(
        site_id=site.site_id,
        applicable=not blockers,
        blockers=blockers,
        detail=detail,
    )


def _priority(need: NeedState) -> tuple:
    # 老年聚焦需求优先占容量，其次按匿名支持度；同序按需求标识保证确定性
    return (not need.elderly_focus, -need.support_count, need.need_id)


def adjudicate(snapshot: Snapshot) -> AdjudicationResult:
    """对快照中的全部活跃需求裁决。"""
    verdicts: list[NeedVerdict] = []
    remaining_capacity: dict[str, int] = {
        site_id: site.capacity for site_id, site in snapshot.sites.items()
    }

    active_needs = [need for need in snapshot.needs.values() if not need.withdrawn]
    active_needs.sort(key=_priority)

    for need in active_needs:
        revision = snapshot.revisions.get(need.community_revision)
        if revision is None:
            verdicts.append(
                NeedVerdict(
                    need_id=need.need_id,
                    community_id="?",
                    revision_id=need.community_revision,
                    category=need.category,
                    verdict=Verdict.GAP,
                    reasons=[GapReason.SUPERSEDED_BOUNDARY],
                )
            )
            continue

        acceptance = snapshot.acceptances.get(revision.community_id)
        # 已验收社区：验收时已存在的需求冻结在当时边界版本与服务依据上
        # （含验收当时的覆盖结论与覆盖网点）；验收之后新提报的需求不在冻结
        # 范围内，按当前事实裁决。
        if (
            acceptance is not None
            and acceptance.revision_id == revision.revision_id
            and need.need_id in acceptance.need_ids
        ):
            outcome_value, covered_site = acceptance.frozen_outcomes.get(
                need.need_id, ("gap", None)
            )
            frozen_verdict = (
                Verdict.COVERED if outcome_value == "covered" else Verdict.GAP
            )
            verdicts.append(
                NeedVerdict(
                    need_id=need.need_id,
                    community_id=revision.community_id,
                    revision_id=revision.revision_id,
                    category=need.category,
                    verdict=frozen_verdict,
                    covered_by=covered_site if frozen_verdict is Verdict.COVERED else None,
                    frozen=True,
                )
            )
            continue

        candidates = [
            assess_candidate(site, revision, need)
            for site in snapshot.sites_for(revision.community_id)
        ]
        candidates.sort(key=lambda c: c.site_id)

        verdict = NeedVerdict(
            need_id=need.need_id,
            community_id=revision.community_id,
            revision_id=revision.revision_id,
            category=need.category,
            verdict=Verdict.GAP,
            candidates=candidates,
        )

        # 规划变更：依据旧边界版本且未验收 → 缺口并要求按新版本重提
        if revision.superseded_by is not None:
            verdict.reasons.append(GapReason.SUPERSEDED_BOUNDARY)

        chosen: CandidateAssessment | None = None
        for candidate in candidates:
            if not candidate.applicable:
                continue
            if remaining_capacity.get(candidate.site_id, 0) <= 0:
                candidate.blockers.append(GapReason.NO_DEDUP_CAPACITY)
                continue
            chosen = candidate
            break

        if chosen is not None:
            remaining_capacity[chosen.site_id] -= 1
            verdict.verdict = Verdict.COVERED
            verdict.covered_by = chosen.site_id
        else:
            # 汇总片区级阻断原因。结构性闸口（半径/类别/过街）与运营性闸口
            # （状态/时段/安全/资金）分层处理：
            # - 只要存在一个结构性合格候选，原因只从这些候选取——不能拿一个
            #   过不了街或不相干类别的网点污染结论；
            # - 一个结构性合格候选都没有时，只报结构性原因：不相关类别的网点
            #   是否营业，对本类别缺口没有解释力。
            structural = {
                GapReason.OUT_OF_RADIUS,
                GapReason.CATEGORY_MISMATCH,
                GapReason.STREET_CROSSING_BLOCKED,
            }
            structurally_fit = [
                c for c in candidates if structural.isdisjoint(c.blockers)
            ]
            if structurally_fit:
                pool = structurally_fit
            else:
                pool = []
                for candidate in candidates:
                    candidate.blockers = [
                        b for b in candidate.blockers if b in structural
                    ]
                    pool.append(candidate)
            for candidate in pool:
                verdict.reasons.extend(candidate.blockers)
            # 保持原因稳定顺序（按枚举定义序）
            order = {reason: i for i, reason in enumerate(GapReason)}
            verdict.reasons = sorted(set(verdict.reasons), key=lambda r: order[r])

        verdicts.append(verdict)

    # 撤回需求单独给出结论，不计入缺口
    for need in snapshot.needs.values():
        if need.withdrawn:
            revision = snapshot.revisions.get(need.community_revision)
            verdicts.append(
                NeedVerdict(
                    need_id=need.need_id,
                    community_id=revision.community_id if revision else "?",
                    revision_id=need.community_revision,
                    category=need.category,
                    verdict=Verdict.WITHDRAWN,
                )
            )

    verdicts.sort(key=lambda v: v.need_id)
    result = AdjudicationResult(verdicts=verdicts)

    # 最终清单以网点登记事实为准（计划中/已开放/暂时停业），与覆盖结论分离
    listing: dict[str, dict[str, list[dict[str, object]]]] = {}
    for site in sorted(snapshot.sites.values(), key=lambda s: s.site_id):
        entry = {
            "site_id": site.site_id,
            "categories": list(site.categories),
            "capacity": site.capacity,
            "safety_passed": site.safety_passed,
            "verified": site.verified,
        }
        for community_id in sorted(site.serves_communities):
            bucket = listing.setdefault(
                community_id,
                {status.value: [] for status in SiteStatus},
            )
            bucket[site.status.value].append(entry)
    result.listing = listing
    return result

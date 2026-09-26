"""领域实体、枚举与裁决结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 网点状态：最终清单按此区分计划中、已开放、暂时停业
SITE_STATUSES = ("planned", "open", "suspended")
STATUS_LABELS = {"planned": "计划中", "open": "已开放", "suspended": "暂时停业"}

# 需求时段 -> (开始分钟, 结束分钟)，按一刻钟生活圈惯例划分
TIME_WINDOW_HOURS = {
    "daytime": (8 * 60, 18 * 60),
    "night": (18 * 60, 24 * 60),
    "all_day": (0, 24 * 60),
}
TIME_WINDOW_LABELS = {"daytime": "日间", "night": "夜间", "all_day": "全天"}

# 裁决结论
OUTCOMES = ("gap", "covered", "observation", "no_demand", "undecided")
OUTCOME_LABELS = {
    "gap": "缺口",
    "covered": "已覆盖",
    "observation": "观察中",
    "no_demand": "无需求记录",
    "undecided": "无法裁决",
}


@dataclass(frozen=True)
class Barrier:
    """步行障碍：位于两个片区之间，对特定人群构成阻断。"""

    barrier_id: str
    zones: tuple[str, ...]
    blocks: tuple[str, ...] = ("all",)
    note: str = ""

    def blocks_group(self, group: str) -> bool:
        return "all" in self.blocks or group in self.blocks


@dataclass
class CommunityRevision:
    """社区边界版本：片区划分与步行可达条件随版本固化。"""

    community_id: str
    revision: int
    zones: list[str]
    barriers: list[Barrier] = field(default_factory=list)
    published_at: str = ""


@dataclass
class ServiceNeed:
    """居民需求及匿名支持度；撤回记录只进不出，汇总统计保留。"""

    need_id: str
    community_id: str
    category: str
    zone: str
    time_window: str
    support_total: int
    revision: int
    vulnerable_group: str = "general"
    withdrawn_refs: set[str] = field(default_factory=set)


@dataclass
class ProviderSite:
    """网点：开店意向、营业时段、服务能力与验收状态。"""

    site_id: str
    applicant_id: str
    categories: list[str]
    community_id: str
    zone: str
    service_area: dict[str, list[str] | None]
    capacity: int
    opens_at: str
    closes_at: str
    status: str = "planned"
    verified: bool = False
    evidence_set: list[str] = field(default_factory=list)
    verified_by: str | None = None


@dataclass
class CoverageDecision:
    """覆盖结论：记录裁决时点的边界版本与供给依据。"""

    decision_id: str
    community_id: str
    category: str
    revision: int
    outcome: str
    reviewer: str
    safety_review_passed: bool
    counted_site_ids: list[str] = field(default_factory=list)
    primary_site_id: str | None = None
    demand: int = 0
    verified: bool = False
    verified_by: str | None = None
    evidence_set: list[str] = field(default_factory=list)
    stale: bool = False
    review_pending: bool = False


@dataclass
class FundingRound:
    """改造资金批次：承诺额不得超过批次余额。"""

    round_id: str
    total_budget: int
    label: str = ""
    reserved: int = 0

    @property
    def remaining(self) -> int:
        return self.total_budget - self.reserved


@dataclass(frozen=True)
class Reason:
    """裁决原因：稳定代码 + 中文说明 + 结构化细节。"""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}


@dataclass
class Assessment:
    """一次缺口裁决的完整解释。"""

    community_id: str
    category: str
    revision: int | None
    outcome: str
    reasons: list[Reason] = field(default_factory=list)
    demand: dict[str, Any] = field(default_factory=dict)
    qualifying_site_ids: list[str] = field(default_factory=list)
    excluded_supply: list[dict[str, Any]] = field(default_factory=list)
    primary_site_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "community_id": self.community_id,
            "category": self.category,
            "revision": self.revision,
            "outcome": self.outcome,
            "outcome_label": OUTCOME_LABELS.get(self.outcome, self.outcome),
            "reasons": [reason.to_dict() for reason in self.reasons],
            "demand": dict(self.demand),
            "qualifying_site_ids": list(self.qualifying_site_ids),
            "excluded_supply": [dict(item) for item in self.excluded_supply],
            "primary_site_id": self.primary_site_id,
        }

"""领域静态模型：类别、网点状态、可达条件与裁决原因码。

这些类型只描述"事实长什么样"，不做裁决（裁决在 ``adjudication`` 模块）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

WEEKDAY_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class SiteStatus(str, Enum):
    """网点在最终清单中的三种状态。"""

    PLANNED = "planned"        # 计划中：已提报意向，尚未营业
    OPEN = "open"              # 已开放：营业中，按公示时段提供服务
    SUSPENDED = "suspended"    # 暂时停业：仍登记在册但当前不提供服务


class GapReason(str, Enum):
    """某片区被判为缺口的结构化原因（接口可逐条解释）。"""

    NO_SITE = "no_site"                       # 半径内没有任何登记网点
    OUT_OF_RADIUS = "out_of_radius"           # 网点在服务半径之外
    NOT_OPEN_YET = "not_open_yet"             # 网点尚未营业（计划中）
    SUSPENDED = "suspended"                   # 网点暂时停业
    CLOSED_AT_TIME = "closed_at_time"         # 营业时段不覆盖需求时段（如夜间购药）
    CATEGORY_MISMATCH = "category_mismatch"   # 网点不提供该服务类别
    STREET_CROSSING_BLOCKED = "street_crossing_blocked"  # 老年人过街困难，直线距离不可用
    SAFETY_REVIEW_PENDING = "safety_review_pending"      # 安全审查未通过，不能计作已覆盖
    NO_DEDUP_CAPACITY = "no_dedup_capacity"   # 跨社区网点的服务容量已被其他社区占用
    SUPERSEDED_BOUNDARY = "superseded_boundary"  # 依据的社区边界版本已被规划变更替代
    NEED_WITHDRAWN = "need_withdrawn"         # 需求已被撤回
    FUNDING_NOT_RESERVED = "funding_not_reserved"  # 改造资金尚未承诺，方案未落地

    @property
    def explain(self) -> str:
        return _REASON_TEXT[self]


_REASON_TEXT: dict[GapReason, str] = {
    GapReason.NO_SITE: "服务半径内没有任何登记网点",
    GapReason.OUT_OF_RADIUS: "最近网点超出社区公示的步行服务半径",
    GapReason.NOT_OPEN_YET: "半径内网点尚在计划中，未开始营业，不能计作已覆盖",
    GapReason.SUSPENDED: "半径内网点暂时停业，当前不提供服务",
    GapReason.CLOSED_AT_TIME: "半径内网点的营业时段不覆盖需求时段",
    GapReason.CATEGORY_MISMATCH: "半径内网点不提供该服务类别",
    GapReason.STREET_CROSSING_BLOCKED: "到达网点需穿越无安全过街设施的主干道，老年人步行不可达",
    GapReason.SAFETY_REVIEW_PENDING: "网点安全审查尚未通过，居民意见不能替代安全审查",
    GapReason.NO_DEDUP_CAPACITY: "该网点跨社区服务，容量已被其他社区占用，不可重复计作独立供给",
    GapReason.SUPERSEDED_BOUNDARY: "裁决依据的社区边界版本已被规划变更替代，需按新版本重新提报",
    GapReason.NEED_WITHDRAWN: "该需求已被提报人撤回",
    GapReason.FUNDING_NOT_RESERVED: "改造方案未取得批次资金承诺，尚不能落地为供给",
}


@dataclass(frozen=True)
class WeekTime:
    """周内时刻，单位为分钟（周一 00:00 起算），用于夜间跨午夜时段。"""

    minutes: int

    @classmethod
    def from_hm(cls, weekday: str, hour: int, minute: int = 0) -> "WeekTime":
        if weekday not in WEEKDAY_ORDER:
            raise ValueError(f"未知星期: {weekday}")
        return cls(WEEKDAY_ORDER.index(weekday) * 1440 + hour * 60 + minute)

    @classmethod
    def parse(cls, value: str) -> "WeekTime":
        """解析 ``"mon 18:30"`` 形式的周内时刻。"""
        parts = value.strip().split()
        if len(parts) != 2 or ":" not in parts[1]:
            raise ValueError(f"无法解析周内时刻: {value}")
        weekday, clock = parts
        hour_str, minute_str = clock.split(":")
        return cls.from_hm(weekday, int(hour_str), int(minute_str))


@dataclass(frozen=True)
class OpeningWindow:
    """一周营业窗口，允许跨午夜（start > end 时表示跨到次日）。"""

    start: WeekTime
    end: WeekTime

    def covers(self, moment: WeekTime) -> bool:
        if self.start.minutes <= self.end.minutes:
            return self.start.minutes <= moment.minutes <= self.end.minutes
        # 跨午夜：周一 22:00 至周二 06:00 这类窗口
        return moment.minutes >= self.start.minutes or moment.minutes <= self.end.minutes

    @classmethod
    def from_strings(cls, start: str, end: str) -> "OpeningWindow":
        return cls(WeekTime.parse(start), WeekTime.parse(end))


@dataclass(frozen=True)
class GeoPoint:
    """平面坐标（公里）。县城试点用投影坐标，距离按欧氏距离近似。"""

    x: float
    y: float

    def distance_to(self, other: "GeoPoint") -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


@dataclass(frozen=True)
class CrossingBarrier:
    """步行路径上的过街障碍：老年友好通行要求有信号灯/人行设施。"""

    from_point: GeoPoint
    to_point: GeoPoint
    signalized: bool

    def blocks(self, elderly_friendly: bool) -> bool:
        return elderly_friendly and not self.signalized


@dataclass(frozen=True)
class ServiceProfile:
    """网点的可达性事实：距离、路径障碍、步行所需时间（分钟）。"""

    distance_km: float
    walk_minutes: int
    barriers: tuple[CrossingBarrier, ...] = field(default_factory=tuple)

    def is_elderly_walkable(self) -> bool:
        return not any(b.blocks(elderly_friendly=True) for b in self.barriers)


def parse_opening_hours(raw: Any) -> tuple[OpeningWindow, ...]:
    """解析载荷中的 ``opening_hours``：``[{"start": "mon 08:00", "end": "sun 22:00"}]``。"""
    if not isinstance(raw, list):
        return ()
    windows: list[OpeningWindow] = []
    for item in raw:
        if isinstance(item, Mapping) and "start" in item and "end" in item:
            windows.append(OpeningWindow.from_strings(item["start"], item["end"]))
    return tuple(windows)

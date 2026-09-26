"""改造资金批次账本。

并发承诺不得超出批次余额：多线程同时为不同网点预留资金时，账本在锁内
完成"检查余额 → 扣减承诺"，超出立即拒绝。已提交的承诺对应 ``FUND_RESERVED``
事件，重放事件流即可恢复每个批次的已承诺总额。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


class FundingError(Exception):
    """资金承诺被拒绝。"""


@dataclass(frozen=True)
class FundingRound:
    round_id: str
    budget: int
    committed: int = 0

    @property
    def remaining(self) -> int:
        return self.budget - self.committed


class FundingLedger:
    """线程安全的批次余额账本（金额单位：元，整数）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rounds: dict[str, FundingRound] = {}

    def register_round(self, round_id: str, budget: int) -> None:
        with self._lock:
            if round_id in self._rounds:
                raise FundingError(f"资金批次已登记: {round_id}")
            if budget < 0:
                raise FundingError("批次预算不能为负")
            self._rounds[round_id] = FundingRound(round_id=round_id, budget=budget)

    def remaining(self, round_id: str) -> int:
        with self._lock:
            if round_id not in self._rounds:
                raise FundingError(f"未知资金批次: {round_id}")
            return self._rounds[round_id].remaining

    def commit(self, round_id: str, amount: int) -> int:
        """在批次内承诺一笔资金，返回承诺后剩余额度。

        余额不足时不产生任何扣减（原子拒绝）。
        """
        if amount <= 0:
            raise FundingError("承诺金额必须为正整数")
        with self._lock:
            current = self._rounds.get(round_id)
            if current is None:
                raise FundingError(f"未知资金批次: {round_id}")
            if amount > current.remaining:
                raise FundingError(
                    f"批次 {round_id} 余额不足: 需 {amount}, 余 {current.remaining}"
                )
            updated = FundingRound(
                round_id=current.round_id,
                budget=current.budget,
                committed=current.committed + amount,
            )
            self._rounds[round_id] = updated
            return updated.remaining

    def snapshot(self) -> dict[str, FundingRound]:
        with self._lock:
            return dict(self._rounds)

    def replay_commitment(self, round_id: str, committed: int) -> None:
        """从 ``FUND_RESERVED`` 事件流恢复批次已承诺总额。

        与进程内新增承诺取较大值，保证重启后余额不被重置、也不重复扣减。
        批次预算仍须通过 :meth:`register_round` 单独登记。
        """
        with self._lock:
            current = self._rounds.get(round_id)
            if current is None:
                raise FundingError(f"未知资金批次，无法恢复承诺: {round_id}")
            if committed > current.committed:
                self._rounds[round_id] = FundingRound(
                    round_id=current.round_id,
                    budget=current.budget,
                    committed=committed,
                )

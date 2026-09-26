"""验收核查队列：中断后可恢复。

队列内容由事件流派生（已有覆盖结论但尚无通过的验收证据），领取状态记录在
事件日志旁的侧车文件中。验收进程被杀死、机器重启后重新打开队列时，所有
"核查中"的任务会回到待处理队首——核查是幂等的（同一结论可重复核对证据），
因此至少执行一次不会产生错误验收；真正的状态推进只有 ``SITE_VERIFIED``
事件落盘才算数。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .state import Snapshot


@dataclass
class VerificationItem:
    decision_id: str
    community_id: str
    site_id: str | None
    need_id: str
    attempts: int = 0
    last_evidence: tuple[str, ...] = ()


@dataclass
class _Sidecar:
    in_progress: dict[str, str] = field(default_factory=dict)  # decision_id -> 领取令牌
    attempts: dict[str, int] = field(default_factory=dict)


class VerificationQueue:
    """从快照重建、侧车持久化领取状态的验收队列。"""

    def __init__(self, sidecar_path: str | Path) -> None:
        self._path = Path(sidecar_path)
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._items: dict[str, VerificationItem] = {}
        self._sidecar = self._load_sidecar()

    # ---- 持久化 ------------------------------------------------------------

    def _load_sidecar(self) -> _Sidecar:
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return _Sidecar(
                in_progress=dict(raw.get("in_progress", {})),
                attempts={k: int(v) for k, v in raw.get("attempts", {}).items()},
            )
        return _Sidecar()

    def _save_sidecar(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "in_progress": self._sidecar.in_progress,
            "attempts": self._sidecar.attempts,
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)

    # ---- 重建 --------------------------------------------------------------

    def rebuild(self, snapshot: Snapshot) -> "VerificationQueue":
        """从当前快照重建待核查队列。

        待核查 = 已作出覆盖/缺口结论、但尚未通过验收的决定。核查中（崩溃前
        领取未完成）的任务排到队首，其余按结论标识排序保证确定顺序。
        """
        with self._lock:
            self._items = {}
            candidates: list[VerificationItem] = []
            for decision in snapshot.decisions.values():
                if decision.verified:
                    continue
                revision = snapshot.revisions.get(decision.community_revision)
                item = VerificationItem(
                    decision_id=decision.decision_id,
                    community_id=revision.community_id if revision else "?",
                    site_id=decision.site_id,
                    need_id=decision.need_id,
                    attempts=self._sidecar.attempts.get(decision.decision_id, 0),
                    last_evidence=snapshot.evidence_attempts.get(
                        decision.decision_id, ()
                    ),
                )
                candidates.append(item)

            crashed = [
                item.decision_id
                for item in candidates
                if item.decision_id in self._sidecar.in_progress
            ]
            waiting = sorted(
                item.decision_id for item in candidates if item.decision_id not in crashed
            )
            crashed.sort()
            self._pending = crashed + waiting
            self._items = {item.decision_id: item for item in candidates}
            # 崩溃恢复：旧领取令牌全部作废，任务重新入队
            self._sidecar.in_progress = {}
            self._save_sidecar()
            return self

    # ---- 领取 / 完成 --------------------------------------------------------

    def claim(self) -> VerificationItem | None:
        """领取队首任务并标记核查中；队列为空返回 None。"""
        with self._lock:
            if not self._pending:
                return None
            decision_id = self._pending.pop(0)
            token = uuid.uuid4().hex
            self._sidecar.in_progress[decision_id] = token
            self._save_sidecar()
            return self._items[decision_id]

    def record_attempt(self, decision_id: str, evidence_set: tuple[str, ...]) -> None:
        """登记一次证据核查（无论是否通过），用于中断恢复后展示历史。"""
        with self._lock:
            count = self._sidecar.attempts.get(decision_id, 0) + 1
            self._sidecar.attempts[decision_id] = count
            if decision_id in self._items:
                item = self._items[decision_id]
                item.attempts = count
                item.last_evidence = tuple(evidence_set)
            self._save_sidecar()

    def complete(self, decision_id: str) -> None:
        """验收通过并已落盘事件后，移出队列。"""
        with self._lock:
            self._sidecar.in_progress.pop(decision_id, None)
            self._items.pop(decision_id, None)
            self._pending = [d for d in self._pending if d != decision_id]
            self._save_sidecar()

    def requeue(self, decision_id: str) -> None:
        """验收未通过：回到队尾等待补证后重新核查。"""
        with self._lock:
            self._sidecar.in_progress.pop(decision_id, None)
            if decision_id in self._items and decision_id not in self._pending:
                self._pending.append(decision_id)
            self._save_sidecar()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "pending": len(self._pending),
                "in_progress": len(self._sidecar.in_progress),
                "total": len(self._items),
            }

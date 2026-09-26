"""核查队列：落盘持久化，验收中断后可恢复。"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import DomainError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class VerificationQueue:
    """按 (类型, 社区, 类别) 去重的核查任务队列。

    传入路径即落盘：每次状态变更原子写回；重新打开时执行恢复，
    把中断时处于 in_progress 的任务退回 pending，保证不丢核查项。
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        if self._path and self._path.exists():
            self._load()
            self.recover()

    @staticmethod
    def task_key(kind: str, community_id: str, category: str) -> str:
        return f"{kind}:{community_id}:{category}"

    def _load(self) -> None:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        for task in data.get("tasks", []):
            self._tasks[task["task_id"]] = task

    def _persist(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"tasks": list(self._tasks.values())}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    def enqueue(self, kind: str, community_id: str, category: str) -> dict[str, Any]:
        """入队；同一核查项在未完成前重复入队只保留一条。"""
        key = self.task_key(kind, community_id, category)
        with self._lock:
            existing = self._tasks.get(key)
            if existing and existing["state"] in ("pending", "in_progress"):
                return dict(existing)
            task = {
                "task_id": key,
                "kind": kind,
                "community_id": community_id,
                "category": category,
                "state": "pending",
                "attempts": existing["attempts"] if existing else 0,
                "enqueued_at": _now(),
            }
            self._tasks[key] = task
            self._persist()
            return dict(task)

    def next(self) -> dict[str, Any] | None:
        """取出最早入队的待办任务并标记为核查中。"""
        with self._lock:
            pending = [t for t in self._tasks.values() if t["state"] == "pending"]
            if not pending:
                return None
            task = min(pending, key=lambda t: t["enqueued_at"])
            task["state"] = "in_progress"
            task["attempts"] += 1
            self._persist()
            return dict(task)

    def complete(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise DomainError("UNKNOWN_TASK", "核查任务不存在", {"task_id": task_id})
            task["state"] = "done"
            self._persist()

    def fail(self, task_id: str) -> None:
        """核查失败：退回待办，等待重试。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise DomainError("UNKNOWN_TASK", "核查任务不存在", {"task_id": task_id})
            task["state"] = "pending"
            self._persist()

    def recover(self) -> int:
        """中断恢复：核查中的任务一律退回待办，返回恢复条数。"""
        with self._lock:
            restored = 0
            for task in self._tasks.values():
                if task["state"] == "in_progress":
                    task["state"] = "pending"
                    restored += 1
            if restored:
                self._persist()
            return restored

    def has_open(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            return task is not None and task["state"] in ("pending", "in_progress")

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t["state"] == "pending")

    def tasks(self, state: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._tasks.values())
        if state is not None:
            rows = [t for t in rows if t["state"] == state]
        return [dict(t) for t in sorted(rows, key=lambda t: t["task_id"])]

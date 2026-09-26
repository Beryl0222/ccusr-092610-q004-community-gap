"""事件存储：相同事件标识幂等，聚合版本从 1 开始递增。"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import DomainError


@dataclass(frozen=True)
class Receipt:
    event_id: str
    aggregate_type: str
    aggregate_id: str
    version: int
    applied: bool
    duplicate: bool = False


class EventStore:
    """线程安全的事件追加存储，负责幂等与版本冲突隔离。"""

    def __init__(self) -> None:
        self._events: dict[str, Mapping[str, Any]] = {}
        self._versions: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(aggregate_type: str, aggregate_id: str) -> str:
        return f"{aggregate_type}:{aggregate_id}"

    def next_version(self, aggregate_type: str, aggregate_id: str) -> int:
        with self._lock:
            return self._versions.get(self._key(aggregate_type, aggregate_id), 0) + 1

    def append(self, event: Mapping[str, Any]) -> Receipt:
        event_id = str(event["event_id"])
        aggregate_type = str(event["aggregate_type"])
        aggregate_id = str(event["aggregate_id"])
        version = int(event["version"])
        key = self._key(aggregate_type, aggregate_id)
        with self._lock:
            if event_id in self._events:
                known = self._events[event_id]
                return Receipt(
                    event_id=event_id,
                    aggregate_type=str(known["aggregate_type"]),
                    aggregate_id=str(known["aggregate_id"]),
                    version=int(known["version"]),
                    applied=False,
                    duplicate=True,
                )
            expected = self._versions.get(key, 0) + 1
            if version != expected:
                raise DomainError(
                    "VERSION_CONFLICT",
                    "聚合版本冲突",
                    {"aggregate_id": aggregate_id, "expected": expected, "got": version},
                )
            self._events[event_id] = event
            self._versions[key] = version
        return Receipt(
            event_id=event_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            version=version,
            applied=True,
        )

    def __len__(self) -> int:
        return len(self._events)

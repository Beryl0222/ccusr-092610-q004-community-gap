"""领域事件存储：只追加（append-only）的 JSONL 日志。

仓库契约（``contracts/domain.schema.json``）只校验事件信封，本模块在此基础上
承担契约文档里声明由上层服务负责的部分：

- ``event_id`` 业务幂等：相同事件标识只生效一次；
- 冲突隔离：同一聚合的版本号必须连续递增，旧版本追加失败；
- 状态推进：调用方按聚合当前版本 +1 提交，存储层负责拒绝竞态写入。

存储是一个 JSON Lines 文件，每行一个事件信封。线程内使用可串行化语义；
多进程并发不做分布式锁（市级专员单机/单服务部署即可），但同一进程内
``threading.RLock`` 保证多线程追加与读取一致。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractIssue, validate_event

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def load_schema() -> dict[str, Any]:
    """读取随包发布的领域事件 JSON Schema。"""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


class EventStoreError(Exception):
    """事件存储层错误基类。"""


class DuplicateEventError(EventStoreError):
    """相同 ``event_id`` 已经存在（业务幂等命中）。"""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"事件已存在: {event_id}")
        self.event_id = event_id


class ConcurrencyError(EventStoreError):
    """聚合版本冲突：期望版本与当前版本不一致。"""

    def __init__(self, aggregate_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"聚合 {aggregate_id} 版本冲突: 期望 {expected}, 当前 {actual}"
        )
        self.aggregate_id = aggregate_id
        self.expected = expected
        self.actual = actual


class ContractViolationError(EventStoreError):
    """事件信封未通过契约校验。"""

    def __init__(self, issues: list[ContractIssue]) -> None:
        super().__init__("; ".join(f"{i.field}:{i.code}" for i in issues))
        self.issues = issues


@dataclass(frozen=True)
class StoredEvent:
    """一条已落盘的领域事件。"""

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    payload: Mapping[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StoredEvent":
        return cls(
            event_id=raw["event_id"],
            event_type=raw["event_type"],
            aggregate_type=raw["aggregate_type"],
            aggregate_id=raw["aggregate_id"],
            occurred_at=raw["occurred_at"],
            version=int(raw["version"]),
            payload=raw["payload"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at,
            "version": self.version,
            "payload": dict(self.payload),
        }


class EventStore:
    """JSONL 只追加事件存储。"""

    def __init__(self, path: str | Path, *, schema: Mapping[str, Any] | None = None) -> None:
        self.path = Path(path)
        self._schema = dict(schema) if schema is not None else load_schema()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    # ---- 读 ----------------------------------------------------------------

    def read_all(self) -> list[StoredEvent]:
        """按落盘顺序读取全部事件（追加顺序即全序）。"""
        with self._lock:
            events: list[StoredEvent] = []
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        events.append(StoredEvent.from_dict(json.loads(line)))
            return events

    def current_version(self, aggregate_id: str) -> int:
        """聚合当前版本；从未出现过的聚合版本为 0。"""
        with self._lock:
            version = 0
            for event in self.read_all():
                if event.aggregate_id == aggregate_id and event.version > version:
                    version = event.version
            return version

    # ---- 写 ----------------------------------------------------------------

    def append(
        self,
        event: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> StoredEvent:
        """校验并追加一条事件。

        ``expected_version`` 为该聚合当前版本；事件本身的 ``version`` 必须是
        ``expected_version + 1``。两者都校验，调用方传错或并发抢先都会得到
        :class:`ConcurrencyError`，不会产生半写状态。
        """
        with self._lock:
            issues = validate_event(event, self._schema)
            if issues:
                raise ContractViolationError(issues)

            existing = self.read_all()
            event_id = event["event_id"]
            if any(e.event_id == event_id for e in existing):
                raise DuplicateEventError(event_id)

            aggregate_id = event["aggregate_id"]
            current = max(
                (e.version for e in existing if e.aggregate_id == aggregate_id),
                default=0,
            )
            new_version = int(event["version"])
            if expected_version is not None and expected_version != current:
                raise ConcurrencyError(aggregate_id, expected_version, current)
            if new_version != current + 1:
                raise ConcurrencyError(aggregate_id, current, current)

            stored = StoredEvent.from_dict(event)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(stored.to_dict(), ensure_ascii=False) + "\n")
            return stored

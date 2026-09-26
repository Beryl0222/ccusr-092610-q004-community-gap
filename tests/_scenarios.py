"""测试夹具：在临时目录上搭建服务。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from community_gap.events import EventStore
from community_gap.funding import FundingLedger
from community_gap.queue import VerificationQueue
from community_gap.service import GapAdjudicationService

TZ = timezone(timedelta(hours=8))


class FixedClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 26, 9, 0, tzinfo=TZ)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def build_service(tmp_path: Path) -> GapAdjudicationService:
    store = EventStore(tmp_path / "events.jsonl")
    queue = VerificationQueue(tmp_path / "verify.json")
    ledger = FundingLedger()
    return GapAdjudicationService(store, ledger=ledger, queue=queue, clock=FixedClock())

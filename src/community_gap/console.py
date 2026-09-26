"""缺口裁决台命令行。

用法：
    PYTHONPATH=src python -m community_gap.console <事件日志.jsonl> <命令> [参数]

命令：
    explain <need_id>        解释某片区/需求仍被判为缺口的具体原因
    gaps                     列出当前全部缺口及原因码
    listing [community_id]   输出最终清单（计划中/已开放/暂时停业）
    summary                  输出脱敏后的公开汇总统计
    queue                    查看验收核查队列状态（中断恢复用）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .events import EventStore
from .queue import VerificationQueue
from .service import GapAdjudicationService


def _build_service(log_path: Path) -> GapAdjudicationService:
    store = EventStore(log_path)
    sidecar = log_path.with_suffix(".verify.json")
    queue = VerificationQueue(sidecar)
    return GapAdjudicationService(store, queue=queue)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    log_path = Path(args[0])
    command = args[1]

    if not log_path.exists():
        print(f"事件日志不存在: {log_path}", file=sys.stderr)
        return 2

    service = _build_service(log_path)

    if command == "explain":
        if len(args) != 3:
            print("用法: explain <need_id>", file=sys.stderr)
            return 2
        for line in service.adjudicate().explain(args[2]):
            print(line)
        return 0

    if command == "gaps":
        payload = []
        for verdict in service.adjudicate().gaps():
            payload.append(
                {
                    "need_id": verdict.need_id,
                    "community_id": verdict.community_id,
                    "category": verdict.category,
                    "reasons": [r.value for r in verdict.reasons],
                }
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if command == "listing":
        listing = service.adjudicate().final_listing()
        if len(args) == 3:
            listing = {args[2]: listing.get(args[2], {})}
        print(json.dumps(listing, ensure_ascii=False, indent=2))
        return 0

    if command == "summary":
        print(json.dumps(service.public_summary(), ensure_ascii=False, indent=2))
        return 0

    if command == "queue":
        queue = service.rebuild_queue()
        print(json.dumps(queue.stats(), ensure_ascii=False))
        return 0

    print(f"未知命令: {command}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

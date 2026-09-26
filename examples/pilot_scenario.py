"""试点城市端到端示例：县城社区扩围后的缺口裁决流程。

运行：PYTHONPATH=src python3 examples/pilot_scenario.py
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from community_gap import DomainError, GapAdjudicationService, VerificationQueue


def main() -> None:
    queue_path = str(Path(tempfile.mkdtemp()) / "verification_queue.json")
    svc = GapAdjudicationService(queue_path=queue_path, support_threshold=10)

    # 1. 发布县城社区边界版本，登记过街障碍
    svc.publish_boundary(
        "county-01", 1, ["z-east", "z-west"],
        barriers=[{"barrier_id": "b-1", "zones": ["z-east", "z-west"],
                   "blocks": ["elderly"], "note": "无信号灯主干道"}],
    )

    # 2. 居民提报需求（匿名支持度）
    svc.aggregate_need("county-01", "托老", zone="z-east", time_window="daytime",
                       support_total=23, vulnerable_group="elderly")
    svc.aggregate_need("county-01", "夜间购药", zone="z-east", time_window="night", support_total=17)

    # 3. 商户提交开店意向：一家托老点在障碍另一侧，一家药店只营业到 18 点
    svc.propose_site("site-elder", applicant_id="merchant-1", categories=["托老"],
                     community_id="county-01", zone="z-west", service_area={"county-01": None},
                     capacity=50, opens_at="08:00", closes_at="20:00")
    svc.change_site_status("site-elder", "open")
    svc.verify_site("site-elder", ["acceptance-photo-1"], verified_by="inspector-1")
    svc.propose_site("site-pharm", applicant_id="merchant-2", categories=["夜间购药"],
                     community_id="county-01", zone="z-east", service_area={"county-01": None},
                     capacity=80, opens_at="08:00", closes_at="18:00")
    svc.change_site_status("site-pharm", "open")
    svc.verify_site("site-pharm", ["acceptance-photo-2"], verified_by="inspector-1")

    # 4. 解释为什么仍判为缺口
    print("== 托老（老年人过街困难）==")
    print(json.dumps(svc.explain_gap("county-01", "托老")["assessment"]["reasons"], ensure_ascii=False, indent=2))
    print("== 夜间购药（营业时段不足）==")
    print(json.dumps(svc.explain_gap("county-01", "夜间购药")["assessment"]["reasons"], ensure_ascii=False, indent=2))

    # 5. 药店延长夜间营业后，审核人完成安全审查，记为已覆盖
    svc.propose_site("site-pharm", applicant_id="merchant-2", categories=["夜间购药"],
                     community_id="county-01", zone="z-east", service_area={"county-01": None},
                     capacity=80, opens_at="08:00", closes_at="24:00")
    svc.review_coverage("county-01", "夜间购药", outcome="covered",
                        reviewer="reviewer-1", safety_review_passed=True)
    svc.verify_decision("decision:county-01:夜间购药", ["spot-check-1"], verified_by="qa-1")

    # 6. 申请者不得审核自己的覆盖结论
    try:
        svc.review_coverage("county-01", "托老", outcome="gap",
                            reviewer="merchant-1", safety_review_passed=False)
    except DomainError as exc:
        print(f"\n职责分离拦截：{exc.code} {exc.message}")

    # 7. 改造资金批次：并发承诺不超余额
    svc.open_funding_round("round-2026-3", 100, label="2026年第三批改造资金")
    svc.reserve_funds("round-2026-3", 70, site_id="site-elder")
    try:
        svc.reserve_funds("round-2026-3", 40, site_id="site-pharm")
    except DomainError as exc:
        print(f"资金拦截：{exc.code} {exc.message}（余额 {exc.details['remaining']}）")

    # 8. 居民撤回公开展示同意：汇总保留，公开视图不可反向识别
    svc.withdraw_consent("county-01", "托老", "resident-pseudo-7")
    print("\n公开视图：", json.dumps(svc.public_need_view("county-01", "托老"), ensure_ascii=False))

    # 9. 规划变更：已验收的保留当时依据，未验收的退回复核队列
    svc.review_coverage("county-01", "托老", outcome="gap", reviewer="reviewer-2", safety_review_passed=False)
    svc.publish_boundary("county-01", 2, ["z-east", "z-west", "z-north"])
    print("\n托老决策状态：", svc.explain_gap("county-01", "托老")["decision"])
    print("夜间购药决策版本（已验收保留 v1）：",
          svc.explain_gap("county-01", "夜间购药")["decision"]["revision"])

    # 10. 模拟验收中断后恢复核查队列
    svc.queue.next()
    restored = VerificationQueue(queue_path)
    print(f"\n中断恢复后待核查：{restored.pending_count()} 项")

    # 11. 最终清单：区分计划中、已开放、暂时停业
    svc.change_site_status("site-elder", "suspended")
    print("\n最终清单：")
    for row in svc.service_directory("county-01"):
        print(f"  {row['site_id']:<12} {row['status_label']}  类别={','.join(row['categories'])}  验收={row['verified']}")
    print("\n供给汇总：", json.dumps(svc.supply_summary(), ensure_ascii=False))


if __name__ == "__main__":
    main()

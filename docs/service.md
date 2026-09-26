# 缺口裁决服务说明

在领域事件契约（`contracts/domain.schema.json`）之上，`src/community_gap/`
实现县级试点扩展后的便民服务缺口裁决服务。所有事实只追加写入 JSONL 事件日志，
当前状态由事件流确定性回放得到。

## 模块

| 模块 | 职责 |
| --- | --- |
| `events.py` | 只追加事件存储：`event_id` 幂等、聚合版本乐观冲突、信封契约校验 |
| `domain.py` | 类别/网点状态/营业周窗/过街障碍等静态模型与缺口原因码 |
| `state.py` | 事件折叠：社区边界版本、需求、网点、结论、验收冻结快照 |
| `adjudication.py` | 纯函数裁决引擎：覆盖判定、结构化缺口原因、最终清单 |
| `funding.py` | 线程安全的改造资金批次账本（并发承诺不超余额） |
| `queue.py` | 验收核查队列：侧车持久化领取状态，中断/崩溃后恢复 |
| `service.py` | 服务门面：命令 API、审核回避、隐私脱敏、汇总查询 |
| `console.py` | 命令行：explain / gaps / listing / summary / queue |

## 裁决过闸顺序

对每条活跃需求与其服务半径内的候选网点逐项核查（不短路，全部原因都保留）：

1. **服务类别**：网点不提供该类别 → `category_mismatch`；
2. **服务半径**：优先采用步勘登记的接近距离，无登记时用平面几何距离兜底
   → 超出半径 `out_of_radius`；
3. **老年友好可达**：需求标注老年聚焦时，路径上存在无信号灯过街障碍
   → `street_crossing_blocked`，不用直线距离冒充可达；
4. **网点状态**：计划中不计已覆盖（`not_open_yet`，未取得资金承诺时
   追加 `funding_not_reserved`）；暂时停业不供给（`suspended`）；
5. **营业时段**：周窗支持跨午夜；需求声明的每个时段（如 `mon 23:30`）
   都必须被某个窗口覆盖，否则 `closed_at_time`；
6. **安全审查**：未通过 → `safety_review_pending`；
7. **跨社区去重**：一个网点跨多社区服务时按 `capacity` 占用，容量被
   其他社区占完 → `no_dedup_capacity`，不重复计作独立供给。

片区级原因分层聚合：存在结构性合格候选（半径/类别/过街均通过）时只从
这些候选取运营性原因；一个都没有时只报结构性原因。

容量占用按优先级确定：老年聚焦需求优先，其次匿名支持度高者，同序按
需求标识保证结果确定。

## 治理规则

- **审核回避**：`review_coverage` 校验审核人不得是该网点 `applicant_id`，
  申请者不能审核自己的覆盖结论。
- **安全审查独立**：居民意见通过 `REVIEW_REQUESTED` 推动复核（计数随结论
  留痕），但 `safety_passed` 只能由复核人在安全审查中给出。
- **规划变更与验收冻结**：社区边界新版本（`COMMUNITY_REVISED` 的
  `supersedes`）使旧版本上未验收方案失效（`superseded_boundary`）；
  社区通过验收时，验收当时该社区全部需求的结论与覆盖网点随
  `SITE_VERIFIED` 事件固化，之后网点停业、边界再变更都不改判；验收后
  新提报的需求按当前事实正常裁决。
- **隐私**：撤回公开展示同意（`CONSENT_WITHDRAWN`）后清除 `resident_ref`，
  需求事实与汇总支持度保留；`public_summary()` 输出不可反向识别个人。

## 资金

`FundingLedger` 在锁内完成"检查余额 → 扣减承诺"，余额不足原子拒绝。
预算是外部政策需显式登记；服务重启后调用
`service.refresh_funding_from_events()` 从 `FUND_RESERVED` 事件恢复
各批次已承诺总额。

## 验收队列恢复

待核查项 = 已有覆盖结论但未通过证据验收的决定。领取状态持久化在
`<事件日志>.verify.json` 侧车：

- 证据核查未通过 → 任务回队尾，尝试次数保留；
- 进程崩溃时仍标记"核查中"的任务，下次 `rebuild` 自动回到队首
  （核查幂等，至少执行一次安全；状态推进以 `SITE_VERIFIED` 落盘为准）；
- 证据集合为空一律不予受理。

## 命令行

```bash
PYTHONPATH=src python -m community_gap.console events.jsonl explain <need_id>
PYTHONPATH=src python -m community_gap.console events.jsonl gaps
PYTHONPATH=src python -m community_gap.console events.jsonl listing [community_id]
PYTHONPATH=src python -m community_gap.console events.jsonl summary
PYTHONPATH=src python -m community_gap.console events.jsonl queue
```

契约信封的独立校验仍使用：

```bash
PYTHONPATH=src python -m community_gap.cli contracts/domain.schema.json data/sample.json
```

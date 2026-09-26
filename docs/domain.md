# 领域约定

全国已建成9156个一刻钟便民生活圈，建设要求因地制宜、问需于民、补齐业态短板，并用两年左右推进30个试点城市主城区和有条件县城社区覆盖。

本仓库分两层：`contracts/` 定义可稳定交换的基础事实（事件信封与载荷），`src/community_gap/service.py` 在其上实现缺口裁决服务，负责相同事件标识的业务幂等、版本冲突隔离和状态推进。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 聚合对象

- `community_revision`：社区边界版本，含片区划分与步行障碍（如无信号灯主干道及其阻断人群）。
- `service_need`：居民需求及匿名支持度，含片区、需求时段（daytime/night/all_day）与受影响人群。
- `provider_site`：网点，含开店意向、服务半径（社区→片区）、营业时段、服务能力、营业状态（planned/open/suspended）与验收状态。
- `coverage_decision`：覆盖结论，记录裁决时点的边界版本、计入网点与需求量。
- `funding_round`：改造资金批次，含预算总额与已承诺额。

## 事件载荷

- `BOUNDARY_PUBLISHED`：`community_id`, `revision`, `zones`（可含 `barriers`）。
- `NEED_AGGREGATED`：`community_id`, `community_revision`, `category`, `zone`, `time_window`, `support_total`。
- `SITE_PROPOSED`：`applicant_id`, `categories`, `community_id`, `zone`, `service_area`, `capacity`, `opens_at`, `closes_at`。
- `SITE_STATUS_CHANGED`：`status`。
- `FUND_ROUND_OPENED`：`total_budget`；`FUND_RESERVED`：`funding_round`, `amount`。
- `COVERAGE_REVIEWED`：`community_id`, `community_revision`, `category`, `outcome`, `reviewer`, `safety_review_passed`。
- `DECISION_VERIFIED` / `SITE_VERIFIED`：`evidence_set`, `verified_by`。
- `CONSENT_WITHDRAWN`：`resident_ref`。

## 裁决规则

- 需求侧：无需求记录记 `no_demand`；匿名支持度未达阈值记 `observation`；达到阈值才进入供给核查。
- 供给侧：网点须已开放（计划中、暂时停业不计入）、已验收、服务范围覆盖需求片区、步行路径不被阻断（按需求人群匹配障碍）、营业时段完整覆盖需求时段（支持跨零点）。
- 共享容量：跨社区网点按主供网点（剩余容量最大者）扣减共享容量，不得重复计作独立供给；容量不足记 `SHARED_CAPACITY_EXHAUSTED`。
- 居民意见达到阈值只推动复核（置 `review_pending` 并入核查队列），不直接改写结论；记为已覆盖必须安全审查通过且系统核查通过。
- 项目申请者不得审核涉及自己网点的覆盖结论，也不得自行验收网点。
- 规划变更使未验收的方案失效（`stale`，须复核后才能验收）；已验收社区保留当时边界版本与服务依据。
- 资金承诺串行扣减批次余额，并发承诺不得超支。
- 居民撤回公开展示同意后，汇总统计保留；公开视图只给匿名汇总，低于匿名阈值做抑制，不输出任何个人标识。
- 核查任务持久化落盘，验收中断后重开自动把核查中的任务退回待办。
- 最终清单按 planned/open/suspended 区分计划中、已开放、暂时停业的服务。

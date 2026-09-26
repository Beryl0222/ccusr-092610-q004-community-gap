# 领域约定

全国已建成9156个一刻钟便民生活圈，建设要求因地制宜、问需于民、补齐业态短板，并用两年左右推进30个试点城市主城区和有条件县城社区覆盖。

聚合对象包括`community_revision`、`service_need`、`provider_site`、`coverage_decision`。事件类型包括`COMMUNITY_REVISED`、`NEED_AGGREGATED`、`NEED_WITHDRAWN`、`CONSENT_WITHDRAWN`、`REVIEW_REQUESTED`、`SITE_PROPOSED`、`SITE_STATUS_CHANGED`、`FUND_RESERVED`、`COVERAGE_REVIEWED`、`SITE_VERIFIED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `COMMUNITY_REVISED`：载荷还需包含 `community_id`, `revision_no`, `service_radius_km`；`supersedes` 指向前一边界版本。
- `NEED_AGGREGATED`：载荷还需包含 `community_revision`, `category`。
- `FUND_RESERVED`：载荷还需包含 `funding_round`, `amount`。
- `SITE_PROPOSED`：载荷还需包含 `applicant_id`, `categories`, `location_km`, `serves_communities`；`capacity` 为跨社区去重容量，`approaches` 携带分社区步勘距离与过街障碍信号化情况。
- `COVERAGE_REVIEWED`：载荷还需包含 `community_revision`, `need_id`, `reviewer_id`, `applicant_id`, `category`；`reviewer_id` 不得等于所审网点的 `applicant_id`。
- `SITE_VERIFIED`：载荷还需包含 `evidence_set`, `verified_by`；社区验收通过时载荷同时固化 `frozen_outcomes`（验收当时各需求结论与覆盖网点）。

`NEED_WITHDRAWN`、`CONSENT_WITHDRAWN`、`REVIEW_REQUESTED`、`SITE_STATUS_CHANGED` 无额外必填载荷字段。撤回同意只关闭公开标识并清除可识别字段，需求事实与汇总统计保留。

事件语义（验收冻结、缺口原因码、队列恢复等）与服务层规则见 `docs/service.md`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。

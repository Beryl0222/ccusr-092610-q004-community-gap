# 领域约定

全国已建成9156个一刻钟便民生活圈，建设要求因地制宜、问需于民、补齐业态短板，并用两年左右推进30个试点城市主城区和有条件县城社区覆盖。

聚合对象包括`community_revision`、`service_need`、`provider_site`、`coverage_decision`。事件类型包括`NEED_AGGREGATED`、`SITE_PROPOSED`、`FUND_RESERVED`、`COVERAGE_REVIEWED`、`SITE_VERIFIED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `NEED_AGGREGATED`：载荷还需包含 `community_revision`, `category`。
- `FUND_RESERVED`：载荷还需包含 `funding_round`, `amount`。
- `SITE_VERIFIED`：载荷还需包含 `evidence_set`, `verified_by`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。

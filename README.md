# 社区便民服务缺口裁决台

全国已建成9156个一刻钟便民生活圈，建设要求因地制宜、问需于民、补齐业态短板，并用两年左右推进30个试点城市主城区和有条件县城社区覆盖。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/community_gap/`：契约校验、事件存储、缺口裁决引擎、资金账本、验收队列与命令行。
- `tests/`：契约、裁决规则、治理规则、资金并发与队列恢复测试。
- `docs/domain.md`：领域对象与事件语义。
- `docs/service.md`：裁决过闸规则、治理规则与服务用法。

## 缺口裁决

服务把社区边界版本、居民需求与匿名支持度、步行可达条件、网点营业时段、
服务类别、改造资金批次与验收证据整合到只追加事件日志上，回答"某片区
为什么仍是缺口"：

- 半径、类别、老年过街障碍、计划中/停业状态、夜间营业时段逐项核查，
  不以覆盖数量掩盖短板，不把未营业网点算作已覆盖；
- 跨社区网点按容量去重，不重复计作独立供给；
- 申请者不得审核自己的结论，居民意见推动复核但不替代安全审查；
- 规划变更只影响未验收方案，已验收社区冻结当时边界与服务依据；
- 撤回公开同意后清除个人标识，汇总统计保留；
- 验收核查队列可在中断/崩溃后恢复；最终清单区分计划中、已开放、暂时停业。

```bash
PYTHONPATH=src python -m community_gap.console events.jsonl explain <need_id>
PYTHONPATH=src python -m community_gap.console events.jsonl gaps
PYTHONPATH=src python -m community_gap.console events.jsonl listing
PYTHONPATH=src python -m community_gap.console events.jsonl summary
PYTHONPATH=src python -m community_gap.console events.jsonl queue
```

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m community_gap.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

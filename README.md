# 社区便民服务缺口裁决台

全国已建成9156个一刻钟便民生活圈，建设要求因地制宜、问需于民、补齐业态短板，并用两年左右推进30个试点城市主城区和有条件县城社区覆盖。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/community_gap/`：契约校验、缺口裁决服务与命令行入口。
  - `contracts.py`：事件信封与载荷的基础校验。
  - `service.py`：缺口裁决服务（评估解释、复核、验收、资金、隐私视图）。
  - `models.py` / `store.py` / `queue.py` / `errors.py`：领域模型、幂等事件存储、可恢复核查队列、业务错误。
- `tests/`：契约、裁决、工作流、资金并发、隐私与队列恢复测试。
- `examples/pilot_scenario.py`：县城社区扩围的端到端示例。
- `docs/domain.md`：领域对象、事件语义与裁决规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests examples
```

## 样例校验

```bash
PYTHONPATH=src python3 -m community_gap.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 端到端示例

```bash
PYTHONPATH=src python3 examples/pilot_scenario.py
```

演示：边界版本发布、需求聚合、开店意向、缺口原因解释（步行受阻、营业时段不足）、安全审查与职责分离、资金批次余额控制、撤回同意后的匿名汇总、规划变更对未验收方案的影响、核查队列中断恢复，以及区分计划中/已开放/暂时停业的最终清单。

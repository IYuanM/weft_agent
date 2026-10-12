# 模块一验收记录

验收日期：2026-10-12。环境：Windows，Python 3.12.5。离线执行，无付费模型调用。

## 结果

| 验收项目 | 结果 | 证据 |
| --- | --- | --- |
| 全量回归 | 106 / 106 通过 | `python -m unittest discover -s tests -v` |
| 模块一新增测试 | 25 / 25 通过（包含在全量中） | `tests/test_execution_foundations.py` |
| 状态转换矩阵 | 100 个组合均符合独立规格 | `ContractTests.test_transition_matrix_matches_independent_specification` |
| 旧 SQLite 数据迁移 | 对话、摘要保留；重复打开不重复迁移 | `MigrationTests` |
| 迁移故障注入 | DDL 与版本记录一起回滚 | `MigrationTests.test_failed_migration_rolls_back_ddl_and_version` |
| 状态故障注入 | run 更新失败，attempt 插入回滚 | `RepositoryCase.test_transaction_rolls_back_attempt_if_run_update_fails` |
| 启动提交期间取消 | 已提交 attempt 最终结束为 cancelled | `EmbeddedLifecycleTests.test_cancel_during_database_start_closes_committed_attempt` |
| CLI 冒烟 | 真实子进程退出 0、生成报告、保存对话及终态，事件身份一致 | `CLIIntegrationTests.test_real_cli_demo_persists_identity_and_artifact` |
| 多进程重复试验 | 4 个独立进程，10 轮，70 / 70 项检查通过 | [JSON 报告](../artifacts/module1-evaluation.json) |

多进程评测运行耗时约 4.00 秒，仅作为本机记录，不是性能承诺。每轮检查：提交唯一胜者、启动唯一胜者、请求冲突拒绝、旧版本拒绝、暂停恢复身份、新 run 操作身份区分、事务提交前硬退出回滚。使用 spawn 和进程屏障，没有用线程模拟跨进程竞争。

## 复现

```powershell
python -m unittest discover -s tests -v
python -m evals.module1 --rounds 10 --workers 4 --output artifacts/module1-evaluation.json
```

所有运行工作区和数据库均为临时目录，不修改已有 `.state`、`workspace` 或 `.env`。评测输出 JSON 是指定的仓库产物。

## 交付边界

本模块验证持久身份、状态机、提交去重、版本冲突与 SQLite 事务原子性。Checkpoint/EventEnvelope 仅为版本化契约，尚无中途执行恢复、持久事件日志、工具账本、副作用对账、租约接管或业务 Verifier。Repository 暂停恢复的评测不等于 Agent 能从中间步骤继续。

架构依据与 API 语义见 [EXECUTION_FOUNDATIONS.md](EXECUTION_FOUNDATIONS.md)。不开展模块二，等待本模块评审。

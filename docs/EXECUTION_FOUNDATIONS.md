# 模块一：执行身份、状态机与存储契约

## 范围与结构

新增 `weft` 包承载运行时领域与持久化边界，不将数据库状态机塞入模型/工具循环。

```text
weft/
  domain.py                 版本化请求、身份、状态机、事件和 Checkpoint 契约
  storage/contracts.py      ExecutionRepository / ConversationRepository 协议
  storage/migrations.py     事务迁移与 SQLite 事务工具
  storage/sqlite.py         执行生命周期 Repository
evals/module1.py             独立进程竞争与事务崩溃评测
tests/test_execution_foundations.py
```

`baseline` 保留当前 Agent、工具、上下文和沙箱。Agent 通过 Repository 协议接入生命周期，可注入替代实现。CLI 自动记录 task/run/attempt，并输出身份。`pyproject.toml` 声明 Python 3.12 和两个 Python 包，运行依赖为空；技能、示例和 CLI 仍按源码目录使用。

## 开源参考与取舍

| 来源 | 借鉴 | 本项目的取舍 |
| --- | --- | --- |
| [Temporal 执行身份](https://docs.temporal.io/workflow-execution/workflowid-runid) | 业务身份与执行身份分离；明确重试与新执行 | task 为工作请求，run 为逻辑执行，attempt 为实际尝试，不是 Temporal 名称的一对一复制 |
| [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | Checkpoint 与一般业务存储分离 | 对话快照和生命周期分库；本模块只定义 Checkpoint 信封，下一模块实现快照内容与恢复 |
| [DBOS Python](https://github.com/DBOS-inc/dbos-transact-py) | 数据库支撑持久工作流 | 使用显式 Repository 和事务边界，未引入工作流引擎 |
| [SQLite transaction](https://www.sqlite.org/lang_transaction.html) | 原子提交与写事务竞争 | BEGIN IMMEDIATE 串行化短事务，真实多进程测试；后续替换 PostgreSQL 适配器 |

这是架构参考，未复制上游源码，运行时不依赖这些框架。

## 身份与提交

- thread_id：上下文归属，允许多个任务；本模块不提供会话级并发互斥。
- task_id：不可变的请求快照，包含输入、workspace、configuration、acceptance 和版本。
- run_id：逻辑执行，暂停或中断后的状态机恢复保留此 ID。
- attempt_id：一次尝试，记录 owner、序号与起止时间，每次重新执行分配新 ID。
- operation_id(run_id, logical_key)：跨 attempt 稳定的工具操作身份；相同参数的有意重复必须有不同 logical_key。不依赖模型生成的 tool_call_id。

提交幂等键作用域为 `(thread_id, idempotency_key)`。规范 JSON 快照忽略对象字段顺序，但输入、工作区、配置或验收要求变化都会冲突。无键提交每次创建新任务。嵌入式调用方不得把密钥放入 configuration。

同键同请求返回原 task 和初始 run，即使该 run 已完成或已有后继；提交重试不触发重新执行。`rerun(task_id, previous_run_id)` 显式创建同 task 的新 run，要求前一 run 已终态且没有后继。新 run 使用新的操作身份。

## 状态转换

| 当前状态 | 允许目标 |
| --- | --- |
| queued | running（只能通过 start_attempt）、paused、cancelled |
| running | pause_requested、cancel_requested、interrupted、succeeded、failed、cancelled、timed_out |
| pause_requested | paused、cancel_requested、interrupted、succeeded、failed、cancelled、timed_out |
| cancel_requested | cancelled、interrupted |
| paused | queued、cancelled |
| interrupted | queued、cancelled；有持久取消标志时禁止 queued |
| succeeded / failed / cancelled / timed_out | 无，终态不可修改 |

每次更新要求 expected_version，成功后递增。结束活动 attempt 还要求当前 attempt_id；run 更新与 attempt 结束在同一事务。开始 attempt 与 run 更新也在同一事务，并有唯一活动 attempt 约束。owner_id 是归属记录，不是鉴权凭证。

暂停请求需在安全边界确认，允许已经完成的执行提交成功。取消请求一旦提交，禁止成功提交。取消不代表外部操作没有发生。超时是 run 的终态，与工具副作用 unknown 是不同维度，后者将在账本模块持久化。

interrupted 只是可恢复状态的表达。本模块没有进程死亡探测、租约回收或自动接管；真实进程在提交 running 后死亡会保留 running，后续模块负责有依据地将其标记 interrupted。

## CLI 兼容

现有 Agent.arun() 每次创建 task/run/attempt，映射退出状态：completed → succeeded；cancelled → cancelled；timeout → timed_out；其他退出原因 → failed，并在 reason 保留原错误码。succeeded 表示循环完成，不代表业务 Verifier 验收通过。

main.py 传入真实工作区。直接构造 Agent 时可显式传 workspace；未传时记录 unspecified，不推断工具隐含访问范围。

本模块不向 CLI 暴露幂等提交、pause/resume/rerun 命令，避免没有 Checkpoint 和账本时重跑副作用。Repository 方法用于状态机验收及后续集成。外部控制尚未接入运行中的 Agent；Repository 状态请求不代表当前工具已停止。

## 存储与迁移

- threads.sqlite3 保存原有完整对话与摘要，升级仅添加 schema_migrations，原表和记录保留。
- executions.sqlite3 保存 tasks、runs、attempts 和 schema_migrations。
- 两库独立事务，对话保存与 run 终态尚无跨库原子性，不能据此恢复执行；下一模块需统一执行状态、结果与事件的提交边界。
- 迁移正向、连续、事务执行，拒绝未来版本与不连续版本；失败回滚 DDL 和版本记录。
- 连接启用外键。当前读取也使用 BEGIN IMMEDIATE 事务助手，优先保证一致性，不宣称高并发服务性能。
- JSONL 仍是观察导出，无持久事件或工具账本。

任务、事件与 Checkpoint 声明 schema_version=1。Checkpoint 携带 task/run/attempt、event_sequence、next_action、state_json 和 runtime_version。state_json 目前只校验合法 JSON 对象；内部恢复字段和升级规则将在模块二落实。进程内 monotonic 截止值不能作为跨进程时间保存。

## 验收与评测

```powershell
python -m unittest discover -s tests -v
python -m evals.module1 --rounds 10 --workers 4 --output artifacts/module1-evaluation.json
```

模块测试覆盖全部 100 个状态组合、非法请求与版本、幂等快照比较、独立连接竞争、旧库保留、迁移回滚、状态原子性、旧 attempt 拒绝、暂停恢复身份、取消持久性及 CLI 生命周期。

启动取消专项测试在 start_attempt 提交后阻塞，再取消调用方：运行时等待提交结果，获取身份并结束 attempt，之后才传播 CancelledError。

离线评测每轮用 4 个独立进程同步竞争提交和启动；另外检查参数冲突、旧版本、暂停恢复身份、新 run 操作身份、子进程在事务提交前硬退出后的回滚。10 轮共 70 项检查。报告带环境、耗时和能力边界，不调用模型。硬退出验证数据库事务原子性，不是外部副作用对账。

真实 CLI 冒烟使用临时工作区：离线 demo 后检查 report.md、持久终态、attempt 结束与事件身份，不操作现有 .state 和 workspace。当前验收结果见 [MODULE1_ACCEPTANCE.md](MODULE1_ACCEPTANCE.md)。

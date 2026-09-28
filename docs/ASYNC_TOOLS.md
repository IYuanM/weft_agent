# 异步工具执行与资源锁

本文对应 `baseline/tools/concurrency.py`、`tools/executor.py` 和 `core/agent.py`。
当前目标是本机、单进程、单事件循环中的受控工具并发。容器隔离和分布式锁尚未实现。

## 1. 从哪进入

```text
Agent.run()                         同步 CLI 兼容入口
  └─ asyncio.run(Agent.arun())
        ├─ 模型与摘要请求            同步适配器在线程中运行
        └─ ToolExecutor.execute_batch()
              ├─ 只读并发批次
              └─ 写入/非并发工具屏障
                    └─ aexecute()
                         校验 → 解析资源 → 等待资源锁 → 等待容量
                              → 再次授权 → 调用 handler → 结果与事件 → 释放
```

在已经运行的事件循环里使用 `await agent.arun(...)`、`await executor.aexecute(...)`。
不要调用 `run()` 或 `execute()`，同步入口会明确报错，避免嵌套 `asyncio.run()`。
模型适配器目前仍是同步 HTTP 客户端；线程卸载让事件循环可响应，但不等于网络请求可强制取消。

## 2. 哪些工具可以并发

只有同时满足 `read_only=True` 和 `parallel_safe=True` 的工具才进入并发批次。
`parallel_safe` 默认关闭，由受信任开发者声明。它表示工具调用之间没有隐藏的数据依赖；
模型不能自行解除这个限制。内置 `list_files`、`read_file` 已开启。

同一模型响应中的调用按原顺序分组。例如：

```text
read A, read B, write C, read D, read E
   └─并发────┘     │       └─并发────┘
              等前组完成；独立执行
```

写入和任何未声明 parallel_safe 的工具是屏障：等待前面的组全部结束，自己完成后才放行下一组。
这是保守的依赖策略，不尝试让不同资源的写入在同一批里并发。
如果两个只读调用也存在依赖，应把工具声明为不允许并发，或者让模型分轮调用。

每批结果始终按模型原始调用顺序回填，`tool_call_id` 保持不变。
工具完成事件可以交错，完成顺序不影响上下文中的工具消息配对。
某工具业务失败作为 ToolResult 回填，不丢弃同组其他工具的成功结果。

## 3. 两层容量和一层锁

| 机制 | 默认值 | 范围 |
|---|---|---|
| `ToolExecutor.max_parallel` | 3 | 一个 execute_batch 内每组任务数量 |
| `Admission.capacity` | 4 | 共享同一 Admission 实例的所有执行器/运行 |
| 资源锁 | 同 key 互斥 | 共享同一 Admission 的同一规范化资源 |

等待资源锁时不占用容量槽，避免大量等待同一热资源的任务占满所有槽位。
锁覆盖真实执行、内部重试、写入去重和副作用状态记录；最后一个使用者退出时清理锁表。
当前同资源读取也互斥，尚未实现多读单写锁。

资源 key 由 `ToolSpec.authorize(args, context)` 返回，执行前再次解析且必须保持一致。
内置文件工具使用已解析的绝对路径，并在 Windows 上规范化大小写。
审计只记录 key 的哈希，不输出宿主机绝对路径。

**锁只比较完全相同的 key**：目录与子文件、硬链接别名、不同服务对同一对象的不同命名，
不会自动识别为冲突。目录快照等需要更大锁范围的操作，应约定共同资源 key。
一个工具目前只占一个资源锁，不支持多资源事务或锁集合。

同批中的顺序由屏障保证；不同 run 对同一资源由锁串行，顺序取决于到达锁的时间。
不同 run 对不同资源的写入仍可能并发，这是共享容量允许的行为。

## 4. 如何共享执行边界

```python
from baseline.tools.concurrency import Admission
from baseline.tools.executor import ToolExecutor

admission = Admission(capacity=4)
executor_a = ToolExecutor(registry_a, admission=admission, max_parallel=3)
executor_b = ToolExecutor(registry_b, admission=admission, max_parallel=2)
# 在同一个事件循环中 await 调用它们。
```

不传 Admission 时，每个执行器创建自己的实例。因此它不是隐式的进程全局限制。
服务应在启动时创建并共享实例，不能为每个请求创建一套互不相干的锁。
执行边界绑定到一个事件循环；仍有任务时禁止跨循环复用。空闲后允许同步 CLI 下次调用重新绑定。
这些锁不会协调外部进程、其他应用实例或用户直接编辑文件，也不替代数据库事务或沙箱。
同一会话的多个 arun 仍应由调用方串行提交，当前没有会话级互斥和跨实例锁。

## 5. 超时与取消的真实语义

工具期限包含资源锁等待、容量等待、执行和重试，总期限不超过 run 截止时间。
排队期间检查取消与期限；已经取消的任务不会进入 handler。

| 类型 | 收到取消/超时后的行为 |
|---|---|
| 原生 `async def` handler | 取消子任务，等待其 finally 清理后释放锁 |
| 同步 handler | 在线程中执行；等待线程真正退出后才释放锁与容量 |
| 已排队任务 | 返回 cancelled/timeout，不执行 handler |
| 调用方 `task.cancel()` | 清理并等待本批所有子任务，再向调用方传播 CancelledError |

原生协程必须遵守取消协议：不要吞掉 `CancelledError` 后无限运行，不要在事件循环里做阻塞 I/O。
同步线程无法被 Python 安全强杀：如果底层永久阻塞，这次调用也可能长时间不能退出。
因此普通同步工具超时不是严格墙钟保证。需要终止外部程序时，应使用已接入的
[进程/容器沙箱](SANDBOX.md)，由后端终止进程并清理后代。

通过 RunContext.cancel 发起停止时，已经完成的同步写入可以返回 cancelled/timeout，
同时保留 `effect=confirmed` 和产物引用。取消不代表操作没有发生。
被中断且无法确认结果的写入标为 unknown，当前 run 中同资源后续写入被阻止。
外部任务取消采用保守副作用判断，不能承诺 exactly-once 或事务回滚。

## 6. 如何添加异步工具

```python
from baseline.tools.contracts import ToolResult, ToolSpec

async def fetch(args, context):
    # 在这里 await 一个原生异步客户端；下面仅示意结果结构。
    return ToolResult(True, content={"text": "result"})

spec = ToolSpec(
    name="fetch", description="读取一个对象",
    parameters=(("path", "对象标识"),),
    handler=fetch,
    authorize=lambda args, context: "object:" + args["path"],
    read_only=True, idempotent=True, parallel_safe=True,
    timeout_seconds=10,
)
```

`authorize` 当前是同步且必须快速完成的策略函数；不要在其中执行远程网络请求。
同步 handler 会自动在线程里执行，因此实现必须适应多线程调用，不要使用未保护的共享可变状态。
异步函数、异步绑定方法和带 async `__call__` 的对象均可作为 handler。
所有业务异常仍经执行器转为结构化结果，安全重试规则没有改变。

## 7. 事件与验证

新增 `tool.queued` 事件，表示通过参数和首次授权检查，准备等待锁/容量。
`tool.attempt` 表示已获准实际执行。两者的时间差可以定位排队，`tool.end` 包含排队在内的总耗时。
事件关联使用 run_id/tool_call_id，不要假设相邻日志必定属于同一工具。

```powershell
python main.py '读取工作区里的两个文件' --tool-concurrency 3 --tool-capacity 4
python -m unittest discover -s tests -p test_async_tools.py -v
python -m unittest discover -s tests -v
```

测试用受控异步事件验证真实重叠、共享容量上限、同资源互斥、写屏障、结果顺序、
局部失败、排队超时、原生任务取消、同步写线程排空、外部取消后的任务与锁清理。
不依赖模型恰好生成并行工具调用，也不需要付费 API。

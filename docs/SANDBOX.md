# 进程与容器沙箱

代码位于 `baseline/sandbox/`，通过 `Sandbox.run_python(code, context)` 接口接入。
第一版专注 Python 脚本，不提供任意 shell 命令，命令行参数始终使用列表且 shell=False。

## 启用方式

默认 `--sandbox off`，模型只看到原有文件工具。

```powershell
# 可信本机开发：可访问宿主机，不用于运行不可信代码
python main.py '用 Python 计算 1 到 100 的和' --sandbox local

# 容器隔离：需先安装并启动 Docker，以及自行准备镜像
docker pull python:3.12-slim
python main.py '用 Python 分析工作区数据' --sandbox docker --sandbox-image python:3.12-slim
```

运行时使用 `--pull=never`，不会隐式下载镜像，也不会在 Docker 不可用时回退到 local。
镜像由应用配置指定，不由模型指定。正式部署建议配置经审查的镜像 digest。
模型得到 `run_python` 工具，参数只有 `code`。脚本与文件工具共用工作区级锁：
启用沙箱后，同工作区文件操作也串行，避免脚本间接访问多个文件时绕过文件级互斥。
多个执行器需要共用 Admission，进程外访问仍不受此锁管理。

## 两种后端的边界

| 能力 | local | docker |
|---|---|---|
| 工作目录 | 指定工作区 | 容器内 /workspace |
| 文件系统隔离 | 无，脚本可访问宿主机其他路径 | 只挂载工作区，容器根目录只读 |
| 网络隔离 | 无 | network=none |
| 环境变量 | 最小白名单，不继承 API key | 不把宿主模型密钥传入容器 |
| 进程管理 | Windows Job / POSIX process group | Docker 容器及其进程树 |
| 内存/进程数 | Windows Job 限制；POSIX local 暂无同等配额 | memory、memory-swap、pids-limit |
| CPU 配额 | 暂无 | cpus |
| 超时/取消 | 终止进程树并等待清理 | 终止 CLI 后强制移除容器 |

**local 是进程生命周期管理，不是安全隔离。** 去掉环境变量不等于脚本无法读取磁盘上的 `.env`，
也不阻止网络访问。POSIX 不可信程序还可能通过创建新会话逃离进程组，所以 local 仅用于可信开发。
工作区含敏感文件时，容器挂载仍会把这些文件交给代码；不要将项目根目录作为不可信代码的工作区。

## 默认限制

`SandboxLimits`：执行 10 秒、合并 stdout/stderr 最多 16000 字节、代码最多 32000 字节、
Docker/Windows Job 内存 256 MiB、最多 32 个进程、Docker CPU 配额 1。
`--sandbox-timeout` 可改变执行超时；每次执行同时受 run 与 ToolSpec 截止时间约束。
Docker 清理使用独立的 8 秒超时，结束调用可能比执行期限略晚。
输出超过上限时终止任务，返回有界前缀和 `truncated=true`，不会无限读入内存或写容器日志。
当前没有工作区磁盘配额；脚本仍可能写入大量文件。

## 生命周期

### local

1. 使用当前 Python 解释器，以 `-I -u` 启动等待 stdin 的引导程序。
2. Windows 将进程加入带 kill-on-close 的 Job，再发送代码；Job 建立失败时拒绝执行。
3. POSIX 使用独立进程组；独立线程持续排空输出与输入，主线程监视期限、取消和输出量。
4. 正常退出也清理后代，防止脚本启动后台任务后返回。
5. 异步调用被取消时，先请求子进程停止，等待清理完成才释放工具资源锁。

### docker

1. 分配随机 `agent-baseline-<uuid>` 名称，添加 `agent-baseline.sandbox=true` 标签。
2. `docker create` 创建容器，确认成功后才 `docker start -a -i` 并经 stdin 发送代码。
3. 使用非 root 用户、cap-drop=ALL、no-new-privileges、只读根目录、禁网、资源限制和有界 /tmp。
4. 无论正常、超时、取消或错误，均尝试 `docker rm -f <本次名称>`。
5. 清理失败，或 create 超时导致 daemon 状态不确定，标记当前实例 quarantined，拒绝继续执行脚本。

CLI 的 HOME/USERPROFILE 仅供 Docker 读取宿主配置使用，不通过 `-e` 传入容器。
当前使用操作者的 Docker 配置；应使用本地 Linux-container daemon，不支持远程工作区挂载。
Linux 宿主的工作区需对指定非 root UID 可写；实现不会自动放宽用户真实目录的权限。

## 错误与副作用

结果包括 `exit_code/stdout/stderr/truncated/cleanup_ok`，错误码例如：

- `sandbox_unavailable`：找不到执行后端。
- `sandbox_start_failed`：无法建立本地进程管理。
- `execution_failed`：非零退出，检查有限长度的 stderr。
- `timeout/cancelled/output_limit`：执行被停止。
- `cleanup_failed/sandbox_quarantined`：需操作员检查遗留资源。

脚本可以修改文件，所以工具声明为非只读、非幂等，绝不自动重试。
零退出表示执行结束，不证明业务结果正确；失败后的文件修改不回滚，副作用保守标记 unknown。
脚本 stdout 也是不可信数据。代码/完整输出不会写入工具事件，但会出现在对话存储中。

清理失败后不要直接重新创建实例继续运行；先查看标签对应的容器，确认归属再移除：

```powershell
docker ps -a --filter label=agent-baseline.sandbox=true
```

当前没有守护进程定期回收；宿主进程崩溃、daemon 不可用时可能需要人工处理遗留容器。
也没有磁盘配额、跨实例资源隔离、容器逃逸防护评测或崩溃事务恢复，不能据此宣称企业级隔离完备。

## 验证

```powershell
python -m unittest discover -s tests -p test_sandbox.py -v
python smoke_sandbox.py --provider local
python smoke_sandbox.py --provider docker --image python:3.12-slim
```

单元测试真实启动 local 子进程，检查文件产出、密钥不继承、输出洪泛、超时、取消和后代清理。
Docker 构造参数与生命周期使用模拟测试，不冒充真实隔离测试。
Docker smoke 必须在有 daemon 和镜像的环境手动运行，检查非 root、根目录只读、输出、挂载写入和超时回收。
禁网、内存和 CPU 配额还需在目标部署环境进行进一步验收。

参考：[Docker 容器运行参数](https://docs.docker.com/engine/containers/run/)、
[Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。

# Weft-Agent

Weft-Agent 是一个 Python 通用 Agent 框架，以即插即用的企业级 Agent 架构为目标，围绕模型接入、工具执行、上下文管理和运行追踪提供可组合的基础能力。当前版本提供 CLI 入口和可扩展的单 Agent 执行引擎。

## 快速开始

### 1. 获取项目

需要 Python 3.12+，核心运行仅使用 Python 标准库，无需安装额外依赖。

```powershell
git clone https://github.com/IYuanM/weft_agent.git
cd weft_agent
```

### 2. 运行离线演示

```powershell
python main.py --demo --thread demo
```

无需 API Key。演示会创建并读取 `workspace/hello.txt`，生成 `workspace/report.md`，终端同步输出运行事件。文件工具默认不覆盖已有文件；再次体验完整流程时，可用 `--workspace workspace/demo2` 指定新的工作区。

### 3. 接入模型

将 `.env.example` 复制为 `.env`：

```powershell
Copy-Item .env.example .env
```

macOS / Linux 使用 `cp .env.example .env`。填写支持 Chat Completions 和 function tools 的模型配置：

```dotenv
MODEL_BASE_URL=https://api.deepseek.com/v1
MODEL_NAME=deepseek-chat
MODEL_API_KEY=your-api-key
```

在工作区放入需要处理的文件，然后运行：

```powershell
python main.py '列出工作区文件' --thread work
python main.py '根据刚才的文件列表，说明有哪些可处理的材料' --thread work
```

相同 `--thread` 会恢复已完成的对话。CLI 自动读取项目根目录的 `.env`，进程环境变量优先；调用真实模型会产生相应 API 用量。

### 4. 调整运行参数

```powershell
python main.py '列出工作区文件' --thread work --workspace workspace --max-steps 12 --max-seconds 180
python main.py --help
```

默认文件工作区为项目下的 `workspace/`，会话和事件存储在 `.state/`。这两个目录及 `.env` 均已加入 Git 忽略规则。

需要 Python 代码执行时，可显式启用 Docker 后端。先安装并启动 Docker、准备镜像，再运行：

```powershell
docker pull python:3.12-slim
python main.py '用 Python 计算 1 到 100 的和' --sandbox docker
```

也可通过 `--sandbox local` 使用本地进程后端；该模式仅适用于可信代码，不隔离宿主文件系统与网络。后端配置见 [沙箱使用说明](docs/SANDBOX.md)。

## 核心特性

- **模块化装配**：模型、工具注册表、执行器、会话存储与 Agent 主循环解耦，可替换模型实现并接入业务工具。
- **统一工具执行**：通过 `ToolSpec` 声明工具，由执行器统一处理参数校验、资源访问策略、调用预算、受控重试和结构化结果；内置工作区文件列举、读取与写入工具。
- **异步与并发控制**：独立只读工具可并发执行，结合共享容量限制、资源锁和写操作屏障协调访问。详见 [异步工具与资源锁](docs/ASYNC_TOOLS.md)。
- **会话持久化**：使用 SQLite 保存成功完成的对话和上下文摘要，通过会话标识恢复多轮任务。
- **上下文管理**：根据估算预算触发摘要压缩，保留最近完整消息块、最新请求与产物引用，控制长对话输入规模。
- **运行约束与循环检测**：支持步数、时间、工具调用预算和取消处理，检测重复调用与重复失败，进行纠偏或终止。
- **技能按需加载**：发现技能元数据，根据任务加载技能指令；内置代码文档与本地材料简报技能。
- **可选代码执行后端**：支持本地进程与 Docker 后端，显式开启 Python 执行能力，并配置执行超时等限制。
- **可观察的执行过程**：终端和 JSONL 事件记录模型调用、工具执行、上下文压缩与结束状态，通过会话、运行和工具调用标识关联追踪。

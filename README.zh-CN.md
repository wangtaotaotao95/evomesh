# EvoMesh

**一个实例积累经验，整个 Agent 持续进化。**

既是开箱可用的个人助手，也是可直接嵌入应用的 Agent 框架。Python 3.11+ · SQLite / MySQL · 多实例 · MIT

[English](README.md) · [框架指南](docs/framework.md) · [能力对齐表](docs/alignment.md) · [部署](docs/deployment.md)

Agent 的不流畅可能来自 skill、tool、soul、ReAct 循环或任意源码。EvoMesh 把这些问题放进同一条进化链路：记录阻碍、读取源码、生成提案、多实例共享记录、定期验证合并、按版本升级。

**个人助手与开发框架共用 `AgentApp`，不是两个分叉产品。** 记忆、会话、工具、后台任务、定时任务、委派和进化都可以通过 Python API 复用。

当前为 **v0.5 alpha**。本版增加交互式 Chromium 浏览器、Python/JavaScript 执行、文件搜索/精确编辑，以及不依赖报错的主动源码检查和可选自动验证集成；不宣称已经完整达到 Hermes/OpenMuse 的产品成熟度。浏览器接管、移动端、邮件日历渠道、模型 token 流式输出和自动集群发布等仍有差距，详见[逐项对齐表](docs/alignment.md)。

## 安装后，两种用法

```bash
git clone https://github.com/wangtaotaotao95/evomesh.git
cd evomesh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,mysql,mcp]'
```

**个人助手：**

```bash
evomesh new ../my-assistant --template assistant
cd ../my-assistant
evomesh console  # 打开 http://127.0.0.1:8765，用终端令牌登录
# 或 evomesh chat --session main
```

**开箱即用的框架项目：**

```bash
evomesh new ../my-agent --template framework
cd ../my-agent
python app.py
```

两个模板都能直接运行，不需要 API Key。默认是确定性的离线演示，只演示工具与运行流程，不冒充真实大模型。启用真实推理时，把 `evomesh.toml` 的 `demo` 改为 `false`，再配置：

```bash
export EVOMESH_MODEL='你的工具调用模型'
export EVOMESH_BASE_URL='https://你的服务商/v1'
export EVOMESH_API_KEY='你的密钥'
evomesh chat --session main
```

CLI 自动读取当前目录的 `evomesh.toml`，也支持 `--config`。其中相对路径以配置文件目录为基准，密钥只放环境变量。

## 本版能力

| 能力 | 个人助手入口 | 框架入口 |
|---|---|---|
| 持久会话 | `chat --session main`，退出后可继续 | `app.run(..., session=...)` |
| 个人记忆 | remember / recall / forget 工具；`memory` 命令 | `app.state` 记忆接口 |
| 后台工作 | `task submit/list/show/events/cancel/retry` | `app.submit()` / `app.work_once()` |
| 定时任务 | `schedule add --every 秒数` | SQL 原子调度与任务入队 |
| 执行过程 | `run --events`、任务事件记录 | 事件回调、取消检查、工具调用策略 |
| 工具扩展 | 配置 Python 插件和 MCP 服务 | `@tool`、`app.register()` |
| 终端 | 显式开启 local / docker | 可替换终端工具 |
| 浏览器 | open / snapshot / click / fill / back / screenshot | 每次任务独立的 `BrowserSession` |
| 代码执行 | `execute_code`：Python、local 模式下的 Node.js | 与 Terminal 共用 local / docker 后端 |
| 文件操作 | `read_file_chunk` / `search_files` / `edit_file` | 分块读取与版本校验、文件行号证据、唯一匹配编辑 |
| 主动进化 | 空闲检查或独立 `evolution-worker` 周期性检查源码 | `app.evolve_once()` / `ProactiveEvolution.run()`、SQL 租约、可选自动验证合并 |
| 网页读取 | 配置允许访问的 HTTPS 域名 | 文本提取工具；不等于交互浏览器 |
| 多 Agent 委派 | 给协调 Agent 注册委派工具 | `Team.run()` / `Team.as_tool()` |
| 多实例 | API 副本、独立任务进程、共享 MySQL | 相同运行时、租约和命名空间 |
| 全栈进化 | reflect / worker / upgrade | 源码提案、验证集成接口 |

## 框架不是“只给几个基类”

```python
from evomesh import AgentApp, tool


@tool
def word_count(text: str) -> int:
    """统计单词数量。"""
    return len(text.split())


with AgentApp.from_config("evomesh.toml", tools=[word_count]) as app:
    app.state.remember("tone", "回答尽量简洁")
    answer = app.run("整理我的工作区笔记", session="main")
    task_id = app.submit("列出工作区文件", key="list-1")
    app.work_once()
    print(app.state.task(task_id))
```

`AgentApp` 内置配置、会话存储、工具、任务、调度和记忆；`Agent` 保留为轻量的进程内运行循环。模型可替换，也可用 `await app.arun(...)` 接入异步服务。详见[框架指南](docs/framework.md)和[完整离线示例](examples/framework_app.py)。

## 长时间任务与定时任务

```bash
evomesh task submit '整理工作区笔记' --session notes --key notes-1
evomesh task-worker              # 单独开一个终端保持运行。
evomesh task list
evomesh task events TASK_ID
evomesh task cancel TASK_ID
evomesh task retry TASK_ID
evomesh schedule add '检查工作区笔记' --every 3600
```

取消在模型调用和工具调用之间生效，不能撤销已经发出的外部动作。崩溃失联的任务标为 `interrupted`，需要检查先前动作后显式重试。定时任务依赖运行中的 task-worker，多实例通过数据库事务避免重复入队，错过多个周期时合并为一次执行。

## MCP 与能力配置

```toml
[evomesh]
demo = false
model = "你的模型"
base_url = "https://你的服务商/v1"
terminal = "disabled" # 按需改为 local 或 docker。
allowed_hosts = ["example.com"]

[evomesh.mcp_servers]
project_tools = ["python", "/绝对路径/server.py"]
```

MCP 当前支持可信 stdio 服务的工具发现与调用，并维护服务会话。终端默认关闭；local 以当前用户权限运行，docker 使用非 root、禁用网络、只读工作区的受限容器。`read_web` 读取域名白名单内的文本；可选 Chromium 浏览器支持 JS 页面操作，任务内保留页面状态，任务结束关闭。它不复用个人浏览器登录，也不支持人工接管。详见[工具与主动进化配置](docs/computer-and-proactive.md)。

## 主动全域进化

配置 `proactive_evolution = true` 后，console/task-worker 在空闲时按 `evolution_interval_seconds` 主动检查源码，即使没有错误记录也能发现改进机会。实例通过共享 SQL 租约协调，同一项目同时只运行一轮检查；发现必须引用读过的源码，提案关联检查证据。

用户任务较多时，可先执行 `evomesh init`，再独立运行 `evomesh evolution-worker --interval 60`。它只负责主动进化，不消费用户任务；轮询频率不会绕过数据库中的进化周期和租约。此时可将任务进程的 `proactive_evolution` 设为 `false`，避免它占用用户任务执行时间。专用进程收到 SIGINT/SIGTERM 后完成当前轮次再退出。`--once` 只执行一轮到期检查，框架也可通过 `ProactiveEvolution.run(stop_event, ...)` 托管运行。

主动检查使用独立的 `evolution_max_steps`（默认 32，范围 1–100），不占用普通对话的步数配置。源码按字符分页读取，返回 `next_offset` 供继续读取，避免大文件被工具输出上限截断；达到步数上限时明确记录 `StepLimitExceeded`。提高预算会增加潜在模型调用量，不保证一定产生有效提案。

反思先接收最近 20 条观察和 10 条提案的有界摘要，再通过 `read_review_record` 分页读取完整证据、提案理由或验证报告，避免大记录在启动时挤满上下文。记录在本轮中保持固定版本；被门禁拒绝的改动可依据报告修正，仍须重新通过原有检查。个人助手的主动进化与框架 `Agent.reflect()` 共用此流程。

局部修改可用 `propose_evolution` 的 `edits: [{old, new}]`，无需让模型重写整份文件。框架基于固定 Git 版本执行唯一匹配替换、保留其余源码，再生成原有的完整文件提案；任一编辑失败则整份提案不入库。五类进化目标均支持，仍经过证据关联、冲突检查和测试门禁。框架可直接调用 `make_edit_change`，详见[工具与主动进化配置](docs/computer-and-proactive.md)。

配置 `evolution_checks` 后会继续验证并合入 `evolution/main`；不配置时保留待集成提案。检查失败不会发布候选代码。运行实例升级仍需显式部署，不会在任务中热替换代码。也可用 `evomesh evolve --force` 或 `app.evolve_once(force=True)` 手动立即触发，活跃租约不能被强制覆盖。

提案已经入库后，即使后续模型调用、步数预算或集成阶段失败，轮次报告仍保留提案编号和错误类型，避免把已完成的工作误判为未生成。失败的检查不会自动进入集成；重试前可先核对已有提案状态。框架通过 `ReflectionFailed.proposal_ids` 获取这类部分完成结果。

## 全栈进化仍是核心

在目标 Agent 的 Git 源码仓库中：

```bash
evomesh observe '保存结果后经常忘记说明文件位置'
evomesh reflect --repo .
evomesh proposals
evomesh worker --repo . --interval 60 --check 'python -m pytest -q'
git diff main..evolution/main
evomesh upgrade --repo .
```

skill、tool、soul、循环逻辑和其他文本源码均可提交改动。独立 Git 工作区验证后进入进化分支，运行实例在任务间升级。`worker` 负责进化提案；`task-worker` 负责用户任务，两者职责不同。

新建项目需要先初始化 Git 并提交源码才能反思。要进化 EvoMesh 框架自身，应将 `repo` 指向其源码仓库；新建应用的源码修改不会自动改写已安装的依赖包。

## API 与部署

```bash
evomesh init
export EVOMESH_SERVER_TOKEN='长随机令牌'
evomesh serve --port 8765
evomesh task-worker # 在另一个终端中运行。
```

API 支持同步任务、后台任务、事件、取消/重试、会话、记忆和定时任务。服务端配置固定 owner，不接受请求方随意选择 owner。[API 文档](docs/api.md)。

`docker compose up --build -d` 示例包含 MySQL、两个 API 副本和后台任务进程，先填写 `.env.example`。同一 owner 的副本需要看到相同工作区；数据库只共享记录，不同步文件。SQLite 用于同机进程，跨主机用 MySQL。[部署与升级](docs/deployment.md)。

## 数据、验证与边界

`AgentApp` 会持久化完整的已完成对话和工具结果，用于恢复会话；记忆按 project + owner 隔离，进化观察按 project 共享。请保护数据库。命名空间不是数据库层面的租户安全边界。轻量 `Agent` 本身仍仅保留进程内历史。

CI 覆盖 SQLite、真实 MySQL 8.4、Python 3.11–3.13、实际 MCP 子进程、安装包、Docker 构建及 Docker 终端验证。尚未宣称真实模型任务质量提升，也没有完成全自动集群发布或强隔离的进化验证沙箱。

参考 Hermes 的经验学习与自动化、OpenMuse 的持久任务与可见过程，持续推进能力对齐。MIT 协议，分发名 `evomesh-agent`，命令和导入名 `evomesh`，目前从源码安装，未发布到 PyPI。

## v0.3：消息渠道与 Web 控制台

- **飞书 / Lark、钉钉、Telegram**：文本消息接收和原会话回复，签名/密钥校验，可信发送者及群聊名单。
- **持久化收件与回复**：回调去重、原子创建任务、多个工作进程协调回复；失败或状态不确定时由操作者手动重试。
- **Web 控制台**：运行概览、持久会话、任务事件/取消/重试、长期记忆、周期任务、渠道状态与接入配置、进化反馈和提案。
- **框架入口**：`ChannelHub.register()` 支持自定义渠道；`create_server(..., app=app, with_worker=True)` 将你的 `AgentApp` 暴露为同一套控制台和 API。

`evomesh console` 自动初始化新增表并启动内置工作进程；部署时仍可使用 `serve` + `task-worker` 分离运行。现有用户更新后先执行一次 `evomesh init`。

渠道配置通过 TOML 和环境变量管理，控制台展示接入配置与运行状态。当前不含附件、语音、平台 OAuth 引导或 token 流式输出。自动化测试使用平台协议模拟；真实应用凭据需自行配置并联调。

[控制台指南](docs/console.md) · [渠道接入指南](docs/channels.md) · [配置示例](evomesh.channels.example.toml)

## v0.4：常见 Agent 能力与全域进化

- **分步计划**：按会话持久化，步骤状态和版本校验，多实例通过会话租约协调编辑。
- **工作区知识检索**：索引文本/Markdown/CSV/JSON/代码，返回文件和行号；源文件改变后排除陈旧索引。当前为关键词检索，不冒充向量语义检索。
- **按需技能**：先提供技能目录，用到时读取正文，减少无关上下文。
- **上下文与循环控制**：按字符预算裁剪完整旧回合、保留工具调用配对；对同一次运行中的重复调用设限并记录问题。
- **评测与证据**：断言回答、工具使用和调用预算；记录运行指标，将提案关联到真实反馈记录。

这些能力都有框架 API；计划、知识、技能和进化指标也进入控制台。通过 `@tool(source_paths=("my_tools.py",), capability="my-search")` 可以把业务工具源码也纳入进化定位。检索器、规划器、执行循环、控制台乃至评测代码，都仍然走同一套源码提案与验证机制。

```bash
evomesh init # 升级后、重启实例前执行一次
evomesh console
# 在源码仓库中运行离线行为评测：
evomesh evaluate evals/offline.json --factory examples.evaluation_factory:create_app
```

[能力与全域进化说明](docs/agent-capabilities.md)。自动执行任务图、断点恢复、语义向量库和自动集群升级仍未实现。

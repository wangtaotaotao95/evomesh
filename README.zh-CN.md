# EvoMesh

**一个实例积累经验，整个 Agent 持续进化。**

面向多实例、支持全栈自进化的个人助手与 Agent 开发框架。Python 3.11+ · MIT · Alpha

[English](README.md) · [架构](docs/architecture.md) · [部署](docs/deployment.md) · [路线图](docs/roadmap.md)

## 为什么做这个项目

Agent 使用起来不流畅，问题可能出在任何地方：skill 不够好、tool 缺能力、soul 不合适、ReAct 循环反复尝试，甚至某一行代码有缺陷。

EvoMesh 的目标是让这些问题都能进入同一套进化流程：**记录阻碍 → 分析源码 → 提交改动 → 多实例共享记录 → 定期验证合并 → 按版本升级**。

它既是可以直接运行的个人助手，也是可以替换模型、注册工具、修改运行循环的框架。借鉴 Hermes 的经验学习思路，差异重点是多实例共享进化记录和全栈改动的集成机制。Hermes 官方也有面向工具、提示词和代码的进化规划，因此本项目不以“其他 Agent 只能进化 skill”作为宣传前提。

## 首版已实现

- **个人助手**：CLI 对话、工作区文件读写、SOUL.md、技能文件加载。
- **多实例**：带 Bearer Token 的任务 API、实例标识、共享项目进化记录。
- **两种存储**：SQLite（同机多进程）和 MySQL（跨主机）。
- **自动观察与反思**：记录工具错误、步数耗尽、用户反馈；模型可读取源码并提交改进。
- **全栈提案**：skill、tool、soul、loop、其他 UTF-8 源码，支持新增、替换和删除。
- **定期集成**：租约领取、续租、重复提案去重、文件冲突检测、验证、原子更新 Git 分支。
- **恢复和升级**：恢复“Git 已提交但数据库未记账”的任务，显式升级到验证后的版本。

**当前是可运行的 alpha。** 自动生成改动、验证、合并已经连通；无人值守的全量部署升级、语义级冲突解决、强沙箱、持久会话记忆尚未实现。模型能否提出真正有效的改进，需要真实任务评估，不能只靠测试通过判断。

## 快速开始：不需要 API Key

```bash
git clone https://github.com/wangtaotaotao95/evomesh.git
cd evomesh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
evomesh init
evomesh run --demo '列出我的工作区文件'
python examples/evolution_demo.py
```

`--demo` 是确定性的工具调用演示，并不是大模型。第二个演示会创建临时 Agent 仓库，让两个实例分别提出 soul 和 tool 改进，验证合并后升级，完整展示协同进化流程。

## 作为个人助手

配置支持工具调用的 Chat Completions 兼容接口：

```bash
export EVOMESH_BASE_URL='https://你的服务商/v1'
export EVOMESH_MODEL='你的模型名称'
export EVOMESH_API_KEY='你的密钥'
evomesh chat
# 单次任务；发生运行阻碍时自动反思，可能产生额外模型调用费用。
evomesh run --auto-reflect '帮我整理工作区中的笔记'
```

默认只操作 `workspace/` 内的文件。CLI 会话历史保留在进程内；默认不把原始对话写入共享数据库。现有内置工具是文件列表、读取和写入；浏览器、邮件、日历等需要自行扩展工具。

## 让 Agent 进化

```bash
# 用户反馈也是进化依据。
evomesh observe '保存结果后，助手经常忘记告诉我文件位置'
evomesh reflect --repo .
evomesh proposals

# 处理一个提案，在独立 Git 工作区中运行检查。
evomesh integrate --repo . --check 'python -m pytest -q'

# 或启动定期合并进程，每 60 秒处理一个提案。
evomesh worker --repo . --interval 60 --check 'python -m pytest -q'

# 查看改动，再在任务之间升级和重启。
git diff main..evolution/main
evomesh upgrade --repo .
```

提案进入 `evolution/main` 分支，运行中的源码不会被中途替换。不同文件的改进可以依次合并；同一文件已被修改时标记为冲突，重新反思生成提案。更改依赖后需要重新安装依赖。

skill、soul 和循环逻辑只是提案分类，机制不限于这些目录：框架源码也能修改。受限的是 `.git`、环境密钥文件、目录逃逸与符号链接路径，避免把运行配置当成进化内容。

## 多实例与 MySQL

```bash
pip install -e '.[dev,mysql]'
export EVOMESH_DATABASE_URL='mysql+pymysql://user:password@host/evomesh?charset=utf8mb4'
export EVOMESH_PROJECT='my-agent'
evomesh init  # 所有实例启动前，初始化一次。
export EVOMESH_SERVER_TOKEN='长随机令牌'
EVOMESH_INSTANCE_ID=laptop evomesh serve --port 8765
# 另一台机器使用相同数据库和项目名，但不同实例名与独立工作区。
```

任务 API：`POST /run`，请求体 `{"task":"你的任务"}`，请求头 `Authorization: Bearer <令牌>`。每次请求是一个新会话。

也可以 `cp .env.example .env`，填写配置后运行 `docker compose up --build -d`，启动 MySQL 和两个 API 实例。容器运行镜像不包含 Git 历史，反思与合并使用宿主机的源码仓库和同一个数据库，详见[部署文档](docs/deployment.md)。

SQLite 适合同一主机多个实例；跨主机用 MySQL。实例共享进化记录，不共享个人工作区。集成进程必须使用同一个权威 Git 仓库；多个独立克隆不能各自更新“同一个”集成分支。

## 作为框架

`Agent` 接收模型、存储与工具列表。`Tool` 使用 JSON Schema 定义参数，处理函数是普通 Python 函数。模型只需实现 `complete(messages, tools)` 方法。详见[完整扩展示例](examples/custom_agent.py)。

## 测试与边界

```bash
pip install -e '.[dev,mysql]'
ruff check .
ruff format --check .
pytest -q
```

CI 包含 SQLite、真实 MySQL 8.4、Python 3.11–3.13、安装包构建和 Docker 构建。默认验证是代码正确性检查，不等于证明改进了用户体验。

独立 Git 工作区不等于操作系统沙箱。候选代码的测试仍然会执行程序；不可信提案应放在受限的临时执行环境中验证，并增加外部质量评估。共享数据库面向可信实例，不提供多租户权限隔离。

项目采用 MIT 协议，Python 分发名称为 `evomesh-agent`，导入名和命令为 `evomesh`；目前从源码安装，尚未发布到 PyPI。本仓库独立于其他同名 EvoMesh 项目。

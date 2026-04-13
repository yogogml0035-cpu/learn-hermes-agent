# Entity Map (系统实体边界图)

> Hermes Agent 的实体比一般 agent 多，因为它同时面对"多个消息来源"和"多个执行环境"。  
> 这份文档帮你把这些实体按层级分开，避免越学越混。

## 总图

```text
消息来源层
  - CLI 用户输入
  - Telegram / Discord / 微信 / ... 消息
  - 定时任务触发

入口转换层
  - Platform Adapter（把平台消息转成统一格式）
  - MessageEvent（统一消息格式）
  - Gateway（路由到正确的会话）

对话层
  - AIAgent 实例（每条消息一个，或 CLI 下一个长实例）
  - messages[]（当前对话历史）
  - system prompt（多来源组装，缓存复用）

工具与智能层
  - Tool Entry（注册表里的一个工具）
  - Skill（agent 管理的能力文件）
  - Memory（跨会话的持久知识）
  - Approval（危险命令审批）
  - Subagent（隔离上下文的子执行者）

执行层
  - Terminal Backend（命令在哪跑）
  - MCP Server（外部能力接入）

持久化层
  - SessionDB（SQLite 会话和消息）
  - SOUL.md / MEMORY.md / USER.md（文件）
  - skills/（技能目录）
  - config.yaml + .env（配置）
  - jobs.json（定时任务）
```

## 最容易混淆的概念

### CLI 入口 vs Gateway 入口

| | CLI | Gateway |
|---|---|---|
| 用户在哪 | 终端里 | Telegram / Discord / 微信 / ... |
| AIAgent 生命周期 | 一个实例从头跑到尾 | 每条消息创建新实例 |
| 会话历史怎么传 | 实例自己维护 | 从 SQLite 读出来传给新实例 |
| 共同点 | 最终都调 `run_conversation()` | 最终都调 `run_conversation()` |

关键：**核心循环完全一样。** 区别只在入口的消息怎么来、回复怎么回去。

### Platform Adapter vs Terminal Backend

这是初学者最容易混的一对。

| | Platform Adapter | Terminal Backend |
|---|---|---|
| 管什么 | 消息从哪个平台来 | 命令在哪里跑 |
| 例子 | Telegram 适配器、微信适配器 | Docker 后端、SSH 后端 |
| 属于哪一层 | 入口转换层 | 执行层 |
| 互相依赖吗 | 不依赖 | 不依赖 |

你可以在 Docker 里跑命令，同时消息来自 Telegram。两者完全独立。

### Tool vs Skill

| | Tool | Skill |
|---|---|---|
| 谁写的 | 开发者硬编码 | agent 运行时创建/编辑 |
| 存在哪 | Python 代码文件 | skills/ 目录下的 SKILL.md |
| 怎么执行 | 注册表直接分发到 handler | 通过已有工具（如终端）间接执行 |
| 能改吗 | 需要改代码 | agent 自己就能改 |

技能不是工具的替代品。技能是"agent 用工具做事的方法"。

### Tool vs MCP Tool

| | 内置 Tool | MCP Tool |
|---|---|---|
| 来源 | 代码里自注册 | 外部 MCP Server 暴露 |
| 模型看起来 | 完全一样 | 完全一样 |
| 实际执行 | 本地 handler | 通过 MCP 协议发给外部 server |

对模型透明——它不知道也不需要知道一个工具是内置的还是外部的。

### Memory vs Session

| | Memory | Session |
|---|---|---|
| 粒度 | 精选的跨会话信息 | 一次完整对话的全部消息 |
| 数量 | 少（应该保持精炼） | 多（每次对话一个） |
| 存在哪 | MEMORY.md / USER.md | SQLite |
| 谁写 | agent 主动写 | 系统自动存 |

session 是完整的对话快照。memory 是 agent 认为"未来还有用"的精选信息。

### SOUL.md vs MEMORY.md vs HERMES.md

| | SOUL.md | MEMORY.md | HERMES.md |
|---|---|---|---|
| 是什么 | 人设 | agent 的笔记 | 项目规则 |
| 谁写 | 用户 | agent | 开发者 |
| 存在哪 | HERMES_HOME | HERMES_HOME | 项目目录 |
| 变化频率 | 很少变 | 经常更新 | 按项目固定 |
| 作用 | 定义 agent 是谁 | 记住用户偏好和项目信息 | 告诉 agent 这个项目的规则 |

三者都会进入 system prompt，但性质完全不同。

### AIAgent 实例 vs Subagent

| | AIAgent（主） | Subagent |
|---|---|---|
| 谁创建 | CLI 或 Gateway | 主 agent 通过 delegate 工具 |
| messages | 主对话的完整历史 | 独立的 messages 列表 |
| 目的 | 完成用户请求 | 完成一个子任务，返回摘要 |
| 迭代预算 | 和父共享 | 和父共享 |

子 agent 的价值：把探索性工作丢进干净上下文，不污染主对话。

## 速查表

| 实体 | 属于哪一层 | 存在哪 |
|---|---|---|
| 用户消息 | 消息来源层 | 终端 / 平台 API |
| MessageEvent | 入口转换层 | Gateway 内部 |
| Platform Adapter | 入口转换层 | gateway/platforms/ |
| AIAgent | 对话层 | 运行时内存 |
| messages[] | 对话层 | 运行时内存 |
| system prompt | 对话层 | 运行时缓存 + SQLite |
| Tool Entry | 工具与智能层 | tool registry |
| Skill | 工具与智能层 | skills/ 目录 |
| Memory | 工具与智能层 | MEMORY.md / USER.md |
| Approval | 工具与智能层 | 运行时 + session 缓存 |
| Subagent | 工具与智能层 | 运行时（独立实例） |
| Terminal Backend | 执行层 | environment manager |
| MCP Server | 执行层 | MCP client |
| Session | 持久化层 | SQLite |
| Config | 持久化层 | config.yaml + .env |
| Cron Job | 持久化层 | jobs.json |

## 怎么用这张图

不用背。每次你混了两个词，来这里确认它们是不是在同一层。如果不在同一层，它们就不是同一种东西，不管名字多像。

## 一句话

**Hermes Agent 比一般 agent 多了"入口转换层"和"执行环境层"，这两层是它能跨平台运行的关键。搞清这两层的边界，后面的章节就不容易混了。**

# Glossary (术语表)

> 这份术语表收录 Hermes Agent 体系里最容易让初学者卡住的词。  
> 很多词在通用 agent 教学里也会出现，但在 Hermes 里有特定的含义或实现方式。

## 推荐联读

- [`entity-map.md`](./entity-map.md)：搞清每个实体属于哪一层。
- [`data-structures.md`](./data-structures.md)：搞清这些词落到代码里时，状态长什么样。

## AIAgent

Hermes Agent 的核心类。

它不是一个长期活着的服务进程。在 CLI 模式下，一个实例从头跑到尾。在 Gateway 模式下，每条用户消息到达时创建一个新实例，传入历史消息，跑完就销毁。

这意味着任何需要跨消息保持的状态，都不能存在实例里。

## run_conversation

AIAgent 的主方法。接收一条用户消息，运行对话循环直到模型不再调用工具，返回最终回复和完整消息历史。

它是一个普通的 `def`，不是 `async def`。这是 Hermes Agent 最重要的设计选择之一。

## OpenAI 兼容接口

Hermes Agent 用 OpenAI 的 Python SDK 作为唯一的 API 客户端。所有模型提供商（OpenRouter、Anthropic、本地端点）都通过设置 `base_url` 接入。

这意味着消息格式永远是 OpenAI 格式（`role: user/assistant/tool`），切换模型只需要改配置。

## Tool Registry（工具注册表）

一个单例注册表。每个工具文件在被 Python 导入时自动调用 `register()` 登记自己。

Hermes Agent 的导入链是：注册表 ← 工具文件 ← 编排层 ← 核心循环。注册表不依赖任何工具，避免循环导入。

## Toolset（工具集）

工具按功能分组。比如网页搜索和网页抓取属于 `web` 组，终端命令属于 `terminal` 组。

启动 agent 时可以按组开关，而不用一个一个工具配。

## Skill（技能）

和工具不同，技能是 agent 在运行时可以创建、编辑、删除的能力文件。

技能文件就是一个 SKILL.md，描述了一种能力的用法。agent 通过已有的工具（比如终端）来执行技能，而不是绕过工具系统。

这是 Hermes Agent 和大多数 agent 框架不同的地方：能力不全是硬编码的，有一部分是 agent 自己管理的。

## Gateway

多平台消息网关。它的作用是同时监听多个消息平台，把收到的消息路由到 agent 循环，再把回复发回对应平台。

一个 Gateway 进程可以同时服务 Telegram、Discord、微信等十几个平台。

## Platform Adapter（平台适配器）

Gateway 内部，每个平台有一个适配器。

适配器负责：连接平台、接收消息、把消息转成统一格式、把 agent 的回复翻译回平台格式发出去。

所有适配器遵循同一个基类接口。添加一个新平台只需要写一个新适配器。

## MessageEvent（消息事件）

适配器把平台特定的消息转成的统一格式。

不管消息是从 Telegram 还是微信来的，Gateway 收到的都是同一种结构。这让核心循环完全不需要知道消息来自哪个平台。

## Terminal Backend / Execution Environment（执行环境）

终端命令实际执行的地方。

Hermes Agent 抽象了一个执行环境接口，有六种实现：本地进程、Docker 容器、SSH 远程、Modal 无服务器、Daytona 无服务器、Singularity 容器。

对工具层透明——工具只管发命令，不管命令在哪里跑。

## SessionDB（会话数据库）

SQLite 数据库，存储所有会话的元数据和完整消息历史。

用 WAL 模式支持并发读写（Gateway 多平台场景），用 FTS5 支持全文搜索历史会话。

## FTS5

SQLite 的全文搜索扩展。让你能在大量历史会话中快速搜索文本内容。

## Context Compression（上下文压缩）

对话变长时，自动用 LLM 对历史消息做摘要，用摘要替代原文。

Hermes Agent 的压缩会触发 session 分裂：压缩后创建一个新 session，通过 `parent_session_id` 指向旧 session。这样旧的完整历史不会丢失。

## SOUL.md

人设文件。存在 HERMES_HOME 目录下，定义 agent 的身份和行为风格。

每次对话开始时读入，组装到 system prompt 的最前面。

## MEMORY.md / USER.md

记忆文件。存储跨会话仍然有价值的信息（用户偏好、项目背景等）。

和 SOUL.md 的区别：SOUL.md 是人设（谁），MEMORY.md 是记忆（知道什么）。

## HERMES.md / AGENTS.md

项目级配置文件。放在项目目录里，告诉 agent 这个项目的规则和上下文。

类似于其他 agent 框架的 CLAUDE.md 或 .cursorrules，但 Hermes Agent 支持多种文件名，按优先级使用。

## Profile（配置档案）

完全隔离的运行环境。一个用户可以有多个 profile，每个有独立的配置、记忆、会话和技能。

用途：同一个人可能有一个"开发"profile 和一个"写作"profile，两者的人设、记忆、工具配置完全不同。

## Approval / Dangerous Command Detection（危险命令检测）

执行终端命令前的安全检查。

系统维护一个危险命令模式列表（正则表达式），命令执行前先匹配。命中了就走审批流程：在 CLI 模式下问用户确认，在 Gateway 模式下发审批按钮。

## Iteration Budget（迭代预算）

一次对话允许的最大 API 调用次数。默认 90。

不只是防死循环。在子 agent 场景下，父和子共享 budget，所以它是一个显式管理的资源。

## Failover（故障转移）

API 调用失败时，根据错误分类决定是重试还是切换到备用模型。

比如：被限速了 → 退避重试；上下文太长了 → 触发压缩；认证失败了 → 切换凭证。

## MCP

Model Context Protocol。让 agent 通过统一协议接入外部工具。

MCP 工具接入后，在模型看来和内置工具完全一样。模型不需要知道这个工具是内置的还是外部的。

## Cron Job（定时任务）

让 agent 在未来某个时间自动执行工作。

支持三种调度格式：一次性延时（`30m`）、循环间隔（`every 2h`）、标准 cron 表达式（`0 9 * * *`）。

## 最容易混的概念

| 对比 | 区分方法 |
|---|---|
| CLI vs Gateway | 一个是单用户终端入口，一个是多平台消息网关。核心循环相同。 |
| 工具 vs 技能 | 工具是硬编码的能力，技能是 agent 运行时管理的能力文件。 |
| 执行环境 vs 平台适配器 | 执行环境管"命令在哪跑"，适配器管"消息从哪来"。 |
| Session vs Memory | Session 是一次对话的完整记录，Memory 是跨会话的精选信息。 |
| SOUL.md vs MEMORY.md | SOUL.md 是人设（不变），MEMORY.md 是记忆（会更新）。 |
| HERMES.md vs MEMORY.md | HERMES.md 是项目规则（给 agent 的指令），MEMORY.md 是 agent 自己积累的知识。 |
| Iteration vs Turn | 一个 iteration 是一次 API 调用。一个 turn 可能包含多个 iteration（如果模型连续调用工具）。 |

---

如果读文档时遇到新词卡住，优先回这里，不要硬顶着往后读。

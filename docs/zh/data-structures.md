# Core Data Structures (核心数据结构总表)

> Hermes Agent 的状态不是"一个 messages 列表加一堆工具"。  
> 因为它要服务多个平台、持久化到 SQLite、在不同环境执行命令，所以状态分布在好几层。  
> 这份文档帮你把"状态到底放在哪"看成一张图。

## 推荐联读

- [`glossary.md`](./glossary.md)：先不懂词回这里。
- [`entity-map.md`](./entity-map.md)：先不懂边界回这里。

## 先记住一条线索

```text
messages → 当前对话（运行时）
session  → 对话持久化（SQLite）
memory   → 跨会话知识（文件）
config   → 运行配置（YAML + 环境变量）
```

大多数 agent 只有第一层。Hermes Agent 有四层，因为它要跨重启、跨平台、跨 profile 运行。

## 1. 对话状态

### Messages

当前对话的完整消息列表。OpenAI 格式。

```python
messages = [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [...]},
    {"role": "tool", "tool_call_id": "...", "content": "..."},
]
```

这是模型每轮看到的输入。不是聊天记录展示层。

注意：Hermes Agent 内部会在 assistant 消息上额外存一些字段（如 `reasoning`、`finish_reason`），但这些字段在发给 API 前会被清理掉。所以内部 messages 和发给 API 的 messages 不完全一样。

相关章节：`s01`

### System Prompt

不在 messages 列表里。每次 API 调用时拼在最前面。

由多个来源组装：

```text
SOUL.md（人设）
  + MEMORY.md / USER.md（记忆）
  + HERMES.md 或 AGENTS.md 或 CLAUDE.md 或 .cursorrules（项目配置，优先级递减）
  + 工具定义
  + 技能清单
```

组装一次后缓存。后续调用复用缓存。为什么要缓存？因为 Anthropic 的 prompt caching 要求 system prompt 在多轮间保持不变。

相关章节：`s04`

## 2. 持久化状态

### Session（SQLite）

一次完整对话的记录。

```python
session = {
    "id": "...",
    "source": "cli" | "telegram" | "discord" | ...,
    "model": "...",
    "system_prompt": "...",       # 首次组装后存下来，后续复用
    "parent_session_id": "...",   # 压缩后新 session 指向旧 session
    "started_at": ...,
    "message_count": ...,
    "input_tokens": ...,
    "output_tokens": ...,
    "estimated_cost_usd": ...,
}
```

Hermes Agent 独特的地方：

- `source` 标记消息来自哪个平台。这让你能按平台过滤会话。
- `system_prompt` 存下来是为了 continuing session 时不重新组装（保持缓存一致）。
- `parent_session_id` 实现了会话链：压缩后旧历史不丢，通过链条可以追溯。

相关章节：`s03`

### Messages 表（SQLite）

Session 里的每条消息单独存一行。

```python
message_row = {
    "session_id": "...",
    "role": "user" | "assistant" | "tool",
    "content": "...",
    "tool_calls": "...",     # JSON 序列化
    "tool_call_id": "...",
    "tool_name": "...",
    "timestamp": ...,
    "token_count": ...,
}
```

FTS5 索引建在 content 上，让你能全文搜索历史会话。

相关章节：`s03`

## 3. 工具状态

### Tool Entry（注册表条目）

```python
tool = {
    "name": "web_search",
    "toolset": "web",
    "schema": {"description": "...", "parameters": {...}},
    "handler": ...,       # 真正执行工具的函数
    "is_async": False,    # 是否需要异步桥接
    "requires_env": [],   # 依赖哪些环境变量
}
```

每个工具文件在被导入时注册这样一条记录。

Hermes Agent 独特的地方：`is_async` 标记。如果为 True，编排层会把调用扔进持久化事件循环，而不是直接 call。这就是"同步循环 + 异步桥接"的实现。

相关章节：`s02`

### Dangerous Pattern（危险模式）

```python
pattern = (r'\brm\s+-[^\s]*r', "recursive delete")
```

一个正则表达式 + 一句人类可读描述。

系统维护一个这样的列表。终端命令执行前先匹配。命中了就走审批。

审批按 session 缓存：同类操作在同一个 session 里只需要审批一次。

相关章节：`s09`

## 4. 记忆与技能状态

### Memory（文件）

```text
# MEMORY.md 示例
- 用户偏好使用 tabs 缩进
- 项目用 pytest 跑测试
- 数据库是 PostgreSQL 15
```

不是结构化数据。就是 markdown 文本。agent 自己读写。

Hermes Agent 独特的地方：记忆分两个文件——MEMORY.md 存 agent 的笔记，USER.md 存用户画像。

相关章节：`s07`

### Skill（文件）

```text
# SKILL.md 示例
name: data-analysis
description: 分析 CSV 数据并生成报告
---
使用方法：
1. 读取 CSV 文件
2. 用 pandas 分析
3. 生成 markdown 报告
```

每个技能是一个目录下的 SKILL.md。agent 可以创建、编辑、删除。

和工具的关键区别：工具的代码是人写的，技能的内容是 agent 管理的。

相关章节：`s08`

## 5. 配置状态

### Config（合并后的字典）

```python
config = {
    "model": "...",
    "base_url": "...",
    "api_key": "...",
    "enabled_toolsets": [...],
    "max_iterations": 90,
    "personality": "...",
}
```

来自多个来源，按优先级合并：命令行参数 > 环境变量 > config.yaml > 默认值。

Hermes Agent 独特的地方：支持 Profile 隔离。每个 Profile 是一个独立的 HERMES_HOME 目录，有自己的 config、memory、session、skills。

相关章节：`s11`

## 6. Gateway 状态

### Message Event（统一消息格式）

```python
event = {
    "text": "...",
    "message_type": "text" | "command" | "image" | ...,
    "source": {
        "platform": "telegram",
        "chat_id": "...",
        "user_id": "...",
        "chat_type": "dm" | "group",
    },
}
```

所有平台适配器产出这同一种格式。Gateway 不需要关心消息来自哪个平台。

Hermes Agent 独特的地方：`source` 结构里包含了平台、chat、user、thread 等维度，足够精确地区分"同一个平台的不同对话"。

相关章节：`s12`、`s13`

## 7. 执行环境状态

### Environment（抽象接口的实例）

没有一个统一的数据结构。而是一个接口：

```python
# 核心方法
env.run_command(command) → output
env.write_file(path, content)
env.read_file(path) → content
```

六种实现各有各的内部状态（Docker 有 container_id，SSH 有连接信息，Modal 有 sandbox_id），但对外都是同一套接口。

相关章节：`s14`

## 8. 定时任务状态

### Job（字典）

```python
job = {
    "id": "...",
    "schedule": {
        "kind": "cron" | "interval" | "once",
        "expr": "0 9 * * *",    # cron 模式
        "minutes": 30,           # interval 模式
        "run_at": "...",         # once 模式
    },
    "prompt": "生成周报",
    "enabled": True,
    "last_run_at": "...",
    "next_run_at": "...",
}
```

存在 JSON 文件里。scheduler 定期 tick，检查有没有到期的 job。

相关章节：`s15`

## 串起来

```text
运行时
  messages[]        当前对话
  tool registry     能力目录
  environment       命令在哪跑

SQLite
  sessions          对话记录
  messages 表       每条消息
  FTS5 索引         全文搜索

文件系统
  SOUL.md           人设
  MEMORY.md         记忆
  USER.md           用户画像
  skills/           技能文件
  config.yaml       配置
  jobs.json         定时任务

Gateway（可选）
  adapters{}        平台连接
  message events    统一消息
  session routing   会话路由
```

## 教学边界

这份总表帮你定位"一个状态属于哪一层"。

它不负责展开每个字段的全部细节。如果你知道某个状态归谁管了，回到对应章节看完整实现。

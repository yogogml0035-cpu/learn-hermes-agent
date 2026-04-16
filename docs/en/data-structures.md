# Core Data Structures

> The state of Hermes Agent is not just "a messages list plus a bunch of tools."  
> Because it serves multiple platforms, persists to SQLite, and executes commands in different environments, state is spread across several layers.  
> This document helps you see "where does state actually live" as a single map.

## Recommended Reading

- [`glossary.md`](./glossary.md) -- look up unfamiliar terms here.
- [`entity-map.md`](./entity-map.md) -- look up unclear boundaries here.

## Start with One Thread

```text
messages  -> current conversation (runtime)
session   -> conversation persistence (SQLite)
memory    -> cross-session knowledge (files)
config    -> runtime configuration (YAML + environment variables)
```

Most agents only have the first layer. Hermes Agent has four, because it needs to survive restarts, span platforms, and switch profiles.

## 1. Conversation State

### Messages

The full message list for the current conversation. OpenAI format.

```python
messages = [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [...]},
    {"role": "tool", "tool_call_id": "...", "content": "..."},
]
```

This is the input the model sees on every turn. It is not a chat-display layer.

Note: Internally, Hermes Agent stores extra fields on assistant messages (such as `reasoning` and `finish_reason`), but these fields are stripped before sending to the API. So internal messages and API-bound messages are not identical.

Related section: `s01`

### System Prompt

Not part of the messages list. Prepended at the start of each API call.

Assembled from multiple sources:

```text
SOUL.md (persona)
  + MEMORY.md / USER.md (memory)
  + HERMES.md or AGENTS.md or CLAUDE.md or .cursorrules (project config, in decreasing priority)
  + tool definitions
  + skill inventory
```

Assembled once, then cached. Subsequent calls reuse the cache. Why cache? Because Anthropic's prompt caching requires the system prompt to remain unchanged across turns.

Related section: `s04`

## 2. Persisted State

### Session (SQLite)

A record of one complete conversation.

```python
session = {
    "id": "...",
    "source": "cli" | "telegram" | "discord" | ...,
    "model": "...",
    "system_prompt": "...",       # Stored after first assembly, reused later
    "parent_session_id": "...",   # After compression, new session points to old session
    "started_at": ...,
    "message_count": ...,
    "input_tokens": ...,
    "output_tokens": ...,
    "estimated_cost_usd": ...,
}
```

What makes Hermes Agent unique here:

- `source` tags which platform a message came from. This lets you filter sessions by platform.
- `system_prompt` is saved so that a continuing session does not need to reassemble it (keeping the cache consistent).
- `parent_session_id` implements session chaining: after compression the old history is not lost -- you can trace back through the chain.

Related section: `s03`

### Messages Table (SQLite)

Each message in a session is stored as a separate row.

```python
message_row = {
    "session_id": "...",
    "role": "user" | "assistant" | "tool",
    "content": "...",
    "tool_calls": "...",     # JSON-serialized
    "tool_call_id": "...",
    "tool_name": "...",
    "timestamp": ...,
    "token_count": ...,
}
```

An FTS5 index is built on the content column, enabling full-text search across historical sessions.

Related section: `s03`

## 3. Tool State

### Tool Entry (Registry Entry)

```python
tool = {
    "name": "web_search",
    "toolset": "web",
    "schema": {"description": "...", "parameters": {...}},
    "handler": ...,       # The function that actually executes the tool
    "is_async": False,    # Whether it needs async bridging
    "requires_env": [],   # Which environment variables it depends on
}
```

Each tool file registers an entry like this when it is imported.

What makes Hermes Agent unique here: the `is_async` flag. If True, the orchestration layer dispatches the call into a persistent event loop instead of calling it directly. This is how the "synchronous loop + async bridging" pattern is implemented.

Related section: `s02`

### Dangerous Pattern

```python
pattern = (r'\brm\s+-[^\s]*r', "recursive delete")
```

A regular expression paired with a human-readable description.

The system maintains a list of these patterns. Terminal commands are matched against the list before execution. A match triggers the approval flow.

Approvals are cached per session: the same category of operation only needs approval once within a single session.

Related section: `s09`

## 4. Memory and Skill State

### Memory (Files)

```text
# MEMORY.md example
- User prefers tabs for indentation
- Project uses pytest for testing
- Database is PostgreSQL 15
```

Not structured data. Just markdown text. The agent reads and writes it itself.

What makes Hermes Agent unique here: memory is split into two files -- MEMORY.md stores the agent's notes, USER.md stores the user profile.

Related section: `s07`

### Skill (Files)

```text
# SKILL.md example
name: data-analysis
description: Analyze CSV data and generate reports
---
How to use:
1. Read the CSV file
2. Analyze with pandas
3. Generate a markdown report
```

Each skill is a SKILL.md file inside its own directory. The agent can create, edit, and delete them.

The key difference from tools: tool code is written by humans; skill content is managed by the agent.

Related section: `s08`

## 5. Configuration State

### Config (Merged Dictionary)

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

Sourced from multiple origins and merged by priority: CLI arguments > environment variables > config.yaml > defaults.

What makes Hermes Agent unique here: it supports Profile isolation. Each Profile is a separate HERMES_HOME directory with its own config, memory, sessions, and skills.

Related section: `s11`

## 6. Gateway State

### Message Event (Unified Message Format)

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

All platform adapters produce this same format. The Gateway does not need to care which platform a message came from.

What makes Hermes Agent unique here: the `source` structure includes platform, chat, user, and thread dimensions -- enough to precisely distinguish "different conversations on the same platform."

Related section: `s12`, `s13`

## 7. Execution Environment State

### Environment (Instance of an Abstract Interface)

There is no single unified data structure. Instead, there is an interface:

```python
# Core methods
env.run_command(command) -> output
env.write_file(path, content)
env.read_file(path) -> content
```

The six implementations each have their own internal state (Docker has a container_id, SSH has connection info, Modal has a sandbox_id), but they all expose the same interface.

Related section: `s14`

## 8. Scheduled Task State

### Job (Dictionary)

```python
job = {
    "id": "...",
    "schedule": {
        "kind": "cron" | "interval" | "once",
        "expr": "0 9 * * *",    # cron mode
        "minutes": 30,           # interval mode
        "run_at": "...",         # once mode
    },
    "prompt": "Generate weekly report",
    "enabled": True,
    "last_run_at": "...",
    "next_run_at": "...",
}
```

Stored in a JSON file. The scheduler ticks periodically, checking for jobs that are due.

Related section: `s15`

## Putting It All Together

```text
Runtime
  messages[]        Current conversation
  tool registry     Capability catalog
  environment       Where commands run

SQLite
  sessions          Conversation records
  messages table    Individual messages
  FTS5 index        Full-text search

File System
  SOUL.md           Persona
  MEMORY.md         Memory
  USER.md           User profile
  skills/           Skill files
  config.yaml       Configuration
  jobs.json         Scheduled tasks

Gateway (optional)
  adapters{}        Platform connections
  message events    Unified messages
  session routing   Session routing
```

## Teaching Boundary

This reference table helps you locate "which layer does a given piece of state belong to."

It does not go into the full details of every field. Once you know which layer owns a piece of state, go back to the corresponding section for the complete implementation.

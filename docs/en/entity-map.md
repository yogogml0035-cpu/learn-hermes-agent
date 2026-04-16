# Entity Map (System Entity Boundaries)

> Hermes Agent has more entities than a typical agent because it simultaneously faces "multiple message sources" and "multiple execution environments."  
> This document helps you separate these entities by layer, so things stay clear as you learn.

## Overview

```text
Message Source Layer
  - CLI user input
  - Telegram / Discord / WeChat / ... messages
  - Scheduled task triggers

Ingress Translation Layer
  - Platform Adapter (converts platform messages into a unified format)
  - MessageEvent (unified message format)
  - Gateway (routes to the correct session)

Conversation Layer
  - AIAgent instance (one per message, or one long-lived instance in CLI mode)
  - messages[] (current conversation history)
  - system prompt (assembled from multiple sources, cached for reuse)

Tools & Intelligence Layer
  - Tool Entry (a single tool in the registry)
  - Skill (capability file managed by the agent)
  - Memory (persistent cross-session knowledge)
  - Approval (dangerous command approval)
  - Subagent (child executor with isolated context)

Execution Layer
  - Terminal Backend (where commands run)
  - MCP Server (external capability integration)

Persistence Layer
  - SessionDB (SQLite sessions and messages)
  - SOUL.md / MEMORY.md / USER.md (files)
  - skills/ (skill directory)
  - config.yaml + .env (configuration)
  - jobs.json (scheduled tasks)
```

## Most Commonly Confused Concepts

### CLI Entry vs. Gateway Entry

| | CLI | Gateway |
|---|---|---|
| Where is the user | In a terminal | Telegram / Discord / WeChat / ... |
| AIAgent lifecycle | One instance runs start to finish | A new instance is created per message |
| How conversation history is passed | The instance maintains it internally | Loaded from SQLite and passed to the new instance |
| What they share | Both ultimately call `run_conversation()` | Both ultimately call `run_conversation()` |

The key point: **the core loop is exactly the same.** The only difference is how messages arrive and how replies are sent back.

### Platform Adapter vs. Terminal Backend

This is the pair beginners confuse most often.

| | Platform Adapter | Terminal Backend |
|---|---|---|
| What it manages | Which platform messages come from | Where commands run |
| Examples | Telegram adapter, WeChat adapter | Docker backend, SSH backend |
| Which layer | Ingress Translation Layer | Execution Layer |
| Do they depend on each other | No | No |

You can run commands inside Docker while receiving messages from Telegram. The two are completely independent.

### Tool vs. Skill

| | Tool | Skill |
|---|---|---|
| Who writes it | Developers, hardcoded | The agent creates/edits at runtime |
| Where it lives | Python source files | SKILL.md files under the skills/ directory |
| How it executes | Registry dispatches directly to the handler | Executed indirectly through existing tools (e.g., terminal) |
| Can it be modified | Requires code changes | The agent can modify it on its own |

Skills are not a replacement for tools. A skill is "a method the agent uses to do things with tools."

### Tool vs. MCP Tool

| | Built-in Tool | MCP Tool |
|---|---|---|
| Source | Self-registered in code | Exposed by an external MCP Server |
| How the model sees it | Exactly the same | Exactly the same |
| Actual execution | Local handler | Sent to an external server via the MCP protocol |

Transparent to the model -- it does not know and does not need to know whether a tool is built-in or external.

### Memory vs. Session

| | Memory | Session |
|---|---|---|
| Granularity | Curated cross-session information | All messages from one complete conversation |
| Volume | Small (should stay concise) | Large (one per conversation) |
| Where it lives | MEMORY.md / USER.md | SQLite |
| Who writes it | The agent, proactively | The system, automatically |

A session is a complete conversation snapshot. Memory is information the agent considers "useful in the future."

### SOUL.md vs. MEMORY.md vs. HERMES.md

| | SOUL.md | MEMORY.md | HERMES.md |
|---|---|---|---|
| What it is | Persona | Agent's notes | Project rules |
| Who writes it | The user | The agent | Developers |
| Where it lives | HERMES_HOME | HERMES_HOME | Project directory |
| How often it changes | Rarely | Updated frequently | Fixed per project |
| Purpose | Defines who the agent is | Remembers user preferences and project context | Tells the agent the rules for this project |

All three feed into the system prompt, but they are fundamentally different in nature.

### AIAgent Instance vs. Subagent

| | AIAgent (primary) | Subagent |
|---|---|---|
| Who creates it | CLI or Gateway | The primary agent, via the delegate tool |
| messages | Full history of the main conversation | Independent messages list |
| Purpose | Fulfill the user's request | Complete a subtask and return a summary |
| Iteration budget | Shared with the parent | Shared with the parent |

The value of a subagent: offload exploratory work into a clean context without polluting the main conversation.

## Quick Reference

| Entity | Layer | Where It Lives |
|---|---|---|
| User message | Message Source Layer | Terminal / Platform API |
| MessageEvent | Ingress Translation Layer | Internal to Gateway |
| Platform Adapter | Ingress Translation Layer | gateway/platforms/ |
| AIAgent | Conversation Layer | Runtime memory |
| messages[] | Conversation Layer | Runtime memory |
| system prompt | Conversation Layer | Runtime cache + SQLite |
| Tool Entry | Tools & Intelligence Layer | Tool registry |
| Skill | Tools & Intelligence Layer | skills/ directory |
| Memory | Tools & Intelligence Layer | MEMORY.md / USER.md |
| Approval | Tools & Intelligence Layer | Runtime + session cache |
| Subagent | Tools & Intelligence Layer | Runtime (independent instance) |
| Terminal Backend | Execution Layer | Environment manager |
| MCP Server | Execution Layer | MCP client |
| Session | Persistence Layer | SQLite |
| Config | Persistence Layer | config.yaml + .env |
| Cron Job | Persistence Layer | jobs.json |

## How to Use This Map

You do not need to memorize it. Whenever you confuse two terms, come here to check whether they belong to the same layer. If they do not, they are not the same kind of thing -- no matter how similar their names look.

## In One Sentence

**Hermes Agent adds an "Ingress Translation Layer" and an "Execution Environment Layer" on top of a typical agent. These two layers are what enable it to run across platforms. Get clear on their boundaries, and the rest of the chapters will be much easier to follow.**

# Glossary

> This glossary covers the terms most likely to trip up newcomers to the Hermes Agent system.  
> Many of these terms appear in general agent tutorials as well, but within Hermes they carry specific meanings or implementations.

## Recommended Reading

- [`entity-map.md`](./entity-map.md) -- understand which layer each entity belongs to.
- [`data-structures.md`](./data-structures.md) -- understand what these terms look like when they land in code as actual state.

## AIAgent

The core class of Hermes Agent.

It is not a long-running service process. In CLI mode, a single instance runs from start to finish. In Gateway mode, a new instance is created when each user message arrives, loaded with the conversation history, and discarded after the run completes.

This means any state that needs to persist across messages cannot live inside the instance.

## run_conversation

The main method of AIAgent. Takes a user message, runs the conversation loop until the model stops calling tools, and returns the final reply along with the full message history.

It is a regular `def`, not an `async def`. This is one of the most important design choices in Hermes Agent.

## OpenAI-Compatible Interface

Hermes Agent uses the OpenAI Python SDK as its sole API client. All model providers (OpenRouter, Anthropic, local endpoints) are connected by setting `base_url`.

This means the message format is always OpenAI format (`role: user/assistant/tool`), and switching models only requires a config change.

## Tool Registry

A singleton registry. Each tool file automatically calls `register()` to register itself when imported by Python.

The import chain in Hermes Agent is: registry <- tool files <- orchestration layer <- core loop. The registry does not depend on any tool, preventing circular imports.

## Toolset

Tools grouped by function. For example, web search and web scraping belong to the `web` toolset; terminal commands belong to the `terminal` toolset.

When starting the agent, you can toggle groups on and off instead of configuring tools one by one.

## Skill

Unlike tools, skills are capability files that the agent can create, edit, and delete at runtime.

A skill file is simply a SKILL.md describing how to use a particular capability. The agent executes skills through existing tools (such as the terminal), rather than bypassing the tool system.

This is where Hermes Agent differs from most agent frameworks: not all capabilities are hardcoded -- some are managed by the agent itself.

## Gateway

A multi-platform message gateway. Its job is to listen to multiple messaging platforms simultaneously, route incoming messages to the agent loop, and send replies back to the appropriate platform.

A single Gateway process can serve Telegram, Discord, WeChat, and over a dozen other platforms at the same time.

## Platform Adapter

Inside the Gateway, each platform has its own adapter.

An adapter is responsible for: connecting to the platform, receiving messages, converting them into a unified format, and translating the agent's replies back into the platform's format for delivery.

All adapters conform to the same base class interface. Adding a new platform only requires writing a new adapter.

## MessageEvent

The unified format that adapters convert platform-specific messages into.

Regardless of whether a message came from Telegram or WeChat, the Gateway receives the same structure. This allows the core loop to be completely unaware of which platform a message originated from.

## Terminal Backend / Execution Environment

Where terminal commands are actually executed.

Hermes Agent abstracts an execution environment interface with six implementations: local process, Docker container, SSH remote, Modal serverless, Daytona serverless, and Singularity container.

Transparent to the tool layer -- tools just issue commands without caring where they run.

## SessionDB (Session Store)

A SQLite database that stores metadata and the complete message history of all sessions.

Uses WAL mode to support concurrent reads and writes (for the multi-platform Gateway scenario), and FTS5 to support full-text search across historical sessions.

## FTS5

SQLite's full-text search extension. Lets you quickly search text content across a large volume of historical sessions.

## Context Compression

When a conversation grows long, the system automatically uses an LLM to summarize the history, replacing the original messages with the summary.

In Hermes Agent, compression triggers session splitting: after compression, a new session is created and linked to the old one via `parent_session_id`. This way the old, complete history is never lost.

## SOUL.md

The persona file. Located in the HERMES_HOME directory, it defines the agent's identity and behavioral style.

Read at the start of each conversation and placed at the very beginning of the system prompt.

## MEMORY.md / USER.md

Memory files. They store information that remains valuable across sessions (user preferences, project context, etc.).

The difference from SOUL.md: SOUL.md is the persona (who the agent is), MEMORY.md is memory (what the agent knows).

## HERMES.md / AGENTS.md

Project-level configuration files. Placed in the project directory, they tell the agent the rules and context for this project.

Similar to CLAUDE.md or .cursorrules in other agent frameworks, but Hermes Agent supports multiple filenames and uses them in priority order.

## Profile

A fully isolated runtime environment. A single user can have multiple profiles, each with its own config, memory, sessions, and skills.

Use case: the same person might have a "development" profile and a "writing" profile, with completely different personas, memories, and tool configurations.

## Approval / Dangerous Command Detection

A safety check before executing terminal commands.

The system maintains a list of dangerous command patterns (regular expressions). Commands are matched against this list before execution. A match triggers the approval flow: in CLI mode the user is prompted for confirmation; in Gateway mode an approval button is sent.

## Iteration Budget

The maximum number of API calls allowed in a single conversation. Defaults to 90.

This is not just a safeguard against infinite loops. In subagent scenarios, the parent and child share the same budget, making it an explicitly managed resource.

## Failover

When an API call fails, the system classifies the error and decides whether to retry or switch to a fallback model.

Examples: rate-limited -> back off and retry; context too long -> trigger compression; authentication failure -> switch credentials.

## MCP

Model Context Protocol. Lets the agent connect to external tools through a unified protocol.

Once an MCP tool is connected, it looks exactly like a built-in tool to the model. The model does not need to know whether a tool is built-in or external.

## Cron Job

Lets the agent automatically perform work at a future time.

Supports three scheduling formats: one-time delay (`30m`), recurring interval (`every 2h`), and standard cron expressions (`0 9 * * *`).

## Most Commonly Confused Concepts

| Pair | How to Tell Them Apart |
|---|---|
| CLI vs. Gateway | CLI is a single-user terminal entry point; Gateway is a multi-platform message gateway. The core loop is the same. |
| Tool vs. Skill | A tool is a hardcoded capability; a skill is a capability file managed by the agent at runtime. |
| Execution Environment vs. Platform Adapter | The execution environment manages "where commands run"; the adapter manages "where messages come from." |
| Session vs. Memory | A session is the complete record of one conversation; memory is curated information that spans sessions. |
| SOUL.md vs. MEMORY.md | SOUL.md is the persona (stable); MEMORY.md is memory (updated over time). |
| HERMES.md vs. MEMORY.md | HERMES.md contains project rules (instructions for the agent); MEMORY.md contains knowledge the agent has accumulated on its own. |
| Iteration vs. Turn | An iteration is one API call. A turn may contain multiple iterations (if the model calls tools consecutively). |

---

If you hit an unfamiliar term while reading the docs, come back here first rather than pushing through.

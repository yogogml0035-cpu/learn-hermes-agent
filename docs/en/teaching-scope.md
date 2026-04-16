# Teaching Scope

> This document explains what this teaching repository covers and what it does not.  
> Hermes Agent is a large system. Not everything in it is suited for ground-up instruction.

## Goal of This Repository

**Teach developers to understand and rebuild the core architecture of a cross-platform autonomous AI Agent.**

Note the words "cross-platform."

Most agent tutorials only teach "one person chatting with an AI in a terminal." Hermes Agent was designed from the start to handle multiple platforms, multiple execution environments, and multiple model providers. These are not afterthoughts -- they shaped the core design.

That means this curriculum covers a broader scope than a simple "agent loop + tools":

- How to make the same loop serve both CLI and Gateway entry points
- How to run tools in different execution environments
- How to let the agent manage its own skill files
- How to use SQLite instead of the file system for persistence
- How to support any model through an OpenAI-compatible interface

## What Must Be Explained Clearly

### The Core Loop

How the synchronous loop works, why synchronous was chosen, how async tools are bridged in, and what the instance lifecycle looks like in Gateway mode.

### Tool Self-Registration

What the import chain looks like, why the registry does not depend on any tool, and why adding a new tool does not require changes to other files.

### Persistence

Why SQLite over files, what problem WAL mode solves, what problem FTS5 solves, and how session splitting and `parent_session_id` work.

### Multi-Source System Prompt

The priority order of SOUL.md, MEMORY.md, HERMES.md, AGENTS.md, and .cursorrules; how they are assembled into a single system prompt; and why the system prompt is cached.

### Gateway and the Adapter Pattern

What the Gateway is, what interface the adapter base class defines, how messages go from a platform-specific format to a unified format, and how sessions are isolated by `chat_id`.

### Execution Environment Abstraction

What interface BaseEnvironment defines, what problem each of the six implementations solves, and why the tool layer does not need to know where commands run.

### Skill Lifecycle

How skills differ from tools, how the agent creates and edits skill files, and how skills are executed through existing tools.

## What Should Not Take Up Main-Line Space

- **Nix flake / Docker packaging details** -- how to build distribution packages is not core architecture
- **Skins and theming engine** -- KawaiiSpinner is cute but not a teaching priority
- **Nous Portal subscriptions and billing** -- commercial integrations are not core mechanisms
- **RL training pipeline** -- batch_runner and trajectory_compressor are standalone subsystems
- **Platform-specific API formats** -- Telegram inline keyboards, WeChat XML message structures, etc. are platform details, not architectural knowledge
- **Legacy migration logic** -- the migration branches in config.py are tech debt, not design decisions
- **Landing pages and marketing materials** -- self-explanatory

## Teaching Challenges Unique to Hermes Agent

### The Mental Overhead of Two Entry Points

Readers need to understand that CLI and Gateway call the same loop, yet their entry-point behavior differs (CLI uses one long-lived instance; Gateway creates a new instance per message).

Teaching strategy: Phases 1-2 cover CLI only. Phase 3 introduces the Gateway. Let the core loop solidify first.

### Six Execution Environments

Commands issued by tools may run on local, Docker, SSH, Modal, Daytona, or Singularity.

Teaching strategy: Phases 1-2 cover local only. Phase 3 introduces the environment abstraction. Let the tool system solidify first.

### Seventeen Platform Adapters

The Gateway has seventeen platform adapters, each with its own message format and callback mechanism.

Teaching strategy: Cover only the base class interface and the unified message format. Walk through one concrete platform as a full example. Mention the others as "the same pattern."

### Multi-Source System Prompt

The system prompt is assembled from five or six sources, with priority ordering and caching logic.

Teaching strategy: Start with "there are multiple sources, combined together." Expand into the full assembly logic and caching strategy in `s04`.

## Recommended Structure for Each Chapter

1. `What problem this chapter solves` -- start with WHY
2. `A few terms to define first` -- define new concepts up front
3. `Minimal mental model` -- one diagram or one block of pseudocode
4. `Key data structures` -- what the state of this mechanism looks like
5. `Minimal implementation` -- runnable code
6. `Hermes Agent's unique design` -- what is special about this mechanism in Hermes
7. `Common beginner mistakes` -- pitfall prevention
8. `Teaching boundary` -- where this chapter deliberately stops

Pay special attention to point 6. This is what sets this curriculum apart from generic agent tutorials. Every chapter should call out what is distinctive about Hermes Agent's design choices for the mechanism at hand, rather than just explaining the general concept.

## Maintainer Checklist

- Does this chapter clearly explain "how Hermes differs from the common approach for this mechanism"
- Did it accidentally turn into a generic agent tutorial (interchangeable with learn-claude-code by swapping names)
- Did it prematurely introduce Gateway or multi-environment complexity in Phase 1
- Do code examples use OpenAI format (not Anthropic format)
- Does it reference any class names or function names that do not exist (hallucination check)

## In One Sentence

**The core of this curriculum is not "how to write an agent," but "how to write an agent that can simultaneously serve a terminal and over a dozen messaging platforms."**

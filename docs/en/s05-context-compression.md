# s05: Context Compression

`s00 > s01 > s02 > s03 > s04 > [ s05 ] > s06 > s07 > s08 > s09 > s10 > s11 > s12 > s13 > s14 > s15 > s16 > s17 > s18 > s19 > s20 > s21 > s22 > s23 > s24 > s25 > s26 > s27`

> *More context is not always better. What matters is keeping the "still useful parts" in the active working surface.*

## What problem does this chapter solve

By `s04`, the agent can call tools, persist sessions, and assemble prompts.

Precisely because it does more now, the context balloons faster:

- Reading a large file stuffs in a lot of text
- Running a long command produces massive output
- After many rounds of tool calls, old results pile up

Without a compression mechanism, these problems surface quickly:

1. The model's attention gets drowned out by old results
2. API requests grow heavier and more expensive
3. Eventually the context limit is hit and the task breaks

So what this chapter really solves is:

**How to free up space in the active context without losing continuity of work.**

## Key terms explained

### What is a context window

Think of the context window as:

> The total input capacity the model can actually see in one turn.

It is not infinite. For example, 200K tokens.

### What is active context

Not everything that has ever appeared in history needs to stay in the window at all times.

Active context is more like:

> The portion that is most worth showing the model right now to continue working.

### What is compression

The compression here is not ZIP file compression.

It means:

> Representing information in a shorter form while preserving what is actually needed to continue working.

![Context Compression Three Layers](../../illustrations/s05-context-compression/01-infographic-three-layers.png)

## Minimal mental model

For this chapter, start by remembering three layers -- do not try to memorize the full algorithm upfront:

```text
Layer 1: Trim old tool outputs first
  -> No LLM needed, pure string replacement
  -> Replace long-ago tool results with a placeholder

Layer 2: Protect head and tail, compress only the middle
  -> Head (task definition) stays untouched
  -> Tail (recent work) stays untouched
  -> Only compress middle turns that have already been "consumed"

Layer 3: Summarize the middle section with an LLM
  -> Call a cheap auxiliary model
  -> Generate a structured summary to replace the original text
```

Visualized:

```text
messages (100 items, 150K tokens)
   |
   +-- Layer 1: old tool results -> "[Old tool output cleared]"
   |   (No LLM needed; sheds a batch of tokens upfront)
   |
   +-- Still too long?
   |
   +-- Layer 2: find boundaries
   |   Head: first N messages (untouched)
   |   Tail: most recent ~20K tokens (untouched)
   |   Middle: the part to be compressed
   |
   +-- Layer 3: middle section -> auxiliary LLM -> structured summary
   |
   v
new messages = [head] + [summary] + [tail]
```

These three layers are progressive: Layer 1 is the cheapest (free), Layer 2 is a boundary calculation, and Layer 3 is where the LLM is actually called.

![Before vs After Compression](../../illustrations/s05-context-compression/02-comparison-before-after.png)

## What must survive compression

This is where the chapter is easiest to hand-wave.

Compression is not simply "shorten the history." What actually matters is:

**Enabling the model to keep working where it left off.**

A proper summary must preserve at least the following:

1. What the current task's goal is
2. Which key actions have been completed
3. What important decisions have been made
4. Which files have been modified or closely examined
5. What the next step should be

If these are lost, the compression freed up space but broke continuity.

Hermes Agent uses a structured summary template to ensure this information is retained:

```text
## Goal
...
## Progress
...
## Key Decisions
...
## Files Modified
...
## Next Steps
...
```

Not free-form text, but a defined format. This makes it much easier for the model to extract key information from the summary.

## Key data structures

### 1. Tool output placeholder

The `content` of old tool messages is replaced with:

```text
[Old tool output cleared to save context space]
```

This is Layer 1. No LLM needed, pure string replacement, but it can eliminate a large number of tokens upfront (tool output is typically verbose).

### 2. Compression boundaries

```python
{
    "head_end": 3,        # First 3 messages stay untouched
    "tail_start": 85,     # From message 85 onward is the tail, stays untouched
    "middle": [3:85],     # These in the middle are to be compressed
}
```

The tail is not a fixed "last N messages" -- it is calculated by token budget (approximately 20K tokens). This keeps the preserved information roughly stable regardless of how long or short recent messages are.

### 3. Post-compression summary message

```python
{
    "role": "user",
    "content": "[CONTEXT COMPACTION] Earlier turns were compacted...\n\n"
               "## Goal\n...\n## Progress\n...\n## Key Decisions\n..."
}
```

Dozens of messages in the middle are replaced by this single summary.

## Minimal implementation

### Step 1: Trim old tool outputs first

```python
def prune_old_tool_results(messages, keep_recent=3):
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for idx in tool_indices[:-keep_recent]:
        messages[idx] = {**messages[idx], "content": "[Old tool output cleared]"}
    return messages
```

The key idea behind this step:

> Tool outputs are usually long, but for subsequent work you only need to know "what was called and roughly what happened." No LLM required -- trim the easiest wins first.

### Step 2: Find the compression boundaries

```python
def find_boundaries(messages, protect_first, tail_token_budget):
    head_end = protect_first
    
    # Count backward from the end, accumulating up to tail_token_budget
    tail_start = len(messages)
    tail_tokens = 0
    for i in range(len(messages) - 1, head_end - 1, -1):
        msg_tokens = len(str(messages[i].get("content", ""))) // 4
        if tail_tokens + msg_tokens > tail_token_budget:
            break
        tail_tokens += msg_tokens
        tail_start = i
    
    return head_end, tail_start
```

The key idea behind this step:

> Protect the head (task definition) and protect the tail (recent work). Only compress the middle.

### Step 3: Summarize with an auxiliary LLM

```python
def summarize_middle(turns, previous_summary=None):
    prompt = "Summarize these conversation turns.\n"
    prompt += "Use sections: Goal, Progress, Key Decisions, Files Modified, Next Steps.\n\n"
    
    if previous_summary:
        prompt += f"Previous summary to update:\n{previous_summary}\n\n"
    
    for msg in turns:
        prompt += f"[{msg['role']}] {str(msg.get('content', ''))[:500]}\n"
    
    return call_auxiliary_llm(prompt)  # Use a cheap model, not the main one
```

Two important points:

1. **Use an auxiliary model** (cheap, fast), not the main model. Compression is a system operation and should not spend the expensive model's budget.
2. **If a previous compression exists, pass in the old summary.** The new summary becomes an "update to the old summary" rather than "written from scratch," which loses less information.

### Step 4: Assemble

```python
def compress(messages, protect_first, tail_token_budget):
    messages = prune_old_tool_results(messages)
    head_end, tail_start = find_boundaries(messages, protect_first, tail_token_budget)
    
    middle = messages[head_end:tail_start]
    summary = summarize_middle(middle)
    
    return (
        messages[:head_end]
        + [{"role": "user", "content": f"[CONTEXT COMPACTION]\n{summary}"}]
        + messages[tail_start:]
    )
```

### Step 5: Hook into the main loop

```python
# At the top of the loop in run_conversation()
if estimate_tokens(messages) >= threshold:
    messages = compress(messages, protect_first=3, tail_token_budget=20000)
```

Starting from this chapter, the main loop is no longer just about "call the model + run tools." It takes on an additional responsibility: **managing the active context budget.**

## Hermes Agent's unique design choices here

### 1. Preflight compression

`run_conversation()` checks the token count before entering the main loop. If the count already exceeds the limit (for example, the user switched from a large-window model to a small-window model), compression happens before the first API call.

Do not wait for the API to return an error. Proactive defense is better than reactive recovery.

### 2. Compression triggers session splitting

After compression, a new session is created with `parent_session_id` pointing to the old session (see `s03`). The old session's full history is never deleted and can be traced through the chain.

### 3. System prompt rebuild

After compression, the cached system prompt is invalidated (because memory may have changed) and needs to be reassembled. This is the exception to the `s04` prompt caching mechanism.

### 4. Orphaned tool_call cleanup

After compression, some assistant messages may have `tool_calls` whose corresponding `tool_result` messages were compressed away. The API requires every pair to match. These orphans must be cleaned up after compression.

## Common beginner mistakes

### 1. Thinking compression means deletion

It does not. More precisely, compression replaces content that "does not need to stay in the active context" with a different representation. The old history is preserved through the session chain.

### 2. Only handling the limit after hitting it

A better approach is the three progressive layers: trim old outputs first, find boundaries, then summarize. Do not jump straight to calling the LLM.

### 3. Writing a summary that says nothing useful

If the summary does not preserve the goal, decisions, files, and next steps, it is useless for continuing work.

### 4. Using the main model for summarization

Compression is a system operation, not a user request. A cheap auxiliary model is sufficient.

### 5. Not protecting the tail

If the most recent messages are compressed too, the agent immediately forgets what it was just doing.

## Teaching boundaries

This chapter should not slide into "an encyclopedia of all compression techniques."

The teaching version only needs to cover three things clearly:

1. Trim old tool outputs first (free)
2. Protect head and tail, compress only the middle
3. Use an auxiliary LLM to generate a structured summary that preserves work continuity

Deliberately deferred: precise token counting, multi-pass iterative compression strategies, passive compression triggered by API errors (-> `s06`).

If the reader can get the agent to "automatically compress the middle when the conversation exceeds a threshold, leave head and tail untouched, and preserve key information in the summary," this chapter has achieved its goal.

## One line to remember

**The core of context compression is not minimizing text length, but letting the model maintain continuity of work within a shorter active context.**

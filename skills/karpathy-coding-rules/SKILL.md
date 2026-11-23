---
name: karpathy-coding-rules
category: engineering
description: Discipline rules to stop over-engineering, unverified assumptions, and unrelated edits.
scope: agent
recommended_tools: [read_file, edit_file_diff, execute_shell_command]
min_model_tier: basic
---

# Karpathy Agent Coding Rules

1. **Think Before Coding**: State assumptions explicitly before editing. If requirements are ambiguous, ask or surface trade-offs instead of guessing silently.
2. **Simplicity First**: Write the minimum code that solves the problem cleanly. Reject speculative abstractions, unused flags, or premature frameworks for single-use logic.
3. **Surgical Changes**: Touch ONLY lines strictly required by the task. Preserve existing comments, formatting, and unrelated behavior. Never silently rewrite untouched modules.
4. **Goal-Driven Execution**: Convert every request into verifiable acceptance checks (test command, syntax compile, API status) and verify them directly after editing before claiming completion.
5. **No Duplicate Noise**: Reuse existing project helpers instead of re-implementing utilities; keep responses concise and evidence-backed.

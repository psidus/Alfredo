---
name: test-driven-development
category: engineering
description: Red-Green-Refactor workflow with focused, deterministic unit tests.
scope: task
recommended_tools: [read_file, edit_file_diff, execute_shell_command]
min_model_tier: standard
---

# Test-Driven Development (Red-Green-Refactor)

1. **Red — Write a Failing Check First**: Before implementing behavior, define or run the smallest assertion reproducing the missing behavior or bug. Confirm it fails for the expected reason.
2. **Green — Minimal Passing Code**: Write only the clean production logic needed to pass the check without breaking existing interfaces.
3. **Refactor — Remove Duplication**: Check for repeated logic across your change, simplify branching with early returns, and re-run the test suite to confirm 100% pass.
4. **Isolation**: Keep unit tests deterministic (no flaky live external calls unless explicitly integration-testing; mock or use temporary workspace paths).

---
name: systematic-debugging
category: engineering
description: 4-phase root-cause investigation before writing fixes (Superpowers method).
scope: task
recommended_tools: [read_file, edit_file_diff, inspect_codebase_graph]
min_model_tier: standard
---

# Systematic Debugging (4 Phases)

Never guess-and-patch blindly. Follow these 4 sequential phases:

1. **Reproduce & Inspect Evidence**: Read the exact traceback, HTTP error, or failing test output. Pinpoint file, line number, and inputs that trigger the failure.
2. **Trace Root Cause Upstream**: Follow data flow backwards from the symptom to its origin (invalid DB row, uninitialized config, wrong type). Map affected callers with `inspect_codebase_graph` before changing interfaces.
3. **Minimal Root Fix**: Fix the underlying defect cleanly in one place rather than adding band-aid try/except wrappers around symptoms. Preserve existing contracts.
4. **Regression Verification**: Run a targeted command/test proving the original reproduction passes AND adjacent call sites remain green.

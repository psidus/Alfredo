---
name: writing-plans
category: planning
description: Produce compact, step-by-step execution plans with exact file paths, edits, and checks.
scope: task
recommended_tools: [inspect_codebase_graph, read_file]
min_model_tier: standard
---

# Writing Actionable Implementation Plans

1. **Verify Target Surface First**: Inspect actual directory layout and symbol names before naming files or functions in a plan.
2. **Atomic Steps**: Break work into small ordered steps, each stating:
   - Exact file path(s) to create or modify.
   - Exact contract / function signature / DB column changes.
   - Exact verification command and expected pass criteria.
3. **Zero Overlap Check**: Ensure new helpers don't duplicate existing utilities and new UI controls don't repeat adjacent controls.
4. **Token & Resource Budget**: Account for 16 GB VRAM local execution constraints; keep injected prompt context lean and modular.

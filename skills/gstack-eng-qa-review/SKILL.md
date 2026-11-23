---
name: gstack-eng-qa-review
category: engineering
description: Architecture review, failure modes, edge-case audit, and release checklist (gstack Eng/QA mode).
scope: task
recommended_tools: [inspect_codebase_graph, read_file]
min_model_tier: standard
---

# Engineering & QA Release Audit (gstack Style)

1. **Boundary & Trust Audit**: Check auth dependencies, path traversal protection, DB transaction atomicity, and null/empty edge cases across new endpoints.
2. **Failure Modes & Rollback**: Identify what fails silently (timeouts, remote host unreachable, missing env var, malformed JSON) and verify actionable error surfacing.
3. **State & Sync**: Verify frontend state, Service Worker cache invalidation, and DB schema migrations remain backward-compatible with existing data.
4. **Sign-Off Matrix**: Summarize tested vs. untested paths cleanly with concrete pass/fail command evidence.

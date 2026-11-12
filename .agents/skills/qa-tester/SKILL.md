---
name: qa-tester
description: >-
  Use this skill to aggressively test newly implemented or modified code: look for edge cases and write or update its unit tests.
---

# Role: Destructive QA

Your goal is to break the code, not to confirm the happy path. The test and verification rules of `CLAUDE.md` (§6 and §7) apply in full.

## Process

1. **Map the weak points** of the code before writing tests and lay them out in your answer. Consider, as applicable: `None` and empty strings, unexpected types, misaligned rows or columns, duplicate or missing UUIDs, NetBox API failures (`RequestError`, network down), re-runs (idempotency) and `--dry-run` writing nothing.
2. **Write the tests** against the expected robust behavior, not against the current behavior. Each test must make clear which destructive scenario it validates.
3. **If a test reveals a bug, never adapt the test to hide it.** Report the bug, fix the source code and repeat. The cycle ends when all the tests pass with the fixed code.

## Result

List of edge cases evaluated, tests added or modified, bugs found with their fix, and the result of the full suite.

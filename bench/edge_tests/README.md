# Edge-case tests (tuning split only)

Extra tests for each tuning task, beyond the upstream fix's own hidden tests: other inputs for the behaviour the
task's instruction asks for, and cases the instruction names that the hidden tests leave out. They look past
pass/fail: a fix can pass the hidden tests and still be incomplete. `python3 -m modelpilot.regrade <run>` runs
them against every saved fix of a benchmark run ($0, no model requests).

Rules (user decision, September 29, 2026):

1. Written from the task's instruction, its reference (upstream) fix and its hidden tests only, before looking
   at any arm's fix. Written September 29 without opening an `agent.diff`; only per-arm scope medians had been
   seen.
2. A suite is used only if the task's reference fix passes all of it (`regrade.check_edge`, `usable`), so no
   test checks behaviour the real fix never had. The base (buggy) tree is also run, for information.
3. Tuning tasks only. Final-split task specs are hash-locked (`bench/splits.json`) and get no edge tests.
4. Frozen once results against agent fixes exist: a change is a new suite, recorded with its reason, and every
   re-grade records each file's SHA-256.

Layout: `bench/edge_tests/<task id>/test_*.py`, plain `unittest`, copied into the graded tree's `_edge/` and run
as `python -m unittest discover -v -s _edge -t _edge` with the task's `pythonpath`, the grader's isolation (no
credentials, fresh HOME/TMPDIR) and the benchmark Python.

Reference and base results when frozen (September 29, `work/bench/py312`):

| Task | Tests | Base passes |
| --- | --- | --- |
| cachetools-cache-key | 5 | 0 |
| cachetools-setitem-evict | 5 | 1 |
| cachetools-tlru-stale | 4 | 3 |
| cachetools-ttl-expire | 6 | 0 |
| mi-is-sorted-lt-only | 3 | 0 |
| mi-last-reversed-none | 4 | 2 |
| mi-numeric-range-consistent | 4 | 0 |
| mi-one-falsy-exception | 5 | 3 |
| mi-running-minmax-stable | 3 | 0 |
| mi-sample-strict-counts | 4 | 0 |
| parse-decimal-grouping | 5 | 1 |
| parse-hyphen-field | 5 | 0 |
| tomli-hex-escape | 5 | 2 |
| tomli-inline-table-newlines | 6 | 2 |
| tomli-loads-typeerror | 3 | 0 |
| tomli-optional-seconds | 4 | 2 |

Harder tuning tasks (added September 29, before any trial ran on them; same rules):

| Task | Tests | Base passes |
| --- | --- | --- |
| cachetools-cached-condition | 5 | 2 |
| cachetools-tlru-cache | 6 | 0 (the module has no `TLRUCache`, so the suite fails to import) |
| mi-reshape-multidim | 5 | 1 |
| mi-running-statistics | 5 | 0 |
| parse-strftime-directives | 6 | 0 |
| tomli-decode-error-attrs | 4 | 0 |
| toolz-compose-annotations | 6 | 0 |

Two of these tests were corrected before freezing, both test bugs found by running them on the reference: the
TLRU `popitem()` case expected the wrong item after an eviction, and a condition test paired a lock with a
condition built on a different lock. `cachetools-cached-condition`'s threaded tests passed three runs in a row.

The reference fix passes every test. A base that passes some tests is expected: those tests target incomplete
fixes (for example, a key evicted while it is being updated), not the original bug. `cachetools-setitem-evict`'s
growth test was changed before freezing, because the buggy base happened to end in the same state.

# Edge-case tests

Extra tests for each tuning task, and since October 18, 2026 each final task, beyond the upstream fix's own hidden tests: other inputs for the behaviour the
task's instruction asks for, and cases the instruction names that the hidden tests leave out. They look past
pass/fail: a fix can pass the hidden tests and still be incomplete. `python3 -m modelpilot.regrade <run>` runs
them against every saved fix of a benchmark run ($0, no model requests).

Rules (user decision, September 29, 2026):

1. Written from the task's instruction, its reference (upstream) fix and its hidden tests only, before looking
   at any arm's fix. Written September 29 without opening an `agent.diff`; only per-arm scope medians had been
   seen.
2. A suite is used only if the task's reference fix passes all of it (`regrade.check_edge`, `usable`), so no
   test checks behaviour the real fix never had. The base (buggy) tree is also run, for information.
3. Tuning tasks only, until October 18, 2026. Then (user decision, October 18, before any final trial ran) each
   final task got a suite too, by these same rules. The final task specs stay hash-locked and unchanged
   (`bench/splits.json`): a suite is a separate file, not part of the spec.
4. Frozen once results against agent fixes exist: a change is a new suite, recorded with its reason, and every
   re-grade records each file's SHA-256.
5. Since October 4, 2026 (user decision) edge tests count: a trial passes only if it passes the hidden grader
   and every test of its task's edge suite. `bench` runs the suite on the graded tree after the hidden grader
   (`grade.edge_passed`, `pass_rule`), its run-start check stops a run whose suite the reference doesn't pass in
   full, and the manifest records each suite's SHA-256. `bench_report` takes the edge results of runs graded
   before then from their latest re-grade (`edge_missing` when there is none) and reports hidden-test passes
   alongside. Since October 18 the final tasks have suites too (rule 3), so final trials are graded the same way.

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

networkx tuning tasks (added October 4, before any trial ran on them; same rules). Where they can, they compare
against brute force or VF2 on small random graphs:

| Task | Tests | Base passes |
| --- | --- | --- |
| nx-connectivity-digraph-cuts | 6 | 1 |
| nx-classes-weak-views | 6 | 5 |
| nx-ismags-monomorphism | 6 | 0 |
| nx-vf2-isolated-nodes | 4 | 2 |
| nx-bipartite-butterflies | 5 | 1 |
| nx-dag-antichain-width | 5 | 0 |

One test was dropped before freezing, a test bug found by running it on the reference: it assumed ISMAGS's existing
subgraph isomorphisms equal VF2's, which they don't on random graphs with self-loops, before or after the fix. The
ISMAGS suite leaves out directed multigraphs: there the upstream fix itself accepts some mappings that put two
parallel subgraph edges on one graph edge (9 of 40 random cases), which VF2 rejects.

Two of these tests were corrected before freezing, both test bugs found by running them on the reference: the
TLRU `popitem()` case expected the wrong item after an eviction, and a condition test paired a lock with a
condition built on a different lock. `cachetools-cached-condition`'s threaded tests passed three runs in a row.

The reference fix passes every test. A base that passes some tests is expected: those tests target incomplete
fixes (for example, a key evicted while it is being updated), not the original bug. `cachetools-setitem-evict`'s
growth test was changed before freezing, because the buggy base happened to end in the same state.

Plan item 8's labelling batch (added October 6, 2026, before any trial ran on them; same rules). Each was written
with its task, from the instruction, the reference fix and the hidden tests, and run on reference and base before
freezing. Where they can, they compare against a brute-force or by-definition computation on small random inputs
(dominators and frontiers, perfection, k-components, centroids, triangles, sampled edge betweenness, Floyd-Warshall
against Bellman-Ford). Four tests were corrected before freezing, all test bugs found by running them on the
reference: an assertion on a module-level `__all__` more-itertools doesn't have, a wrong expected `argmax`, periodic
lattice sizes the generators refuse, and a TLRU test of behaviour the instruction doesn't specify (dropped).

| Task | Tests | Base passes |
| --- | --- | --- |
| cachetools-cached-none-deprecated | 4 | 2 |
| cachetools-tlru-expire-pairs | 3 | 0 |
| mi-argmin-argmax | 5 | 0 |
| mi-bucket-phantom-keys | 5 | 2 |
| mi-exactly-n-negative | 4 | 3 |
| mi-extract-monotonic | 5 | 0 |
| mi-nth-permutation-r-too-large | 3 | 2 |
| mi-numeric-range-eq-hash | 5 | 2 |
| mi-numeric-range-reversed-values | 5 | 3 |
| mi-seekable-getitem | 3 | 0 |
| mi-serialize | 3 | 0 |
| mi-split-maxsplit-zero-empty | 4 | 2 |
| mi-subfactorial | 5 | 0 |
| mi-zip-broadcast-single-open | 5 | 3 |
| nx-all-triangles | 4 | 0 |
| nx-dominance-definitions | 4 | 0 |
| nx-dominating-set-greedy-cost | 4 | 1 |
| nx-edge-betweenness-k-scaling | 4 | 3 |
| nx-floyd-warshall-negative-cycle | 4 | 0 |
| nx-generalized-petersen | 4 | 0 |
| nx-gexf-dynamic-booleans | 3 | 2 |
| nx-hyper-wiener-index | 4 | 0 |
| nx-is-perfect-graph | 5 | 0 |
| nx-k-components-lost | 4 | 3 |
| nx-lattice-node-attributes | 4 | 2 |
| nx-nonisomorphic-trees-small-orders | 4 | 0 |
| nx-planar-embedding-faces | 4 | 0 |
| nx-tree-centroid | 5 | 0 |
| tomli-key-parts-limit | 3 | 0 |
| tomli-parse-float-illegal-types | 4 | 1 |

sqlglot stage 1, harder tuning tasks (added October 10, 2026, before any trial ran on them; same rules). Each suite was
run against fresh reference and base trees (`bench_tasks.task_tree`) with the benchmark Python. Expectations were
corrected only where sqlglot's unrelated normalization differed from the draft (type names, `NUMERIC` to `DECIMAL`,
how it writes `TO_DAYS`, colons in `STRUCT` fields, a parenthesis node around `a || b`); every test checks the
behaviour its task's instruction asks for. The SQLite suite also checks meaning, not only text: each rewritten
expression must evaluate in SQLite (3.53) to what the input does.

| Task | Tests | Base passes |
| --- | --- | --- |
| sqlglot-clickhouse-modify-column | 4 | 0 |
| sqlglot-collate-nested-types | 4 | 1 |
| sqlglot-duckdb-macro-overloads | 4 | 0 |
| sqlglot-mysql-alter-options | 4 | 1 |
| sqlglot-pivot-literal-aliases | 3 | 1 |
| sqlglot-postgres-param-modes | 4 | 1 |
| sqlglot-sqlite-json-arrow-precedence | 4 | 2 |
| sqlglot-starrocks-partitions | 4 | 0 |

sqlglot stage 2, harder tuning tasks (added October 11-12, 2026, before any trial ran on them; same rules). Each suite
was run against fresh reference and base trees (`bench_tasks.task_tree`) with the benchmark Python, and checks only
what its task's instruction states. Where the reference writes one of several equivalent forms (three nested DAX
FILTERs come out as `(a AND b) AND c`), the test checks the parsed tree instead of the text. The SQLite suite also runs
what sqlglot writes in SQLite and checks the column's storage (`PRAGMA table_xinfo`).

| Task | Tests | Base passes |
| --- | --- | --- |
| sqlglot-array-append-nulls | 4 | 1 |
| sqlglot-dax-dialect | 5 | 0 |
| sqlglot-from-first-multi-insert | 5 | 0 |
| sqlglot-snowflake-corr-nan | 5 | 2 |
| sqlglot-snowflake-filter-within-group | 5 | 1 |
| sqlglot-sqlite-generated-columns | 4 | 1 |
| sqlglot-postgres-create-trigger (optional, branch `sqlglot-trigger-task`) | 5 | 1 |

Known label caveat (user decision, October 6, 2026: noted, suite unchanged):

- `tomli-decode-error-attrs`, `test_line_starts_and_keywords`: it requires keyword construction,
  `TOMLDecodeError(msg='m', doc='ab', pos=0)`, to take the new path without a `DeprecationWarning`, as the reference
  fix does. The instruction names the parameters (`TOMLDecodeError(msg, doc, pos)`) but lists the deprecated calls
  only as "no arguments, fewer or more than three, or arguments that are not (str, str, int)", so it neither asks
  for keywords nor rules them out. A fix taking `*args` only reads the instruction one defensible way and fails this
  test; every Sonnet 5.5 miss on the task and both Opus 5.5 misses are this test alone. Strict results on the task
  stand as recorded; read them as partly the instruction's ambiguity, not only the model's
  (`runs/miss-predictability-20261006.json`).

Final split (added October 18, 2026, before any trial ran on any final task, so no agent's fix existed to look at;
user decision: tuning showed 7 of 8 misses failing only an edge suite, and without suites the final tasks would be
graded on hidden tests alone). Each was written from its task's instruction, reference fix and hidden tests, checks
only what the instruction states, and was run with the benchmark Python against fresh reference and base trees, twice,
with the same results (`regrade.check_edge`). Where they can, they compare against an independent computation:
`zoneinfo` for the US timezones, a list for `BarrelList` and `IndexedSet`, a Python evaluator for random marker trees,
the instruction's own rules for random short-help texts. Base passes count distinct tests (a test with failing subtests
counts once). Corrected before freezing, all found by running the suites, none by looking at an agent's fix:

- `click-short-help`: the random texts used negative limits, where the reference returns an empty text unchanged and
  the rules as written give `...`; the instruction doesn't settle it, so limits start at 0. A known abbreviation
  (`vs.`, `e.g.`, `etc.`) is always followed by a lowercase word: whether `vs. Pears` ends a sentence is the one case a
  fix could read either way.
- `click-prompt-strip-ansi`: the check outside `CliRunner` reads what reaches the output stream, not the text handed to
  the prompt function, so a fix that writes the prompt another way isn't failed for that.
- `packaging-metadata-fold`: two control characters (`\x1f`, `\x0e`) dropped from the "not a line boundary" check: the
  instruction lists the boundaries but doesn't say every other character must be accepted.
- `boltons-indexedset-negative`: a pop loop could run forever on the base once a failure left the set and the list
  apart; a trial now stops at its first failure.

| Task | Tests | Base passes |
| --- | --- | --- |
| boltons-bulk-removal | 9 | 4 |
| boltons-indexedset-negative | 4 | 1 |
| boltons-isoparse-fraction | 4 | 1 |
| boltons-jsonl-eof | 4 | 0 |
| boltons-table-to-text | 4 | 0 |
| boltons-us-tz-fold | 5 | 1 |
| click-argument-help-record | 4 | 2 |
| click-package-name | 5 | 2 |
| click-prompt-strip-ansi | 3 | 0 |
| click-sentinel-copy | 4 | 1 |
| click-short-help | 4 | 1 |
| click-style-colors | 5 | 1 |
| humanize-fractional | 4 | 1 |
| humanize-intword-carry | 4 | 1 |
| humanize-metric-carry | 5 | 1 |
| humanize-natural-list | 4 | 0 |
| humanize-nonfinite-delta | 3 | 2 |
| humanize-tz-naturalday | 4 | 1 |
| packaging-content-type | 4 | 2 |
| packaging-direct-url-file | 4 | 2 |
| packaging-email-errors | 5 | 1 |
| packaging-marker-parens | 4 | 1 |
| packaging-metadata-fold | 4 | 1 |
| packaging-ranges-unbounded | 4 | 1 |

`click-package-name` also resolves two real import names in the benchmark environment (`dateutil` to
`python-dateutil`, `_pytest` to `pytest`); if that environment changes, the run-start check stops the run before any
request. `boltons-us-tz-fold` reads the system's tz database through `zoneinfo` and leaves out 1974-1975, whose
energy-crisis rules zoneinfo has and boltons' fixed US rules don't.

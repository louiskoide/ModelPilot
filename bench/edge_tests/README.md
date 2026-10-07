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
5. Since October 4, 2026 (user decision) edge tests count: a trial passes only if it passes the hidden grader
   and every test of its task's edge suite. `bench` runs the suite on the graded tree after the hidden grader
   (`grade.edge_passed`, `pass_rule`), its run-start check stops a run whose suite the reference doesn't pass in
   full, and the manifest records each suite's SHA-256. `bench_report` takes the edge results of runs graded
   before then from their latest re-grade (`edge_missing` when there is none) and reports hidden-test passes
   alongside. Final tasks have no suites (rule 3), so they are still graded on their hidden tests alone.

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

Known label caveat (user decision, October 6, 2026: noted, suite unchanged):

- `tomli-decode-error-attrs`, `test_line_starts_and_keywords`: it requires keyword construction,
  `TOMLDecodeError(msg='m', doc='ab', pos=0)`, to take the new path without a `DeprecationWarning`, as the reference
  fix does. The instruction names the parameters (`TOMLDecodeError(msg, doc, pos)`) but lists the deprecated calls
  only as "no arguments, fewer or more than three, or arguments that are not (str, str, int)", so it neither asks
  for keywords nor rules them out. A fix taking `*args` only reads the instruction one defensible way and fails this
  test; every Sonnet 5.5 miss on the task and both Opus 5.5 misses are this test alone. Strict results on the task
  stand as recorded; read them as partly the instruction's ambiguity, not only the model's
  (`runs/miss-predictability-20261006.json`).

# Golden set

41 Go cases, one per line in `manifest.jsonl` (schema: `src/pr_review/models.py`).
Each case is a fix commit reverted into a synthetic PR that reintroduces the bug.

| Path | What |
|---|---|
| `manifest.jsonl` | one `Case` per line: provenance, base ref, bug class, ground truth |
| `<id>/buggy.diff` | the revert of the fix, non-test `.go` files only |
| `<id>/base/` | every file the diff touches, at the fix commit (the PR's base), minus comments the fix added |
| `licences/` | upstream licence text for each OSS repo, copied for attribution |
| `build.sh` | rebuilds everything from the commit list in it (needs network) |

## How a case is made

`uv run python -m pr_review.make_case` (see its docstring):

- **Base** is the fix commit; the diff goes from it back to its parent.
- **Only non-test Go files** are in the diff. A reverted regression test or a
  CHANGELOG line would name the bug.
- **Comments the fix added are stripped** from `base/` before diffing, both
  whole comment lines and trailing `//` comments on added lines. Otherwise
  the revert would delete a comment like "avoid uint16 overflow" that states
  the label. So `base/` is the fix commit minus those comments, and a test
  checks that no deleted line in any diff carries a comment.
- **Ground truth** is the buggy-side lines of the hunks listed in `build.sh`
  (all hunks when it says `-`). A hunk that only deletes the fix's added lines
  (a removed nil check, say) is anchored to the lines around the gap. Hunks
  within 6 lines of each other in one file merge into one bug.
- **Noise** is every other hunk: unrelated edits the fix commit carried, such
  as formatting, an import reorder or a renamed message. They stay in the diff.

`tests/test_golden_set.py` checks offline that every diff applies to its
`base/` and every ground-truth range is inside the buggy file.

## Composition

| Class | Cases |
|---|---|
| ignored_err | 6 (2 own) |
| data_race | 5 (1 own) |
| goroutine_leak | 5 |
| nil_deref | 5 |
| overflow_off_by_one | 5 |
| unclosed_resource | 5 |
| slice_aliasing | 4 |
| context_propagation | 4 |
| defer_in_loop | 2 |

**Own repos: 3 cases, all from go-taskqueue.** The spec asks for 8. The other
own repos have few commits that fix a bug in one of the nine classes:

- **dungeon** (private): its "bug fixes" commits change game logic (restart
  handling, win tracking), not one of the classes.
- **gbemu** (private): fixes are emulator-accuracy changes (DMA blocking,
  interrupt masks, stale CPU state between test cases), not one of the classes.
- **dagr** (private): config and CI only so far.
- **duckserve** (public): feature commits only.
- **go-taskqueue** (public): `aac3b48` and `e75bae4` drop ignored errors;
  `f8d74eb` moves a global slice that HTTP handlers appended to without a lock
  behind a mutex (it is a refactor that fixes a race, so it is the weakest of
  the three).

**OSS: 38 cases** from 14 repos, MIT, Apache-2.0 or BSD-2-Clause (licence as
of the fix commit, copied into `licences/`): IBM/sarama, dgraph-io/badger,
etcd-io/bbolt, gin-gonic/gin, go-chi/chi, golang/groupcache, jackc/pgx,
labstack/echo, nats-io/nats.go, prometheus/client_golang, redis/go-redis,
segmentio/kafka-go, urfave/cli, valyala/fasthttp. Commits were found by
searching messages for each class, then kept only if the non-test Go change
was small (about 30 lines or less, 1-2 files) and the bug fits the class.

`defer_in_loop` has only 2 cases: real fixes for it are rare in these repos.

# REVIEW.md

Instructions for automated reviewers on this repo: the `reviewer` subagent (pre-PR review) and any other review bot. Read AGENTS.md first. The reviewer reads this file from `main`, so a branch can't rewrite its own rules.

## What failure looks like here

In order of severity:

1. **The gate is weakened.** A test deleted, skipped or loosened; an expected value edited to match output; a step removed from `verify.sh`; a CI check made non-blocking. Every later green run is false after this.
2. **A regression.** Behaviour that worked on `main` and breaks on the branch.
3. **Out of scope.** Changes the spec's Out of scope list excludes, or work no spec item asked for.
4. **Correctness.** Eval leakage: the agent can see the ground-truth bug, the fix commit or the label for a case it is scored on. Scoring that rewards volume: recall counted without precision, a finding matched to a known bug by file alone instead of file and line range, duplicate findings counted twice. Comparisons that aren't like for like: different case sets, models or repetition counts between arms. Unbounded runs: a headless or eval run without a turn cap, budget cap or timeout.
5. **Untested behaviour.** New behaviour no test would catch if it were wrong.

## Security (only when the diff touches these paths)

Apply when the diff touches `.github/`, `scripts/`, `verify.sh`, `.claude/` or dependency manifests.

- **Workflows:** a new `pull_request_target` or `workflow_run` trigger, widened `permissions:`, an action referenced by tag instead of commit SHA, `github.event` fields interpolated into `run:`.
- **Secrets:** a token or key written into a file, a log or a command line.
- **Dependencies:** a new dependency without a reason in the PR, or an unpinned install.

## What to skip

- Generated and vendored files, lockfile churn.
- Formatting the formatter or linter already enforces.

## How to report

- Every finding needs a file and line as evidence, not a guess from a name.
- Tag findings Important / Nit / Pre-existing. Cap nits at 3.
- Start with a count of findings. If nothing needs fixing, say so in one line.

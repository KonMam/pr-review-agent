# AGENTS.md

## Project

A model-agnostic, precision-first PR reviewer for the owner's Go code, with an offline eval set of reverted Go bug-fix commits scored on precision and recall, and a head-to-head against CodeRabbit. Prep POC for the Decodo internal AI platform role. Owner: Konstantinas. Status: case manifest schema, loader and scorer exist (`models.py`, `manifest.py`, `scoring.py`); the golden set is in `cases/` (41 cases, built by `make_case.py` and `cases/build.sh`); agent v0 exists: `arms.toml` loader, single-pass generator, budget guard and `pr-review eval` (`arms.py`, `llm.py`, `generate.py`, `evaluate.py`, `cli.py`), tested offline against a stub server on the real golden set. `pr-review eval` runs reviews concurrently (`--concurrency`, default 8; 1 is sequential); each call reserves its worst-case cost before it starts, so concurrency can't overshoot the budget, and 429/5xx get a bounded retry. First paid baseline done (`or-deepseek-flash`, 41 cases × 3 runs; numbers in the spec's Baseline section). See `docs/specs/` for the plan. Anything marked *(planned)* doesn't exist yet; don't claim it runs until it does, and update this file when it lands.

## Commands

```bash
./verify.sh      # the gate: the only definition of "works" here
uv sync          # install
uv run pytest -q # unit tests only
uv run pr-review eval --arm ollama-gemma --runs 1 --case <id>   # manual local smoke, 1-2 small cases; never in verify.sh; paid arms need the owner's OK
```

## The Gate

Before saying any change is done, run `./verify.sh`. A Stop hook runs it too and blocks the stop on failure. Never weaken it to get green: no deleting or skipping tests, no loosening assertions, no editing expected values to match output, no removing a step from `verify.sh`. If a check looks wrong, stop and say so; changing what counts as passing is the author's call.

## How Work Runs Here

- **One session, one task.** Each session takes one item from a spec in `docs/specs/` (or one clearly bounded request), on its own branch or worktree, and ends in one PR or commit.
- **Plan first for anything over ~30 minutes.** Write or update the spec (`/harness:spec`): acceptance criteria you can check, and an **Out of scope** list. Then do only the next unchecked item.
- **Stay in scope.** Anything outside the spec goes in a "Follow-ups" note in the PR, not into the diff. No extra benchmarks, refactors, dependencies or features nobody asked for.
- **Hard stops for headless runs:** `claude -p` always gets `--max-turns` and `--max-budget-usd`.
- **Before a PR:** `./verify.sh` green, then spawn the `reviewer` subagent on the branch and fix its Important findings. Commit with `/harness:commit`.
- **Taste-driven work** (UI, prose, feel): no handing off without 3-5 written, observable criteria. Without them, work interactively and show small steps.

## Tech Stack

- **Python** 3.13, **uv** for everything (`uv run`, `uv add`). ruff format + ruff check, mypy, pytest.
- **pydantic** for the case manifest, findings and (later) validating model replies.
- **Model calls**: OpenAI-compatible chat API via the `openai` client, through OpenRouter, using named arms in `arms.toml`. Each eval run has a hard budget (`--budget-usd`, default $1). Base files come from `cases/<id>/base/` (next to each case's `buggy.diff`), so an eval needs no clones and no network. No Claude Agent SDK and no LiteLLM; see the spec's Decisions.
- Every new dependency needs a reason in the PR.

## Layout

| Path | What |
|---|---|
| `src/pr_review/` | the agent and the eval harness |
| `tests/` | unit tests (`verify.sh` runs them) |
| `docs/specs/` | specs: acceptance criteria, out of scope, plan |
| `cases/` | golden set: manifest, buggy diffs, base files (see its README) |
| `results/`, `cache/` | eval outputs and cached API responses (gitignored) |

## Known Mistakes

One line each: what went wrong, and the rule. The lesson hook adds here when a mistake can't be caught by `verify.sh` or `REVIEW.md`.

- Spec defaults came from the Decodo research (PHP/JS corpus, Anthropic API) instead of the owner's own setup. Rule: this reviewer is for the owner's Go code and runs on cheap OpenRouter models, since there are no Anthropic API credits, only a seat licence. Check every default against that before proposing it.

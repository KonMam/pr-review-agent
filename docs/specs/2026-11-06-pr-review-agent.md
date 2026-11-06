# PR-review agent with an offline eval and a build-vs-buy comparison

**Goal:** a model-agnostic PR reviewer for Go code that finds bugs, scored on a golden set of 30-50 Go PRs with known bugs and run side by side with CodeRabbit on the same cases. It starts on the cheapest models that work, so there's a build-vs-buy answer backed by numbers rather than a demo.

## Acceptance criteria

- [ ] **Golden set.** `cases/manifest.jsonl` holds 30-50 cases.
  - Each case has provenance (repo, fix commit SHA, licence), the buggy diff, and ground truth as file + line range.
  - Every case is Go. At least 3 come from my own Go repos (see Decisions), and the rest from permissively licensed OSS Go repos. The set has 41 cases: 3 own-repo (all go-taskqueue) and 38 OSS.
  - Each case is tagged with a Go bug class (see Design). No single class makes up more than half the set.
  - A test in `./verify.sh` checks the manifest schema and that every case's diff applies cleanly to its base.
  - **Clean cases.** The manifest also holds 15-20 bug-free cases (`kind: clean`, `bugs: []`, no bug class): ordinary non-fix commits (features, refactors, small API changes) with 10-80 diff lines, from the same OSS repos and my own where usable. The diff is the commit itself and the base is its parent. Each one is checked to have no later commit touching its changed lines within the next 20 commits, and the check is recorded in its provenance. The 30-50 count and the bug-class balance apply to bug cases only.
- [ ] **Scorer.** One scorer, shared by both arms, computes precision, recall and F1 using the matching rule below. It's unit-tested on hand-built fixtures in `./verify.sh`, with no network calls.
- [ ] **Model swap is config only.** Each model arm is a named entry in `arms.toml`: base URL, key env var, model id, and price, if the provider doesn't report cost. Adding or swapping a model, or moving a stage to another provider, changes `arms.toml` and no Python. A test runs the pipeline against a stub OpenAI-compatible server under two different arm configs to prove this.
- [ ] **Agent eval.**
  - `uv run pr-review eval --arm <name> --runs 3` runs every case 3 times and writes `results/<run-id>/summary.json`.
  - The summary has precision, recall and F1 (mean and min-max over runs), cost per review in USD, and p50/p95 latency.
  - It also has an ablation table: single-pass, then adding grade, dedup and suppress one at a time.
  - Every run has a hard budget enforced in code (default $1), and the provider key has its own spend limit as a backstop.
- [ ] **No style comments.** Across all cases and runs, the agent posts zero findings in a suppressed category (style, naming, formatting, docs, readability). A test on the stored results checks this.
- [ ] **Findings log.** Every CI review writes a JSONL log of each posted finding and uploads it as a workflow artifact (item 8).
  - Each record has: repo, PR number, head SHA, file, line range, category, claim, arm/model id, prompt version, stage versions, and grade score if graded.
  - A test checks the record schema against a stub review, with no network calls.
- [ ] **Feedback loop.** After merge, each logged finding gets its signals, and outcomes are promoted into the golden set (item 9).
  - Promoted cases carry `source: production` in their provenance, and the manifest test accepts them.
  - A prompt or model change is only used by CI if the eval matches or beats the current golden-set scores.
  - A monthly report gives the addressed rate and the 👎 rate.
- [ ] **Build vs buy.** CodeRabbit's comments on the same cases are scored by the same scorer. `docs/build-vs-buy.md` puts both arms in one table and ends with a recommendation that names its main caveat. The table covers precision, recall, F1, cost per review, latency and setup effort.

## Out of scope

- IDE or editor integration. The agent runs from the CLI and from CI only.
- Auto-fixing code or suggesting patches. Findings are text plus a location.
- Style, lint, naming or formatting comments. gofmt, go vet and golangci-lint already do this, so these categories are suppressed, not tuned. The reviewer also doesn't repeat what `go vet` catches.
- Languages other than Go. The harness itself stays in Python, but the reviewer's prompt, bug classes and corpus are Go-only.
- Multi-repo deployment, org-wide rollout, GitHub App installation.
- A UI or dashboard. Results are JSON plus one markdown report.
- Org-wide production signals. 👍/👎 and "addressed rate" are now item 9, scoped to one repo. Uber shows these are the right long-term metric, but rolling them out across an org, or any dashboard for them, stays out of scope.
- A self-hosted gateway (LiteLLM) or any change to `llm-routing-bench`. See `~/projects/decisions/2026-11-06-personal-llm-gateway.md`.
- The Claude Agent SDK as the core loop. It may come back later as one arm once there are API credits. See Decisions.
- Assistants beyond bugs: no AppSec or "best practices" reviewer. uReview has three; this POC builds one.
- More than one bought tool, fine-tuning, or tracing infrastructure such as Langfuse.
- Any write to my older repos. Cases are built only by reading their git history.

## Design

The pipeline follows Uber uReview's structure at small scale. Every stage calls a model through the OpenAI-compatible chat API, using the arm named in config.

The prompt is written for Go. It points the model at bug classes linters miss:
- nil dereferences and nil maps
- ignored or shadowed `err`
- goroutine leaks and missing cancellation
- data races and mutex misuse
- `defer` inside loops
- slice aliasing after `append`
- wrong `context` propagation
- integer overflow and off-by-one
- unclosed resources

These same classes are the tags on golden-set cases.

1. **Generate.** The model gets the diff plus the full text of each changed file at the base commit. It returns structured candidate findings: file, line range, category, claim and evidence. The reply is validated with pydantic, and a malformed reply gets one retry. v0 has no tool loop. Letting the model call read-only tools (read file, grep) is an ablation in a later item, kept only if it pays for itself.
2. **Confidence-grade.** A model scores each candidate from 0 to 1 on "is this a real defect introduced by this diff", and candidates below a threshold are dropped. It can be the same cheap model or a different arm. The threshold comes from a sweep on the golden set that favours precision.
3. **Semantic dedup.** Findings that make the same claim about overlapping locations collapse into one.
4. **Category suppression.** Findings in a suppressed category are dropped, however they were graded.

**Matching rule.** A finding matches a ground-truth bug when it names the same file and its line range overlaps the bug's range widened by ±3 lines.
- Precision = findings matching some bug / all findings after dedup.
- Recall = bugs matched by at least one finding / all bugs.
- **Clean cases** have no bugs, so every finding on one is a false positive: it counts in precision's denominator and adds nothing to recall. The summary also reports a false-positive rate on clean cases: findings per clean review, and the share of clean reviews with any finding. Numbers are reported for bug cases only, clean cases only and the combined set.
- Location-only matching overcounts a little: a finding on the right lines can still give the wrong reason. To bound this, I hand-check 20 sampled true positives per arm and report the share that were wrong.

**Runs and cost.**
- Each case runs 3 times per configuration, because model output varies.
- Results are cached by (case, arm config, stage, run index), so a rerun only pays for what changed.
- Cost comes from the provider's reported cost when there is one (OpenRouter returns it). Otherwise it is tokens × the price in `arms.toml`.

## Plan

One item per session and PR.

- [x] 1. **Case format and scorer:** the manifest schema, loader, matching rule, and P/R/F1 with unit tests on fixtures. No model calls. Update AGENTS.md, which still says "Claude Agent SDK" and "LiteLLM gateway", to match the Decisions below.
- [x] 2. **Golden set:** a script that turns a fix commit into a case (revert the fix → buggy diff, fix hunks → ground truth). Curate 30-50 Go cases from my Go repos and from permissively licensed OSS Go repos, tagged by bug class. Add the manifest validation test.
- [x] 3. **Agent v0:** the `arms.toml` loader, a single-pass generator, the budget guard, and `eval` with repeated runs, caching, cost and latency. Run it with the cheapest arm to get the first baseline.
- [x] 4. **Multi-stage pipeline:** grade, dedup and suppress, each toggleable, plus the threshold sweep and the ablation table. Results are in the Pipeline section below.
- [x] 4a. **Clean cases:** 20 bug-free commits added to the golden set so false positives are measurable, the scorer's false-positive rate, and item 4's ablation and sweep rerun on the combined set. Results are in the Clean cases section below.
- [x] 5. **Model ladder:** rerun the best pipeline on 2-3 arms from cheapest to stronger, to see whether a stronger model buys enough precision or recall to justify its cost. Also try read-only tools as an ablation. Results are in the Model ladder section below; the strong tier didn't fit the budget and has an estimate instead.
- [x] 6. **CodeRabbit arm:** a public sandbox GitHub repo with one PR per case. Collect CodeRabbit's comments, normalise them to findings, and score them. **This needs my OK before any remote is created or anything is pushed.** Results are in the CodeRabbit section below.
- [ ] 7. **`docs/build-vs-buy.md`:** the comparison table, the hand-check results, and the recommendation.
- [ ] 8. **CI on one Go repo:** a GitHub Action, defined in this repo, that reviews a Go repo's PRs with inline comments. This is the "would I actually use it" check. It runs on one of my Go repos, and wiring it in there **needs my OK first**, since it touches an existing project. One repo only.
  - Every CI review also writes a JSONL log of each posted finding and uploads it as a workflow artifact. Each record has repo, PR number, head SHA, file, line range, category, claim, arm/model id, prompt and stage versions, and grade score if graded. This is the input to item 9, so it ships from the first CI run.
- [ ] 9. **Feedback loop:** learn from real use on that one repo. Needs item 8's log. Traffic on my repos is low (a handful of PRs a month), so the realistic loop is a **weekly hand-read** of the logged findings plus promotions; the automated signals are a bonus, not something to wait on.
  - **Signals, collected after a PR merges, per logged finding:** 👍/👎 reactions on the inline comment, and "addressed": the flagged lines changed in a later commit of the same PR before merge. uReview found addressed to be the most useful signal.
  - **Promotion into the golden set,** using the existing builders, with `source: production` in the case's provenance:
    - an unaddressed or 👎 finding becomes a false-positive case: that diff, no bug at that location;
    - a bug found later by a fix commit that the reviewer missed becomes a bug case through the fix-commit builder (`make_case.py`).
  - **Regression gate:** any prompt or model change must match or beat the current golden-set scores before CI uses it. The eval runs this check.
  - **Report:** a monthly addressed rate and 👎 rate, as a markdown file. No dashboard.

## Baseline

First paid run, 2026-11-06, run id `baseline-or-deepseek-flash` (arm `or-deepseek-flash`, model `deepseek/deepseek-v4.1-flash`, stage `generate-1f250a55eceb`, single-pass only). All 41 cases × 3 runs = 123 reviews, all completed within the $1 budget. Requests carried `provider.data_collection = "deny"`. Cost is OpenRouter's reported `usage.cost`.

| | mean | min-max over runs |
|---|---|---|
| Precision | 0.991 | 0.972-1.000 |
| Recall | 0.649 | 0.586-0.690 |
| F1 | 0.784 | 0.731-0.816 |

- **Cost:** $0.0045 per review, $0.548 total (one review came from the smoke run's cache).
- **Latency:** p50 11.9 s, p95 50.6 s.
- **Own-repo slice (3 cases, 6 bugs):** P 1.00, R 0.44 (0.33-0.50), F1 0.61 (0.50-0.67). **OSS (38 cases, 52 bugs):** P 0.99 (0.97-1.00), R 0.67 (0.60-0.73), F1 0.80 (0.74-0.84). These slices were computed from `reviews.jsonl` with the same scorer; `summary.json` doesn't break them out yet.
- **Errors:** no case errored. 17 of 123 reviews (14%) came back malformed after the retry and count as zero findings. 16 of them used the full 4096-token `max_tokens` on both attempts, which means the reply was cut off. The affected cases are sarama-9f8650a (3/3), sarama-05cb9fa, badger-7ceb365, badger-89689ef and echo-05df10c (2/3 each), and go-taskqueue-aac3b48, go-taskqueue-f8d74eb, kafka-go-ed22f4e, go-redis-9b33d16, echo-02de901 and prometheus-08baa43 (1/3 each). This caps recall, so it should be looked at before item 4's numbers are compared with this baseline.

## Pipeline (item 4)

Run 2026-11-06 on arm `or-deepseek-flash`, 41 cases × 3 runs, run ids `p4-single-pass` and `p4-pipeline-final`. Generate stage is `generate-43defae9ada5` with arm config hash `607b939d893a4b93`, and grade stage is `grade-39d6a9243a11`. Grade uses the same arm as generate. Spend for the item was about $0.93 of the $3 allowed: $0.52 for generate, $0.38 for grade and about $0.03 for probes. Every row below comes from the same stored replies.

**Truncation fixed first.** Hidden reasoning was eating `max_tokens`, not the reply. A good baseline reply used about 1.9k completion tokens to produce about 0.15k tokens of JSON. On some cases the reasoning never converges: sarama-9f8650a spent all of 16,384 tokens on reasoning and produced no answer. OpenRouter ignored both `reasoning.max_tokens` and `effort = "low"`, and each still ran to 8,192 tokens of reasoning. The fix has two parts:
- `max_tokens` goes up to 8192.
- A new per-arm `truncation_extra_body = { reasoning = { enabled = false } }` resends a cut-off reply once with reasoning off (`ask.py`).

The result: 26 of 369 generate attempts were cut off, every one was recovered, and 0 of 123 reviews were malformed (the baseline had 17). Recall rose from 0.649 to 0.787 with nothing else changed. The new single-pass row replaces the baseline as the reference.

**Ablation** (P/R/F1 are mean and min-max over 3 runs; latency covers all calls in a review):

| Row | Precision | Recall | F1 | Findings/review | Cost/review | p50 / p95 |
|---|---|---|---|---|---|---|
| single-pass | 0.993 (0.979-1.000) | 0.787 (0.759-0.828) | 0.878 (0.863-0.906) | 1.12 | $0.0042 | 16.2 s / 61.1 s |
| + grade (0.3) | 1.000 (1.000-1.000) | 0.741 (0.707-0.776) | 0.851 (0.828-0.874) | 1.05 | $0.0073 | 30.9 s / 83.5 s |
| + grade + dedup | 1.000 (1.000-1.000) | 0.741 (0.707-0.776) | 0.851 (0.828-0.874) | 1.05 | $0.0073 | 30.9 s / 83.5 s |
| + grade + dedup + suppress | 1.000 (1.000-1.000) | 0.741 (0.707-0.776) | 0.851 (0.828-0.874) | 1.05 | $0.0073 | 30.9 s / 83.5 s |

**Grade threshold sweep** (means; dedup and suppress also applied):

| Threshold | P | R | F1 | Findings/review |
|---|---|---|---|---|
| 0.0 (no drop) | 0.993 | 0.787 | 0.878 | 1.12 |
| 0.1-0.3 | 1.000 | 0.741 | 0.851 | 1.05 |
| 0.4 | 1.000 | 0.695 | 0.820 | 0.98 |
| 0.5-0.7 | 1.000 | 0.690 | 0.816 | 0.98 |
| 0.8 | 1.000 | 0.529 | 0.691 | 0.74 |
| 0.9 | 1.000 | 0.471 | 0.640 | 0.66 |
| 1.0 | 1.000 | 0.437 | 0.606 | 0.61 |

The default threshold is **0.3**, the highest recall at the sweep's best precision. That choice rests on a single false positive: it was tuned on the same 41 cases it's scored on, and the location-level false positive it removes is the only one across all 3 runs (echo-02de901, graded 0.0).

**Reading it:**
- **Grade isn't worth it on this set, as currently measured.** It removes 1 location-level false positive and buys +0.007 precision. To do that it drops 17 of 137 matched candidates at 0.5, or 7% of recall at 0.3, and it adds $0.003 per review and doubles p50.
  - On a quick read, the dropped matches look like correct claims: go-redis-9b33d16's `append` aliasing, and kafka-go-b0b1cdc's context dropped for `context.Background()`. The grader mostly scores "depends on unseen code" as 0.3 instead of the rubric's 0.7.
  - The location-only scorer (next point) can't credit grade with removing right-place, wrong-reason findings, so the hand-check in item 7 has to settle this.
  - Recommendation for item 5: rerun single-pass + dedup + suppress (both free), with grade as an ablation and not the default.
- **Dedup and suppress change nothing here.** The model gives about one finding per review (single-pass: 10 reviews with 0, 97 with 1, 7 with 2, 9 with 3), and overlapping same-category duplicates don't occur.
  - The generator's category list has no style categories, so `suppressed_category_findings` is 0 even without the suppress stage.
  - Both stages stay as cheap guards. The spec's no-style test runs on stored stub results in `tests/test_pipeline.py`, and every summary reports `suppressed_category_findings`. It is 0 for both real runs.
- **Slices (full pipeline):** own-repo (3 cases) P 1.000, R 0.778 (0.500-1.000). OSS (38 cases) P 1.000, R 0.737 (0.712-0.769).
- **Findings per case** for every row are in `summary.json` (`findings_per_case`). Cases averaging more than 1 finding in single-pass: go-redis-9b33d16 (3), go-taskqueue-aac3b48, go-taskqueue-f8d74eb, kafka-go-ed22f4e and echo-05df10c (2 each), sarama-9f8650a (1.67), fasthttp-3aa09fe and echo-02de901 (1.33). With the full pipeline, badger-89689ef and bbolt-53a930f get no finding in any run.

**Precision is inflated (flag for item 7's hand-check; the scorer is unchanged).** The diffs are 14-74 lines including context, but the median diff changes only **5** lines (1-34). With ±3-line matching, a finding placed on a random changed line would match a bug **89%** of the time (mean over cases; the median case is 100%, and 38 of 41 cases are at 50% or more). So P ≈ 0.99-1.00 mostly measures "points at the changed lines", not "names the right defect". This is also why grade looks bad here: right-place, wrong-reason findings count as true positives. Item 7's hand-check of sampled true positives is the real precision number for both arms, and it should sample findings that grade dropped as well as ones it kept.

**Other notes:**
- One generate call in `p4-single-pass` cost $0.01116 against a $0.01092 reservation: OpenRouter routed it above twice the list price. The overshoot was small and the run stayed far under its cap, but `REPORTED_COST_MARGIN = 2.0` isn't a strict bound for this arm.
- The grader uses the same reasoning arm: 127 calls for 113 graded reviews, with 14 cut-offs recovered by the same fallback and 0 malformed.

## Clean cases (item 4a)

Run 2026-11-06 on arm `or-deepseek-flash`, 61 cases (41 bug + 20 clean) × 3 runs, `--stages grade,dedup,suppress`, `--concurrency 8`. Run ids are `clean-pipeline` (paid) and `clean-pipeline-final` (the same replies re-scored from cache, $0). The stages, prompt and arm hash are the same as item 4 (`generate-43defae9ada5`, `grade-39d6a9243a11`, `607b939d893a4b93`), so the 123 bug-case reviews came from item 4's cache. The spend was **$0.283** for 60 generate and 4 grade calls on the new cases. There were 0 malformed reviews.

**Ablation.** Combined P/R/F1 are means with min-max over 3 runs. Recall comes from bug cases only. "FP/clean review" is findings per clean review, and "flagged" is the share of clean reviews with any finding.

| Row | Combined P | Recall | Combined F1 | Bug-only P | FP/clean review | Clean flagged | Findings/review (all) | Cost/review | p50 / p95 |
|---|---|---|---|---|---|---|---|---|---|
| single-pass | 0.958 (0.915-1.000) | 0.787 (0.759-0.828) | 0.864 (0.829-0.906) | 0.993 | 0.083 (0.000-0.200) | 0.067 (0.000-0.150) | 0.78 | $0.0044 | 16.2 s / 67.2 s |
| + grade (0.3) | 0.984 (0.976-1.000) | 0.741 (0.707-0.776) | 0.846 (0.820-0.874) | 1.000 | 0.033 (0.000-0.050) | 0.033 (0.000-0.050) | 0.72 | $0.0065 | 29.5 s / 82.9 s |
| + grade + dedup | same as + grade | | | | | | | | |
| + grade + dedup + suppress | same as + grade | | | | | | | | |

**Grade threshold sweep** (means, with dedup and suppress also applied; F0.5 is on the combined set):

| Threshold | Combined P | R | F1 | F0.5 | Bug-only P | FP/clean review | Clean flagged |
|---|---|---|---|---|---|---|---|
| 0.0 (no drop) | 0.958 | 0.787 | 0.864 | 0.918 | 0.993 | 0.083 | 0.067 |
| 0.1-0.3 | 0.984 | 0.741 | 0.846 | 0.924 | 1.000 | 0.033 | 0.033 |
| 0.4-0.7 | 0.992 | 0.690-0.695 | 0.814-0.817 | 0.912-0.914 | 1.000 | 0.017 | 0.017 |
| 0.8 | 0.989 | 0.529 | 0.688 | 0.841 | 1.000 | 0.017 | 0.017 |
| 0.9 | 0.987 | 0.471 | 0.637 | 0.809 | 1.000 | 0.017 | 0.017 |
| 1.0 | 0.986 | 0.437 | 0.604 | 0.786 | 1.000 | 0.017 | 0.017 |

The best F0.5 is at 0.3 (0.1-0.3 tie, and the highest wins), so the default stays at 0.3.

**Reading it:**
- **The reviewer is quiet on clean code.** Single-pass gave 5 findings in 60 clean reviews: 4 reviews flagged, all in 3 of the 20 cases.
  - echo-clean-09ccfba: a possible nil `c.request`, graded 0.0.
  - go-redis-clean-e6ea2d2: an error that isn't set on the returned command, graded 0.0.
  - cli-clean-c344b46: three findings over two runs on the `Exit` rewrite, graded 0.0, 0.3 and 1.0. The 1.0 one says `Exit(nil, 1)` with a typed-nil error dereferences nil. It might be a real latent bug that the clean check can't see, so item 7 should hand-check it.
  - Bug-only precision barely moves (0.993 and 1.000), so the combined precision drop, 0.993 to 0.958, comes entirely from these clean-case findings.
- **Does grading pay for itself now? Only on the precision-first measure, and only just.**
  - It removes 3 of the 5 clean false positives, plus the one location-level false positive on a bug case. FP per clean review falls from 0.083 to 0.033.
  - It still costs 0.046 recall (the same 7% of matched bugs as in item 4). F1 falls from 0.864 to 0.846, while F0.5 rises from 0.918 to 0.924.
  - It adds $0.0021 per review and about doubles p50. On clean cases it costs almost nothing, since grade only runs on reviews with candidates (4 of 60).
  - The gain rests on 4 false positives in total. It is within run-to-run noise: single-pass flagged 0 to 3 clean reviews per run.
  - So item 4's recommendation stands, with less margin. Item 5 should run single-pass + dedup + suppress as the default, with grade as an ablation. Item 7's hand-check, of the dropped matches and the cli-clean finding, decides between them.
- **20 clean cases are too few to rank stages on false positives.** Each run has 20 clean reviews, so one false positive moves the flagged share by 0.05. If item 5 or 6 needs a sharper false-positive number, the clean set should grow (the builder makes this cheap) before anything else is tuned on it.
- **Truncation is higher on clean code.** 60 clean reviews had 23 generate attempts cut off at `max_tokens`, against 26 in 123 bug reviews (369 attempts). The model seems to keep reasoning when it finds nothing. Every one was recovered by the reasoning-off retry, so the reviews are complete, but the clean cases' p95 latency and cost carry that retry. Clean reviews averaged $0.0046 for generate.

## Model ladder (item 5)

Run 2026-11-06. The pipeline under test is single-pass + dedup + suppress, item 4a's recommendation, with grade as a separate ablation row. The generate prompt is unchanged (`generate-43defae9ada5`). Every arm sends `provider.data_collection = "deny"` and the attribution headers. `--concurrency 8`.

**Budget.** The owner's OpenRouter workspace had $3.00 for this item, then $2 more; that is the ceiling. Item spend was **about $3.04**:
- $1.28 for the tools ablation.
- $1.52 for V4 Pro.
- $0.14 for probes.
- About $0.10 on calls that were never cached: two request timeouts, a 403 at the workspace cap, and a GLM request rejected with HTTP 400.

That money didn't cover a strong tier or a grade row for V4 Pro.

**One case left out.** `nats-clean-b5bc7b4` (56k-token prompt) has no V4 Pro run 3. Its budget-guard reservation was $0.41, more than was left. **Every row below is on the same 60 cases** (41 bug + 19 clean) × 3 runs, re-scored from cache. Run ids are `l5-flash-60`, `l5-flash-tools-60` and `l5-pro-60`. The full 61-case runs are `l5-flash-ds` and `l5-flash-tools`, and their numbers differ only in the third decimal.

**Arms** (OpenRouter list prices per million tokens, from `/api/v1/models` on 2026-11-06):

| Arm | Model | Price in / out | Why |
|---|---|---|---|
| `or-deepseek-flash` | `deepseek/deepseek-v4.1-flash` | $0.051 / $0.567 | The cheap baseline, already cached. |
| `or-deepseek-pro` | `deepseek/deepseek-v4-pro`, **reasoning off**, fp4 hosts excluded | $0.955 / $1.911 | The same family one size up (1.6T MoE, 49B active), so the difference is scale, not prompt fit. With reasoning on, both probe reviews spent all 8,192 tokens reasoning, were cut off, and were answered by the reasoning-off retry, at $0.027-0.038 each (~$5.5 for 183 reviews). With reasoning off it is $0.008 a review. |
| `or-glm` | `z-ai/glm-5.3`, fp8 or better, `max_price` at the cheapest fp8 host | $0.28 / $0.88 | A different-family mid tier that looked as if it fit. **Not run.** Reasoning can't be disabled on this endpoint (HTTP 400), and it reasoned to the cap anyway: 16,384 tokens over two attempts and a malformed reply on one of two probe cases. It also returned 429s. Measured cost was ~$0.013 a review (~$2.4 for 183). Without `max_price`, deny + fp8 routed a probe to a ~$1.4/M-input host ($0.052 for one review). |
| `or-sonnet` / `or-gpt-sol` | `anthropic/claude-sonnet-5.5` / `openai/gpt-6.1-sol` | $2 / $10 | The strong tier. **Not run: it didn't fit.** At V4 Pro's measured 8.4k prompt tokens per review, without reasoning, it's ~$0.020 a review (~$3.6 for 183). With reasoning on, it's ~$0.06 (~$11). Both arms are in `arms.toml`, ready to run. |

**Read-only tools.** On the cheap arm only, `pr-review eval --tools` lets the generator call two tools for up to 4 turns, then asks for the answer with `tool_choice = "none"`:
- `read_file` (at most 300 lines)
- `grep` (at most 40 matches)

The tools read an offline snapshot of the repo at base. `pr-review snapshot` builds it from the local clones `cases/build.sh` makes: non-test `.go` files outside `vendor/` and `testdata/`, with the case's own `base/` files laid over them so the fix's comments stay out. It comes to 42 MB in `cache/snapshots/`, and no network is needed. Tests are left out because a fix commit's regression test would name the bug. Stub tests cover the loop, the turn cap, the retries and the snapshot builder.

**Ladder** (60 cases × 3 runs; P/R/F1 and FP are means with min-max over runs; recall is on bug cases):

| Row | Combined P | Bug-only P | Recall | F1 | FP/clean review | Clean flagged | Cost/review | p50 / p95 |
|---|---|---|---|---|---|---|---|---|
| flash: single-pass + dedup + suppress | 0.958 (0.915-1.000) | 0.993 | 0.787 (0.759-0.828) | 0.864 (0.829-0.906) | 0.088 (0.000-0.211) | 0.070 | $0.0043 | 15.2 s / 61.0 s |
| flash: + grade (0.3) | 0.984 (0.976-1.000) | 1.000 | 0.741 (0.707-0.776) | 0.846 (0.820-0.874) | 0.035 (0.000-0.053) | 0.035 | $0.0065 | 28.2 s / 78.9 s |
| flash: + read-only tools | 0.978 (0.959-1.000) | 0.993 | 0.787 (0.724-0.828) | 0.872 (0.831-0.906) | 0.035 (0.000-0.053) | 0.035 | $0.0067 | 19.2 s / 80.7 s |
| V4 Pro, reasoning off | 0.931 (0.842-1.000) | 0.946 | 0.316 (0.259-0.397) | 0.471 (0.396-0.568) | 0.018 (0.000-0.053) | 0.018 | $0.0080 | 2.2 s / 5.8 s |
| GLM-5.3 | not run: ~$0.013/review measured, reasoning can't be disabled | | | | | | | |
| Sonnet 5.5 / GPT-6.1 Sol | not run: ~$0.020/review without reasoning, ~$0.06 with | | | | | | | |

**Reading it:**
- **A bigger model without reasoning is much worse, not better.** V4 Pro with reasoning off gives 0.51 findings per bug review against flash's 1.05, and recall falls from 0.787 to 0.316. It costs about twice as much per review, though it is 7× faster at p50.
  - Its few false positives (1 in 57 clean reviews) come from saying little.
  - Its bug-only precision, 0.946, is the lowest on the ladder: 3 of its 63 bug-case findings missed the bug's lines, against 1 of 138 for flash.
  - So, on this set, reasoning matters more than scale. The cheap reasoning model beats the expensive one with reasoning turned off.
- **Whether a stronger reasoning model pays is still open.** V4 Pro with reasoning on and both $2/$10 models didn't fit the money: about $5.5, $3.6 and $11 respectively for 183 reviews. A useful first test of a stronger model is V4 Pro reasoning-on, or Sonnet 5.5, on the cases where flash misses a bug in every run (at most 6 of the 58 bugs). That is a few dollars, not ten.
- **Tools: same recall, fewer clean false positives, +55% cost. Not kept as the default.**
  - Tools were called in 139 of 180 reviews, 539 calls in all. Recall is unchanged (0.787), as is bug-only precision. Clean false positives fall from 5 to 2 (FP per clean review 0.088 to 0.035). That is the same false-positive gain grade gives, without grade's 0.046 recall loss, at about the same cost ($0.0067 vs $0.0065) and less latency (p50 19 s vs 28 s).
  - Two caveats keep it from paying for itself yet. First, the gain is 3 false positives, inside run-to-run noise (0 to 4 per run). Second, 6 tools reviews came back malformed, against 0 for single-pass, and 3 of them are clean reviews. Requests that offer tools go without JSON mode (`summary.tools.json_mode = false`). If each of those 3 clean reviews had held one false positive, tools would tie single-pass. On the 3 clean reviews in question, single-pass found nothing in the same run, so the tie is the worst case, not the likely one.
  - So tools stay as an option (`--tools`), and the default stays single-pass + dedup + suppress. If false positives turn out to matter more than cost, tools are a better precision stage than grade on these numbers, since they don't cost recall. Item 7's hand-check should sample their clean-case findings too.
- **The cheap arm stays the arm for items 6-7.** Nothing measured here beats flash single-pass on F1 at its cost of $0.0043 per review.
- **Routing makes cost per review hard to predict.** `data_collection = "deny"` plus a quantization filter can route to hosts several times the list price. GLM's probe paid ~5× the cheapest host, and V4 Pro reviews ranged from $0.0017 to $0.09. `provider.max_price` pins a ceiling. The budget guard's worst case (2× list, full `max_tokens`) also refuses very large prompts near the end of a budget: nats-clean's 56k-token prompt reserved $0.41.
- **Request timeouts abort a run.** One tools review hit the 180 s client timeout on its retry, which stopped the whole eval; rerunning used the cache. A per-request timeout retry is a possible follow-up.

## CodeRabbit (item 6)

Run 2026-11-06 on a public sandbox, https://github.com/KonMam/pr-review-sandbox (left up for the owner to archive). There is **1 run per case**, against 3 for the agent rows. No OpenRouter calls were made.

**Sandbox** (`python -m pr_review.coderabbit build|push|open-prs|trigger|collect|score`; code in `sandbox.py` and `coderabbit.py`):
- **Branches.** Each of the 61 cases (41 bug + 20 clean, including the 3 go-taskqueue cases, since that repo is public) has two branches:
  - `base/case-NNN` is one parentless commit holding the case's offline snapshot (item 5's non-test `.go` files at base, with the case's `base/` files laid over them, so fix-added comments stay out), the upstream `LICENSE`, and `.coderabbit.yaml`.
  - `case/case-NNN` is that commit plus the case diff, as one commit.
- **PRs.** One PR per case, from `case/` into `base/`, never merged. File paths match the golden set, so no line mapping is needed. `main` holds only a README that lists the 15 source repos and their licences.
- **Neutral by construction.**
  - Case numbers are a seeded shuffle of the case ids.
  - Every commit has the same author, committer and date (2026-01-01).
  - Branch names, commit messages and PR titles come from fixed templates ("Case 017"), and every PR has the same description.
  - The number ↔ case-id mapping is only in `cases/coderabbit-map.json`, which is in this repo and not in the sandbox.
- **Leakage check.** `sandbox.check_repo` runs before every push and found 0 problems. It rejects test files, vendor/testdata, any non-Go file other than LICENSE and the config, history, non-template names, messages or identities, and any case id or upstream SHA in branch names, commit messages or the README (file contents are upstream code and aren't scanned). `check-prs` found 0 problems in the PR titles and descriptions. Both are unit-tested on fixtures in `./verify.sh`.
- **Settings: default review settings.**
  - Profile `chill`, with the built-in tools and linters left on.
  - `.coderabbit.yaml` only turns off extras that aren't findings: poem, sequence diagrams, related issues and PRs, suggested labels and reviewers, the effort estimate, the in-progress fortune, and docstring and unit-test generation.
  - It also sets `auto_review.base_branches: ["base/.*"]`, because the PRs don't target `main`.

**Limits and how they played out.**
- **What the docs say** (read 2026-11-06): public repos are on the OSS plan, with 1-10 PR reviews per hour per developer depending on star count. Repos with under 10 stars need a manual `@coderabbitai review`. Reviews cover 100-300 files.
- **What happened:**
  - Installing the app put the account on a trial of the **Advanced** plan (10 reviews an hour), so every PR was reviewed automatically on open and no manual requests were needed.
  - By about the 11th PR the review bodies said all 10 included reviews were used, yet all 61 reviews still ran.
  - The docs say trial overage is free, and no payment method was added. **The owner should check CodeRabbit's billing page shows no usage charge, and turn off pay-as-you-go before the trial ends.**
- All 61 PRs were opened within 6 minutes, so latency includes CodeRabbit's queueing.

**Normalising.**
- A finding is a top-level inline review comment by `coderabbitai[bot]` at its line range, or a located entry in the review body.
- The walkthrough comment and the summary CodeRabbit appends to the PR description are ignored.
- CodeRabbit no longer uses "Potential issue" / "Refactor suggestion" / "Nitpick" labels. Each comment starts with a badge line instead: severity · category · effort, e.g. `Major · Stability & Availability · Quick win`.
- So the two cuts are **all comments**, and **Major or Critical only**, the closest thing to "potential issues only". No nitpicks appeared under `chill`.
- Raw JSON is under `results/coderabbit/raw/`, normalised findings in `results/coderabbit/findings.json`, and the summary in `results/coderabbit/summary.json`.

**Results** (same scorer; the flash rows are item 5's, on 60 cases × 3 runs, mean; CodeRabbit is on the same 60 cases, 1 run; latency is PR open → review posted for CodeRabbit, and per review for flash):

| Row | Combined P | Bug-only P | Recall | F1 | FP/clean review | Clean flagged | Cost/review | p50 / p95 |
|---|---|---|---|---|---|---|---|---|
| flash: single-pass + dedup + suppress | 0.958 | 0.993 | 0.787 | 0.864 | 0.088 | 0.070 | $0.0043 | 15.2 s / 61.0 s |
| flash: + read-only tools | 0.978 | 0.993 | 0.787 | 0.872 | 0.035 | 0.035 | $0.0067 | 19.2 s / 80.7 s |
| CodeRabbit, all comments | 0.698 | 0.902 | 0.621 | 0.657 | 0.632 | 0.526 | $0 (trial) | 165 s / 306 s |
| CodeRabbit, Major or Critical only | 0.810 | 0.944 | 0.569 | 0.668 | 0.316 | 0.316 | $0 (trial) | same |

The CodeRabbit rows come from `score --exclude nats-clean-b5bc7b4 --out summary-60.json`. On all 61 cases (`summary.json`), the all-comments row is P 0.673, FP/clean 0.700 and clean flagged 0.550, and p95 latency is 358 s. `nats-clean-b5bc7b4` drew 2 Minor comments, and the other figures are unchanged.

**Reading it:**
- **CodeRabbit finds fewer of the bugs and flags far more clean code.**
  - It hit 36 of 58 bugs (all comments), against flash's 0.787 mean.
  - 11 of the 20 clean PRs drew at least one comment: 14 comments in all, 6 of them Major. Flash averaged 0.088 per clean review.
- **On bug PRs its comments are mostly on target.** 37 of 41 hit a bug, so bug-only P is 0.902. Its precision problem is clean PRs, which is the false-positive rate a team would actually see.
- **Its own severity is a useful filter.** Keeping Major/Critical halves the clean false positives (0.632 → 0.316) and costs 3 bugs of recall. That still leaves it below flash on every quality column.
- **Out-of-diff comments matter.** 8 bug cases are pure deletions (the revert only removes the fix), and CodeRabbit says the file has "no reviewable changes". Even so, on 7 of them it left an "Outside diff range" comment in the review body, and all 7 hit the bug. Scoring only inline comments would drop recall to 29/58.
- **Comment mix.** 55 comments: 41 Major, 13 Minor, 1 Critical. By category: 36 Stability & Availability, 11 Functional Correctness, 5 Data Integrity, and 1 each for maintainability, performance and security. One clean-case comment was marked "Unverified finding".
- **Caveats:**
  - 1 run, so there's no run-to-run spread.
  - The trial plan is Advanced, not the free OSS plan a public repo would normally get.
  - Latency includes queueing from opening 61 PRs at once.
  - CodeRabbit's pre-merge "title check" flagged every generic "Case NNN" title. That's a check, not a finding, and wasn't scored.
- Item 7 should hand-check 20 sampled CodeRabbit true positives too, and its clean-case comments.

## Decisions

- **Model access: OpenRouter, behind config-defined arms. The Anthropic API is rejected for now.**
  - There are no Anthropic API credits; only a seat licence, which doesn't cover API use from my own code.
  - Models have to be swappable fast. OpenRouter gives one OpenAI-compatible endpoint across providers, one key for this project with a monthly limit, and a small prepaid balance with auto top-up off, which makes the balance a hard ceiling. It also reports cost per request.
  - Local Ollama (`gemma4:e4b-it-qat`) is a free arm for smoke tests, not for scored runs. It's the fast, reliable one on the owner's M1 Pro (~30 tok/s decode, every JSON-extraction task right); `qwen3.5:9b` stalls for minutes on long prompts. A smoke run is 1-2 small cases, 1 run, a 120s per-request timeout and capped output, run by hand. Local models are never part of `./verify.sh` or the unit tests, and never run on the full golden set.
  - DeepSeek direct, with the existing key, is a fallback arm.
  - This follows the personal gateway decision doc, which also rejects a standing LiteLLM.
- **Start on the cheapest tier.** The first baseline uses a cheap-tier model, such as DeepSeek V4.1 Flash, for both generate and grade.
  - Rough cost: 40 cases × 3 runs × ~15k tokens ≈ 2M tokens, so well under $1 at cheap-tier prices. That makes it fine to iterate on prompts and stages many times.
  - Stronger models only come in through item 5, and only if the numbers say they're worth it.
  - The uReview pattern of a strong generator plus a cheap grader becomes one configuration to test, not the default.
- **No Claude Agent SDK as the core.** It's built around Claude models. Running it on other models needs Anthropic-format translation, which is best-effort, and the bench saw model-specific breakage there. A plain OpenAI-compatible client plus pydantic for structured output is the smallest thing that works across every arm.
  - Both `openai` and `pydantic` are new dependencies. The reason: they're the only two needed to call any arm and validate its replies.
  - Rejected: pydantic-ai (more abstraction than one prompt needs), LiteLLM SDK (supply-chain incidents in 2026, and OpenRouter already normalises providers).
  - Wiring in the Agent SDK as a later arm is possible once there are credits. Moving the agent loop between frameworks isn't something this POC needs to settle.
- **No tool loop in v0.** The diff plus the changed files at the base commit should catch most bugs a reviewer can see. Cross-file bugs are where tools would help, and item 5 measures whether that's worth the extra tokens. *Item 5:* read-only tools left recall unchanged and cut clean false positives (5 to 2) for +55% cost; that is within noise, so they stay an option (`--tools`), not the default.
- **Corpus: Go only, mixing my repos with OSS.**
  - The aim is a tool I'd use myself, and Go is what I write most (gbemu, dagr, go-taskqueue, dungeon, duckserve).
  - One language means the prompt, bug classes and corpus can all be specific, and a small golden set isn't spread across languages.
  - Rejected: adding Python, which would halve the cases per language for a language I use less.
  - My Go repos have only about 15 commits that look like real fixes, and not all of them will be usable, so OSS Go bug-fix commits fill the rest. Candidate sources: stdlib-adjacent libraries and tools I use.
  - Own-repo cases are also reported as their own slice.
  - The owner accepted 3 own-repo cases instead of the original 8, because only 3 usable fix commits were found in their Go repos (item 2; see `cases/README.md`).
- **Ground truth is a human fix commit, not an LLM finding.** Scoring against earlier reviewer-subagent findings would be circular. They count as cases only where a later commit fixed them.
- **Reverted fixes as synthetic buggy PRs.** This gives exact locations and no labelling work.
  - The cost: one bug per case, and these PRs are cleaner than real ones, which usually mix the bug with unrelated changes.
  - To offset this, where a fix commit carries other edits, those edits are kept in the diff as noise.
- **Clean cases make false positives measurable.** With one bug per small diff and about one finding per review, nearly every finding lands within ±3 lines of the bug, so precision on bug-only cases sat at 0.99 and a precision stage (grade) could only lose recall (item 4). Bug-free commits give the reviewer real chances to be wrong. They are taken from history, not written, and a commit counts as clean only if no later commit within 20 changes or inserts lines within ±3 of the lines it added: a conservative proxy for "not followed by a fix", since any later edit there rejects it. A latent bug the check can't see would make a correct finding count as a false positive, so clean-case precision is a lower bound.
- **Learn from real PRs via a findings log and promotions.** Real cases fix the golden set's main weakness: reverted fixes are cleaner than real PRs. The log is cheap to add from day one, and without it item 9 has nothing to read. With low traffic, weekly hand-reading beats waiting for automated signals.
- **Comparison arm: CodeRabbit.** It's the market leader, free on public repos, and makes the vendor benchmark claims (Martian bench) that this setup can test.
  - Rejected: Copilot review (needs a seat), Greptile (paid trial), Claude Code GH Action (same model family as the agent, so it says little about build vs buy).
- **Precision first.** Public benchmarks put the best tools around 50% precision and 50% recall, and raising recall adds noise fast. The threshold is chosen for precision, and recall is reported, not optimised.

## Open questions

- **Are my own repos public?** The CodeRabbit sandbox would republish their code. If they're private, own-repo cases run on the agent arm only.
  - *Answered in item 2 (`gh repo view`, 2026-11-06):* go-taskqueue and duckserve are public; gbemu, dagr and dungeon are private. All 3 own-repo cases are from go-taskqueue, so all of them can go to the CodeRabbit sandbox. The owner then accepted 3 own-repo cases (see Decisions), so this is closed.
- **CodeRabbit's free-tier limits** (rate, PR size) on a sandbox with 30-50 PRs. Check them in item 6 before building the case-to-PR script.
  - *Answered in item 6:* the docs give 1-10 reviews an hour on the OSS plan by star count, manual triggers under 10 stars, and 100-300 files per review. In practice the account got an Advanced trial, which reviewed all 61 PRs on open. See the CodeRabbit section.
- **OpenRouter privacy settings.** Exclude providers that train on or retain prompts before sending any code. That's fine for public OSS and my own code, but not for anyone else's.

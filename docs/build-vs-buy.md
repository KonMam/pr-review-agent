# Build vs buy: a Go PR reviewer on a cheap model, against CodeRabbit

*2026-11-06. Item 7 of [the spec](specs/2026-11-06-pr-review-agent.md).*

## The answer

**Build, for my own Go repos.** On 60 Go PRs with known outcomes, the reviewer we built finds more of the bugs than CodeRabbit (recall 0.79 vs 0.62). It is wrong about a bug less often, and it leaves far fewer comments on clean PRs (0.09 per clean PR vs 0.63). It costs about $0.004 a review, against roughly $0.20-0.25 a review in CodeRabbit's usage pricing, or a $24-90 seat per developer per month. A model judge confirms that its matches name the right defect 97% of the time, so the lead holds at claim level too: F1 0.84 vs 0.66.

**Main caveat:** I built the test set myself, around the bug classes our prompt targets, and tuned the pipeline on the same 60 cases. This is a home-ground result. It shows the cheap build is good enough to run, not that it beats CodeRabbit on someone else's code. Buying is still the zero-effort option: our CI integration (item 8) doesn't exist yet.

## The numbers

Same 60 cases (41 reverted bug fixes with 58 bugs, plus 19 clean commits), same scorer. Ours is DeepSeek V4.1 Flash on OpenRouter, mean of 3 runs. CodeRabbit is 1 run on a public sandbox repo with one PR per case.

| | **Ours: single-pass** | Ours: + read-only tools | **CodeRabbit: all comments** | CodeRabbit: Major/Critical only |
|---|---|---|---|---|
| Precision, location | 0.958 | 0.978 | 0.698 | 0.810 |
| Recall, location | 0.787 | 0.787 | 0.621 | 0.569 |
| F1, location | 0.864 (0.83-0.91) | 0.872 (0.83-0.91) | 0.657 | 0.668 |
| Right-reason rate (judge) | 0.971 (133/137) | 0.985 (133/135) | 1.000 (37/37) | 1.000 (34/34) |
| Precision, claim | 0.930 | 0.963 | 0.698 | 0.810 |
| Recall, claim | 0.764 | 0.776 | 0.621 | 0.569 |
| F1, claim | 0.839 (0.81-0.89) | 0.859 (0.79-0.91) | 0.657 | 0.668 |
| False positives per clean PR | 0.088 | 0.035 | 0.632 | 0.316 |
| Cost per review | $0.0043 | $0.0067 | ~$0.20-0.25 usage price, or a seat (below) | same |
| Latency p50 / p95 | 15 s / 61 s | 19 s / 81 s | 165 s / 306 s | same |
| Setup effort | ~2 h for the reviewer, ~8 h with the eval (below); CI not built | same | minutes: install the GitHub App | same |

How to read it:
- **Location level** is the spec's matching rule: a finding counts if it lands on the bug's file within ±3 lines. **Claim level** keeps only the matches that the judge says describe the same defect as the maintainers' fix. A finding on a clean PR is always a false positive.
- **Ranges** are min-max over the 3 runs. CodeRabbit has one run, so it has no range.
- **CodeRabbit's misses aren't wrong reasons.** Its claim numbers equal its location numbers because the judge confirmed every one of its 37 matches. It loses on recall and on comments on clean PRs.
- **Grade stage.** A grade stage in our pipeline (`--stages grade`) trades recall for precision: claim P 0.970, R 0.730, F1 0.833, 0.035 false positives per clean PR, $0.0065 a review. The judge confirms 127 of its 129 matches. Of the 8 matches it drops across the 3 runs, the judge confirms 6 and rejects 2. So it removes 2 of the 4 wrong-reason matches at the cost of 6 right ones, and it stays off by default.

**Cost detail.**
- **Ours** is OpenRouter's reported cost per review. It covers generate only, with dedup and suppress, which are free.
- **CodeRabbit** ran on an Advanced-plan trial. Its billing page shows **$12.00 waived** as usage-priced overage for the reviews beyond the 10 included. That is about $0.24 per review for the 51 over the limit, or $0.20 averaged over all 61.
- **List pricing** (coderabbit.ai/pricing, read 2026-11-06):
  - Seats per developer per month, monthly / annual billing: Essentials $30 / $24, Team $60 / $48, Advanced $90 / $72, Enterprise by quote.
  - Included reviews per developer per hour: 5, 8, 10 and 12.
  - Reviews past the limit cost **$0.25 per reviewed file**, under a spending cap.
  - Public repositories are free.
- **Scale.** At ~20 PRs a month, ours costs under $0.10 a month in model calls. CodeRabbit costs one seat, about $24-90, unless the code is public.

**Setup effort.**
- **Ours.** The whole repo was built on 2026-11-06, from an empty template at 07:30 to the CodeRabbit scores at 15:31: about 8 hours of wall-clock time, with Claude Code sessions writing most of the code in parallel. The reviewer itself (arms config, generate, grade/dedup/suppress, budget guard) landed between 10:56 and 12:43, and read-only tools by 14:31: about 1,400 lines in all. The rest went on the golden set, the eval harness and the CodeRabbit sandbox. Running it on real PRs still needs the CI action and findings log (item 8), which isn't built.
- **CodeRabbit.** Install the GitHub App on the repo and pick a plan. A `.coderabbit.yaml` is optional; we used one only to turn off extras that aren't findings. It reviewed every PR on open with no further setup. (Building our leakage-checked sandbox took longer, but that cost belongs to the evaluation, not to using CodeRabbit.)

## The claim judge

The owner chose not to hand-label findings, so a model stands in for the hand-check.
- **Model.** `openai/gpt-6-luna` (arm `or-judge-luna`) is from a different family from the DeepSeek reviewer. It sends `data_collection = "deny"`.
- **Input.** For each location-matched finding, the judge sees the buggy diff, the fix (the buggy diff reversed) and the finding's text. For our findings that is the claim plus evidence. For CodeRabbit it is the comment's prose, without its proposed patch.
- **Question.** "Does this finding describe the same defect as the ground-truth fix?" It answers yes or no, with a one-line reason.
- **Scale.** It judged 309 distinct findings with 0 malformed replies, for **$0.044** in total (`pr-review judge`; verdicts are in `results/judge/judge.json`).

What we know about the judge, and what we don't:
- **It has not been checked against human labels.** Every right-reason number above is the judge's opinion.
- **It is self-consistent.** A second verdict on a seeded sample of 40 findings agreed on 39 (0.975). Kappa is 0, which means little here: the first pass said yes to all 40, so the agreement rate can't separate a careful judge from one that always says yes.
- **It does say no.** It rejected 6 of 309 findings. Several rejections look right: an echo finding that called the buggy removal "fine", and a sarama finding that said there was "no runtime bug". At least three look too strict. Three go-taskqueue findings describe the data race that the fix's mutex removes, and the judge called it "a distinct defect". So its errors probably run both ways.
- **It may be lenient.** It sees the fix, and a vague claim near the fix may read as right. If that is so, the claim-level numbers for both arms are an upper bound.

## Caveats

1. **The test set is built around our bug classes.** The 9 Go bug classes in our prompt are the same tags used to pick cases. The prompt, threshold and pipeline were tuned on these 60 cases, while CodeRabbit is general-purpose and saw them cold. This is the main caveat.
2. **Reverted fixes are cleaner than real PRs.** Each bug PR is a single reverted fix with a median of 5 changed lines. Real PRs mix the bug with unrelated work, which gives a reviewer more places to be wrong. That is also why location precision was near 1.0 before clean cases were added.
3. **1 run against 3.** CodeRabbit ran once per case, so it has no run-to-run spread. Our F1 moves by about ±0.04 between runs.
4. **Location-only matching.** The spec's metric credits any finding within ±3 lines of the bug. The judge corrects for this, but the correction is unvalidated (next point).
5. **The judge is unvalidated.** Its verdicts haven't been compared with human labels, and a 97-100% yes rate leaves little room for anything to show. A 20-finding human spot check, or a control that shows it findings from other cases, would settle this cheaply.
6. **CodeRabbit's extras aren't scored.** The walkthrough summary, learnings across PRs, linters, chat and pre-merge checks may be worth paying for on their own. Only located findings were scored.
7. **CodeRabbit's latency includes queueing.** All 61 PRs were opened within 6 minutes, so its p50 and p95 partly measure its queue. Ours is per-review latency at 8 concurrent calls.
8. **A single cheap model.** Our numbers are one model, DeepSeek V4.1 Flash. A provider change or model update can move them; the regression gate in item 9 is meant to catch that.
9. **The strong reasoning tier wasn't run.** Sonnet 5.5, GPT-6.1 Sol and V4 Pro with reasoning on didn't fit the budget (item 5). A stronger model might close the remaining recall gap, or might not be worth its 5-15× cost.
10. **CodeRabbit ran on a trial plan.** It was the Advanced plan, not the free OSS plan a public repo would normally get.

## Recommendation

Build: run our reviewer on my own Go repos. Use single-pass with dedup and suppress, and add read-only tools if clean-PR noise matters more than the extra $0.002 a review. The measured edge is large on every quality column, at a fraction of the cost. **The main caveat is that the test set is home ground:** I picked its cases around the bug classes our prompt targets and tuned the pipeline on the same 60 cases. So the next step isn't more tuning. It is item 8: run the reviewer in CI on real PRs, log every finding, and let item 9's feedback loop show whether the edge holds on code it wasn't built around. For a team that wants a reviewer today with no maintenance, CodeRabbit's Major/Critical comments are mostly on target (bug-only precision 0.94). Its weak spot is noise on clean PRs.

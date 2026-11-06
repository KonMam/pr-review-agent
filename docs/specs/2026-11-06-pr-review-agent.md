# PR-review agent with an offline eval and a build-vs-buy comparison

**Goal:** a model-agnostic PR reviewer for Go code that finds bugs, scored on a golden set of 30-50 Go PRs with known bugs and run side by side with CodeRabbit on the same cases. It starts on the cheapest models that work, so there's a build-vs-buy answer backed by numbers rather than a demo.

## Acceptance criteria

- [ ] **Golden set.** `cases/manifest.jsonl` holds 30-50 cases.
  - Each case has provenance (repo, fix commit SHA, licence), the buggy diff, and ground truth as file + line range.
  - Every case is Go. At least 8 come from my own Go repos, and the rest from permissively licensed OSS Go repos.
    **Not met:** item 2 found only 3 usable own-repo fixes (all go-taskqueue); see `cases/README.md`. The set has 41 cases, 38 of them OSS.
  - Each case is tagged with a Go bug class (see Design). No single class makes up more than half the set.
  - A test in `./verify.sh` checks the manifest schema and that every case's diff applies cleanly to its base.
- [ ] **Scorer.** One scorer, shared by both arms, computes precision, recall and F1 using the matching rule below. It's unit-tested on hand-built fixtures in `./verify.sh`, with no network calls.
- [ ] **Model swap is config only.** Each model arm is a named entry in `arms.toml`: base URL, key env var, model id, and price, if the provider doesn't report cost. Adding or swapping a model, or moving a stage to another provider, changes `arms.toml` and no Python. A test runs the pipeline against a stub OpenAI-compatible server under two different arm configs to prove this.
- [ ] **Agent eval.**
  - `uv run pr-review eval --arm <name> --runs 3` runs every case 3 times and writes `results/<run-id>/summary.json`.
  - The summary has precision, recall and F1 (mean and min-max over runs), cost per review in USD, and p50/p95 latency.
  - It also has an ablation table: single-pass, then adding grade, dedup and suppress one at a time.
  - Every run has a hard budget enforced in code (default $1), and the provider key has its own spend limit as a backstop.
- [ ] **No style comments.** Across all cases and runs, the agent posts zero findings in a suppressed category (style, naming, formatting, docs, readability). A test on the stored results checks this.
- [ ] **Build vs buy.** CodeRabbit's comments on the same cases are scored by the same scorer. `docs/build-vs-buy.md` puts both arms in one table and ends with a recommendation that names its main caveat. The table covers precision, recall, F1, cost per review, latency and setup effort.

## Out of scope

- IDE or editor integration. The agent runs from the CLI and from CI only.
- Auto-fixing code or suggesting patches. Findings are text plus a location.
- Style, lint, naming or formatting comments. gofmt, go vet and golangci-lint already do this, so these categories are suppressed, not tuned. The reviewer also doesn't repeat what `go vet` catches.
- Languages other than Go. The harness itself stays in Python, but the reviewer's prompt, bug classes and corpus are Go-only.
- Multi-repo deployment, org-wide rollout, GitHub App installation.
- A UI or dashboard. Results are JSON plus one markdown report.
- Production signals: 👍/👎 buttons and "addressed rate" on live PRs. Uber shows these are the right long-term metric, but they need real traffic. Deferred.
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
- Location-only matching overcounts a little: a finding on the right lines can still give the wrong reason. To bound this, I hand-check 20 sampled true positives per arm and report the share that were wrong.

**Runs and cost.**
- Each case runs 3 times per configuration, because model output varies.
- Results are cached by (case, arm config, stage, run index), so a rerun only pays for what changed.
- Cost comes from the provider's reported cost when there is one (OpenRouter returns it). Otherwise it is tokens × the price in `arms.toml`.

## Plan

One item per session and PR.

- [x] 1. **Case format and scorer:** the manifest schema, loader, matching rule, and P/R/F1 with unit tests on fixtures. No model calls. Update AGENTS.md, which still says "Claude Agent SDK" and "LiteLLM gateway", to match the Decisions below.
- [x] 2. **Golden set:** a script that turns a fix commit into a case (revert the fix → buggy diff, fix hunks → ground truth). Curate 30-50 Go cases from my Go repos and from permissively licensed OSS Go repos, tagged by bug class. Add the manifest validation test.
- [ ] 3. **Agent v0:** the `arms.toml` loader, a single-pass generator, the budget guard, and `eval` with repeated runs, caching, cost and latency. Run it with the cheapest arm to get the first baseline.
- [ ] 4. **Multi-stage pipeline:** grade, dedup and suppress, each toggleable, plus the threshold sweep and the ablation table.
- [ ] 5. **Model ladder:** rerun the best pipeline on 2-3 arms from cheapest to stronger, to see whether a stronger model buys enough precision or recall to justify its cost. Also try read-only tools as an ablation.
- [ ] 6. **CodeRabbit arm:** a public sandbox GitHub repo with one PR per case. Collect CodeRabbit's comments, normalise them to findings, and score them. **This needs my OK before any remote is created or anything is pushed.**
- [ ] 7. **`docs/build-vs-buy.md`:** the comparison table, the hand-check results, and the recommendation.
- [ ] 8. **CI on one Go repo:** a GitHub Action, defined in this repo, that reviews a Go repo's PRs with inline comments. This is the "would I actually use it" check. It runs on one of my Go repos, and wiring it in there **needs my OK first**, since it touches an existing project. One repo only.

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
- **No tool loop in v0.** The diff plus the changed files at the base commit should catch most bugs a reviewer can see. Cross-file bugs are where tools would help, and item 5 measures whether that's worth the extra tokens.
- **Corpus: Go only, mixing my repos with OSS.**
  - The aim is a tool I'd use myself, and Go is what I write most (gbemu, dagr, go-taskqueue, dungeon, duckserve).
  - One language means the prompt, bug classes and corpus can all be specific, and a small golden set isn't spread across languages.
  - Rejected: adding Python, which would halve the cases per language for a language I use less.
  - My Go repos have only about 15 commits that look like real fixes, and not all of them will be usable, so OSS Go bug-fix commits fill the rest. Candidate sources: stdlib-adjacent libraries and tools I use.
  - Own-repo cases are also reported as their own slice.
- **Ground truth is a human fix commit, not an LLM finding.** Scoring against earlier reviewer-subagent findings would be circular. They count as cases only where a later commit fixed them.
- **Reverted fixes as synthetic buggy PRs.** This gives exact locations and no labelling work.
  - The cost: one bug per case, and these PRs are cleaner than real ones, which usually mix the bug with unrelated changes.
  - To offset this, where a fix commit carries other edits, those edits are kept in the diff as noise.
- **Comparison arm: CodeRabbit.** It's the market leader, free on public repos, and makes the vendor benchmark claims (Martian bench) that this setup can test.
  - Rejected: Copilot review (needs a seat), Greptile (paid trial), Claude Code GH Action (same model family as the agent, so it says little about build vs buy).
- **Precision first.** Public benchmarks put the best tools around 50% precision and 50% recall, and raising recall adds noise fast. The threshold is chosen for precision, and recall is reported, not optimised.

## Open questions

- **Are my own repos public?** The CodeRabbit sandbox would republish their code. If they're private, own-repo cases run on the agent arm only.
  - *Answered in item 2 (`gh repo view`, 2026-11-06):* go-taskqueue and duckserve are public; gbemu, dagr and dungeon are private. All 3 own-repo cases are from go-taskqueue, so all of them can go to the CodeRabbit sandbox. The remaining open question is only how to get more own-repo cases.
- **CodeRabbit's free-tier limits** (rate, PR size) on a sandbox with 30-50 PRs. Check them in item 6 before building the case-to-PR script.
- **OpenRouter privacy settings.** Exclude providers that train on or retain prompts before sending any code. That's fine for public OSS and my own code, but not for anyone else's.

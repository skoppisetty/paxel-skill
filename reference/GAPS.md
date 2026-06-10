# Paxel score-reproduction gap register

Goal: a local test that produces a **similar** v3 score to YC's. This maps every gap between the client image and that goal, ranked by impact. Grounded in the 9-agent audit of `source/latest/rails` (file:line throughout).

## v2 status — client-side gaps closed (2026-06-09)

The skill now ports the full client-side episode-construction pipeline. In plain terms (gap-register numbers in parentheses, defined in the tables below):

**Closed.** Transcript condensing was already ported in v1 (G7). v2 adds: the decision-law catalog, bundled and injected verbatim into the classifier prompt (G9); the schema coercions that alter scorer input (G10); commit grouping and session→episode linking with the exact confidence constants, first-match-wins (G11); the deterministic signal layer — events, session signals, plan files, user highlights (`events.py`); decision exchanges with the verbatim classifier prompt and deterministic in-session outcomes (`decisions.py`); and a byte-faithful `build_episode_input` (`episodes.py`). The repo-mounted code-quality and velocity analytics (G13) are ported as a transparency report (`analytics.py`) — in Paxel these are upload-only and never reach the scorer, so they cannot move the local score either way.

**Still open — not present in the client image, unclosable by porting:** the server-side overall-score rollup (G1), possible server-side re-scoring (G2), the weights used to fold git-derived signals into the score (G3), run-to-run nondeterminism and provider drift (G4–G6), and version skew across client builds (G14).

**Deliberate local deviations:** decision-exchange *chains* are not detected — they need YC's embedding service, and the client itself silently produces no chains when embeddings fail, so this matches an existing degradation path; `pr_number` is recovered only from an in-session `gh pr create` (Paxel uses a gh-CLI sidecar built at upload time); the subagent committed-return ratio uses the parent-commit-after-return branch of the Ruby predicate, because child subagent session records don't exist locally.

## Headline correction (supersedes earlier notes)

**The scoring model is `gpt-5.5-none` (GPT-5.5 at reasoning effort "none"), NOT Haiku.** `AnthropicClient::MODELS` maps all tiers `{fast, quality, opus}` → `"gpt-5.5-none"` (`anthropic_client.rb:929-933`). The `claude-haiku-4-5` mapping is a retired rollback comment (`:926-927`). The method is still named `anthropic_model(:fast)` but resolves to GPT-5.5. **Any artifact (tweet/email) that says "Haiku" is now factually wrong for the current build** and is refutable. Use "an LLM (currently GPT-5.5) routed through YC's proxy."

## How scoring actually works (verified)

Raw `.jsonl` → **(deterministic)** discover/chunk/condense → `condensed_text` → **(LLM #1, gpt-5.5-none :quality)** per-session *narrative* → **(LLM #2, gpt-5.5-none :fast)** per-episode *5-axis score* → **(SERVER, absent)** rollup → overall score + band.

- 5 axes: `execution_leverage, steering, engineering_quality, product_thinking, planning` (`episode_summarizer.rb:20`), each 1.0–10.0.
- The scored text is **two LLM hops** from the transcript: the model scores the *narrative*, not the raw text.
- The scoring prompt is **provably complete & server-unwrapped**: `proxy_call_validator.rb:131,168` SHA256-hashes the FULL `build_episode_prompt(nil)` and 403s any mismatch, so the server cannot inject rubric text.
- **No temperature / top_p / seed is set anywhere** (`anthropic_client.rb` call_llm forwards only model/max_tokens/system/messages). Sampling is provider-default on YC's proxy.

## Gap register (ranked)

### Tier 1 — Structural blockers (cannot reproduce; must approximate/fit)
| # | Gap | Evidence | Impact |
|---|---|---|---|
| G1 | **Episode→overall→band rollup is server-side and absent.** No client code aggregates per-episode scores. | `client_pipeline.rb:1524,1752` name `Api::V1::ResultsController#build_v3_results`; no controllers in image | **Decisive** |
| G2 | **Server may re-score, not trust client scores.** Client path passes NO `velocity_context`/`language_band` to the scorer (`client_pipeline.rb:475`); server may re-judge with them injected. | `upload.rb` reads `v3_results` but never writes it; `ServerScoringJob#compute_cross_modal/#import_code_quality` referenced, absent | High |
| G3 | **Folded-in signals have unknown weights.** code_quality (14 dims), parallelism (Orchestrator-mode gate, cross_tool sqrt-taper), steering_trace, decisions — reproducible as *inputs*, not as *score contribution*. | `parallelism_analyzer.rb:5-9,20-22` → `AgenticProficiencyScorer` (absent) | High |

### Tier 2 — Nondeterminism floor (caps "similar" even with perfect logic)
| # | Gap | Evidence | Impact |
|---|---|---|---|
| G4 | **No temperature/seed → run-to-run variance**, compounded over **two** stacked LLM stages (narrative + score). ±0.5–1.5/axis can flip a band at 8.0 (ELITE) or 9.0 (EXEMPLAR). | `episode_summarizer.rb:306-311`; `session_narrative_analyzer.rb:119-126` | High |
| G5 | **Exact GPT-5.5 snapshot + proxy sampling are server-controlled**; a provider model update shifts scores with no code change. | `anthropic_client.rb:914-918` effort via suffix, parsed server-side | High |
| G6 | **Proxy can remap the model** regardless of client request. | `YC_LLM_PROXY_URL` routing, `anthropic_client.rb:845-847` provider nil | Med |

### Tier 3 — Reproducibility hazards (replicate exactly or diverge)
| # | Gap | Evidence | Impact |
|---|---|---|---|
| G7 | **Scored text = `condensed_text`, not raw transcript.** Must run the identical condensing: ToolInputSummarizer drops Write/Edit/MultiEdit bodies + Task prompts→byte+sha; tool_result bodies→byte markers; SecretScrubber; 20K truncation; 5K-token chunking. Feed raw text and you score longer, different text. | `transcript_chunker.rb:408,436-447`; `tool_input_summarizer.rb:96,188-193`; `transcript_session.rb:146-147` | Med-High |
| G8 | **Narrative is LLM-made and cached per-machine.** Fresh machine recomputes (varies); stale cache returns OLD scores for new code. Scoring path itself is **uncached** (always fresh/stochastic). | `llm_result_cache.rb:319-326`; episode_summarizer has no cache call | Med |
| G9 | **`db/decision_catalog.json` (49 laws) injected verbatim** into the DecisionClassifier prompt — pin the data, not just the loader. | `decision_law_catalog.rb:92-96,128-132` | Med |
| G10 | **`db/client_schema.rb` column defaults/types** silently coerce values: `significance` default `tactical`, `reversibility` `unknown`, `domain` `general`; `episodes.scores` sink. A narrower/absent column vs server truncates. | critic finding; `client_schema.rb:122,318,334-336` | Med |
| G11 | **`linking_strategy.rb` link confidences** (pr 1.0/sha 0.9/branch 0.7/timestamp 0.5, 1h overlap) decide which sessions group into an episode. | critic finding `linking_strategy.rb:6,14,25,33,38` | Med |
| G12 | **`lib/tasks/analyze_local.rake` is the real entry point** — decides which sessions/projects/agent-mounts reach the scorer (project filter, `_metadata.json` grouping). Different session set → different score. (The audit's own `find` missed `*.rake`.) | critic finding `analyze_local.rake:39-104` | Med |

### Tier 4 — Inputs beyond transcripts
| # | Gap | Evidence | Impact |
|---|---|---|---|
| G13 | **code_quality + velocity + commit metrics need the real git repo** mounted at `/repo` + `/git_metrics.txt` — transcripts alone insufficient. | `local_code_quality_analyzer.rb:16-18` | Med (server-folded) |

### Tier 5 — Version skew
| # | Gap | Evidence | Impact |
|---|---|---|---|
| G14 | **17 builds differ** (PROMPT_VERSION episode v15 / narrative v5 / decision v5; catalog; model pins). To reproduce a *specific* upload's score, pin the build that scored it; server "graces" old prompt signatures. A single `latest` snapshot can't reproduce earlier builds. | `episode_summarizer.rb:9`; `VERSION` = 0.3.38.3 | Med |

## Verdict: can a local test give a "similar" score?

**Per-axis, directional reads — yes.** You can rebuild the exact scorer input bit-for-bit (the prompt is complete and signature-locked; the condensing + signal pipeline is deterministic) and call gpt-5.5-none. Each session's axis judgments will be directionally similar.

**A matching overall number + band — no, not from the image alone.** The rollup is server-side and unknown (G1), the server may re-score (G2), and nondeterminism over two LLM hops (G4) plus provider drift (G5) means ±1 band at boundaries is the floor.

**The goal is therefore an EMPIRICAL CALIBRATION problem, not a code-port.** The only way to "similar overall score":
1. Reproduce per-episode scores locally (pin **gpt-5.5-none**, set **temperature 0** to kill *your* variance, run the exact condensing pipeline, pin decision_catalog.json + client_schema + linking constants + build VERSION, mount the real repo for code_quality).
2. Collect several real `(your Paxel upload → the overall+band YC showed you)` pairs.
3. **Fit the aggregation weights** (per-axis rollup + band cuts) to match — since the rule isn't in the code, it must be regressed from observed pairs.
4. Accept ±1 band drift as the irreducible floor.

# paxel-skill

A local **self-assessment** that scores your own Claude Code and Codex CLI sessions on YC Paxel's exact 5-axis rubric — **without uploading anything to YC**.

It bundles Paxel's **verbatim** scoring and narrative prompts (extracted from the public `ghcr.io/yc-software/paxel-client` image) and its band-cut thresholds. The one change: every narrative/scoring call is forced to **Claude Haiku 4.5** (`model: haiku`), not YC's gpt-5.5-none proxy. Nothing leaves your machine.

## What it does

```
your ~/.claude/projects/**/*.jsonl  +  ~/.codex/sessions/**/*.jsonl  +  your git repo
  → condense.py        (deterministic: drop tool/file bodies, scrub secrets, chunk;
                        Codex rollouts auto-detected & normalized to the same form)
  → events.py          (deterministic: events, session_signals, user highlights,
                        plan files, dispatch/return pairs — EventExtractor port)
  → gitdata.py         (deterministic: commit grouping + session→episode linking
                        with real git LOC — CommitGrouper/EpisodeLinker port)
  → decisions.py       (extract → Haiku classifier, verbatim v5 prompt + 49-law
                        catalog → finalize with in-session outcome analysis)
  → narrative prompt   (Haiku, verbatim Paxel SYSTEM_PROMPT)    → per-session notes
  → episodes.py        (deterministic: byte-faithful build_episode_input — all
                        blocks, caps, Code volume, dispatch ratio)
  → scoring prompt     (Haiku, verbatim Paxel rubric)           → 5-axis scores
  → aggregate.py       (verbatim band cuts + approx rollup)     → overall + band
  → analytics.py       (optional: the upload-only payload Paxel's server would
                        receive — 14 code-quality dims, velocity, steering traces)
```

Axes: **Execution Leverage, Steering, Engineering Quality, Product Thinking, Planning** (1.0–10.0). Bands: WEAK / LIMITED / STRONG / ELITE / EXEMPLAR.

## What's faithful vs approximate (read this)

| Layer | Fidelity |
|---|---|
| Scoring rubric + calibration | **Verbatim** from the client image |
| Narrative prompt | **Verbatim** |
| Condensing (drop bodies, scrub, caps) | **Faithful port** of the client pipeline |
| Codex CLI ingestion | **Faithful port** of `CodexNormalizer` (boilerplate filter, `apply_patch` → per-file edits, `update_plan` → plan signal), plus one deliberate extension: the newer `custom_tool_call` apply_patch shape, which the archived client misses |
| Event extraction + `session_signals` | **Faithful port** of `EventExtractor` / `SessionSignalExtractor` (verbatim regexes, caps, Ruby string semantics) |
| Commit grouping + episode linking | **Faithful port** of `CommitGrouper` / `EpisodeLinker` / `LinkingStrategy` (pr 1.0 / sha 0.9 / branch 0.7 / ±1h timestamp 0.5; first-match-wins; real numstat LOC) |
| Decision exchanges | **Faithful port** of extractor + classifier (verbatim v5 prompt + 49-law catalog, Haiku-classified) + deterministic in-session outcome analysis; **chains not ported** (need YC's embedding service — the client itself degrades identically when embeddings fail) |
| Episode input (`build_episode_input`) | **Byte-faithful port** — all blocks, caps (50K/30K/10K/5K), Code volume gating, committed-return ratio. `## Code Reviews` absent exactly as in Paxel's own local pipeline (server-side only) |
| Upload-only analytics (14 code-quality dims, velocity, steering, parallelism) | **Faithful port**, exposed as a transparency report (`analytics.py`) — never feeds scores, same as in Paxel |
| Band cut thresholds (<4/<6/<8/<9/≥9) | **Verbatim** |
| Per-axis scores | **Faithful** (model differs — see below) |
| **Overall score + band** | **APPROXIMATION** — YC's rollup is server-side, not in the image |
| Scorer model | **Claude Haiku 4.5** (forced via `model: haiku`), not gpt-5.5-none (Paxel's actual model) — directional, not identical |

LLM scoring is nondeterministic; re-runs vary. The overall number is a calibration target, not YC's verdict. Full gap analysis: [`reference/GAPS.md`](reference/GAPS.md).

**Known local deviations:** decision-exchange chains (need embeddings); `pr_number` recovered from in-session `gh pr create` output instead of Paxel's gh-CLI sidecar; committed-return ratio via the parent-commit-after-return branch (no child subagent session records locally); Gemini CLI / Cursor / opencode ingestion not ported (only Claude Code + Codex CLI here).

## Install (as a Claude Code skill)

Clone straight into your Claude Code skills directory:

```bash
git clone https://github.com/skoppisetty/paxel-skill.git ~/.claude/skills/paxel-skill
```

Start a **new** Claude Code session (skills load at startup), then invoke `/paxel-skill` — or just ask *"score my Claude Code sessions on the Paxel rubric."* Update later with `git -C ~/.claude/skills/paxel-skill pull`.

**Hacking on it?** Clone anywhere and symlink instead, so your edits are live:

```bash
git clone https://github.com/skoppisetty/paxel-skill.git
ln -s "$(pwd)/paxel-skill" ~/.claude/skills/paxel-skill
```

**Requirements:** Claude Code + Python 3 (standard library only — nothing to `pip install`).

> **Note:** scoring is **per-project** — it defaults to the logs for the repo you're currently in (`~/.claude/projects/<encoded-cwd>/`, plus Codex sessions whose `cwd` matches), not your entire history. Point it at another project's log dir to score that one instead.

## Run the scripts directly (no skill)

```bash
python3 scripts/condense.py ~/.claude/projects/<encoded-cwd> > sessions.jsonl
# narrate + score each session with the prompts/ files (any LLM), collect to episodes.json
python3 scripts/aggregate.py episodes.json
```

Tests (stdlib only): `python3 scripts/test_scripts.py`.

## Provenance & IP

Prompts and thresholds are extracted verbatim from YC's **publicly pullable** client image for **personal, interoperability/assessment** use. They are YC's text, not original work here — do not redistribute this as your own rubric. If you publish or share a derivative, reimplement the rubric in your own words.

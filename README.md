# paxel-skill

A local **self-assessment** that scores your own Claude Code sessions on YC Paxel's exact 5-axis rubric — **without uploading anything to YC**.

It bundles Paxel's **verbatim** scoring and narrative prompts (extracted from the public `ghcr.io/yc-software/paxel-client` image) and its band-cut thresholds. The one change: every narrative/scoring call is forced to **Claude Haiku 4.5** (`model: haiku`), not YC's gpt-5.5-none proxy. Nothing leaves your machine.

## What it does

```
your ~/.claude/projects/**/*.jsonl
  → condense.py        (deterministic: drop tool/file bodies, scrub secrets, chunk)
  → narrative prompt   (Haiku, verbatim Paxel SYSTEM_PROMPT)    → per-session notes
  → scoring prompt     (Haiku, verbatim Paxel rubric)           → 5-axis scores
  → aggregate.py       (verbatim band cuts + approx rollup)     → overall + band
```

Axes: **Execution Leverage, Steering, Engineering Quality, Product Thinking, Planning** (1.0–10.0). Bands: WEAK / LIMITED / STRONG / ELITE / EXEMPLAR.

## What's faithful vs approximate (read this)

| Layer | Fidelity |
|---|---|
| Scoring rubric + calibration | **Verbatim** from the client image |
| Narrative prompt | **Verbatim** |
| Condensing (drop bodies, scrub, caps) | **Faithful port** of the client pipeline |
| Band cut thresholds (<4/<6/<8/<9/≥9) | **Verbatim** |
| Per-axis scores | **Faithful** (model differs — see below) |
| **Overall score + band** | **APPROXIMATION** — YC's rollup is server-side, not in the image |
| Scorer model | **Claude Haiku 4.5** (forced via `model: haiku`), not gpt-5.5-none (Paxel's actual model) — directional, not identical |

LLM scoring is nondeterministic; re-runs vary. The overall number is a calibration target, not YC's verdict. Full gap analysis: [`reference/GAPS.md`](reference/GAPS.md).

**Not ported in v1:** commit-cluster episode grouping (needs git), the deterministic `session_signals` counts, decision-exchange extraction, code-quality dimensions. All medium/low impact.

## Install (as a Claude Code skill)

```bash
ln -s "$(pwd)" ~/.claude/skills/paxel-skill      # or copy the folder
```
Then in Claude Code: invoke `/paxel-skill`, or just ask "score my Claude Code sessions on the Paxel rubric." The scripts need only Python 3 stdlib.

## Run the scripts directly (no skill)

```bash
python3 scripts/condense.py ~/.claude/projects/<encoded-cwd> > sessions.jsonl
# narrate + score each session with the prompts/ files (any LLM), collect to episodes.json
python3 scripts/aggregate.py episodes.json
```

Tests (stdlib only): `python3 scripts/test_scripts.py`.

## Provenance & IP

Prompts and thresholds are extracted verbatim from YC's **publicly pullable** client image for **personal, interoperability/assessment** use. They are YC's text, not original work here — do not redistribute this as your own rubric. If you publish or share a derivative, reimplement the rubric in your own words.

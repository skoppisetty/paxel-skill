---
name: paxel-skill
description: Score your own Claude Code sessions on YC Paxel's exact 5-axis rubric, locally, with Claude as the scorer (no upload, no backend). Use when the user wants to see how Paxel would evaluate their work, run a self-assessment on Execution Leverage / Steering / Engineering Quality / Product Thinking / Planning, or check their builder score without sending sessions to YC.
---

# Paxel Skill

Reproduces Paxel's per-session judgment **locally**. The scoring/narrative prompts and the band cuts are **verbatim from Paxel's client image**; the only change is the model — every narrative and scoring call is **dispatched to Claude Haiku 4.5** (the Agent/Task tool with `model: haiku`), instead of YC's gpt-5.5-none proxy. The orchestrating session model only condenses, builds inputs, and aggregates; it never scores.

**Read `reference/GAPS.md` before presenting any result.** The per-axis reads are faithful; the overall score + band are a labeled **approximation** of YC's server-side rollup (which is not in the client image). Never present them as YC's actual verdict.

## Pipeline (run in order)

### 1. Resolve target sessions
Default to the current project's logs: `~/.claude/projects/<encoded-cwd>/*.jsonl` where `<encoded-cwd>` is the absolute working directory with every non-alphanumeric char replaced by `-`. If the user names a project or path, use that. Confirm the session count with the user before scoring many.

### 2. Condense (deterministic — do NOT score raw transcripts)
```
python3 scripts/condense.py <dir-or-files...>
```
Emits one JSON per session: `condensed_text`, `token_estimate`, `too_short`, and `facts` (user/assistant msg counts, tool_uses, **subagent_dispatches, code_edits, git_commits**, first_prompt). **Skip sessions where `too_short` is true** (under ~200 tokens — Paxel doesn't score them).

This is load-bearing: it drops tool-output bodies, file/diff contents, and Task prompts to byte markers exactly as Paxel does. Scoring the raw `.jsonl` instead would score different, longer text.

### 3. Narrate each session
For each scored session, **dispatch a subagent running Claude Haiku** (the Agent/Task tool with `model: haiku`) to write the note — do NOT narrate inline on the orchestrating model. Hand the subagent the full text of `prompts/session_narrative.md` **verbatim as its governing instruction**, followed by the session's `condensed_text` as the input to analyze. It returns the 5-section markdown note + the trailing `<session_intent>` tag. Haiku is the scorer; the orchestrator only relays the prompt and the text.
- **Large sessions:** if `token_estimate > 60000`, split the condensed text on `USER:` boundaries into <60k chunks, dispatch **one Haiku subagent per chunk**, then dispatch **one more Haiku call** to merge the part-notes into a single note that keeps the same five headers and stays under 520 words.
- Capture the `<session_intent>` value (shipping/exploration/ambiguous) — it's needed for session-only scoring.

### 4. Build episodes
v1 = **one episode per session** (Paxel groups by commit clusters; that needs git history, so this is a faithful simplification — note it to the user). Classify `episode_type` from `facts`:
- `git_commits > 0` or `code_edits > 0` → `implementation`
- else → `session_only`

### 5. Score each episode
**Dispatch a subagent running Claude Haiku** (`model: haiku`) for each episode — never score inline on the orchestrating model. Hand it the full text of `prompts/episode_scoring.md` **verbatim as its governing instruction**, then, as the input to score, this **exact structure** (Paxel's `build_episode_input`), including only the blocks that apply:
```
Episode type: <episode_type>
Sessions: 1, Commit groups: 0

Session intent: <intent>        # only for session_only episodes
First prompts: <facts.first_prompt truncated to ~200 chars>

## Session Narratives
<the narrative from step 3>

## Subagent Dispatch Activity        # only if facts.subagent_dispatches > 0
Dispatches: <facts.subagent_dispatches> | Committed-return ratio: n/a (no git data locally)
```
- **Do NOT emit a "Code volume:" line** — there's no git LOC locally, and the prompt forbids inventing one. (This is fine; it just means EL is judged from the narrative, not a LOC anchor.)
- Output is the rubric's JSON object: `title, facts, interpretation, counterweight, confidence, scores{...}`. Honor **axis omission** — for `session_only` episodes omit `execution_leverage` and `engineering_quality`; omit any axis with no evidence.

### 6. Aggregate
Collect the per-episode `{scores, confidence}` into a JSON list and run:
```
python3 scripts/aggregate.py episodes.json
```
Returns `axes_APPROX`, `overall_score_APPROX`, `band_APPROX` (WEAK/LIMITED/STRONG/ELITE/EXEMPLAR). The band cuts are verbatim; the rollup is a confidence-weighted mean (a labeled approximation, since YC's rule is server-side).

### 7. Present
Lead with the **per-axis profile** (the faithful, useful part) and each episode's title + counterweight. Show `overall_score_APPROX` + `band_APPROX` only with the explicit caveat that the composite is an approximation of YC's server-side number, not their verdict, with a ±1 band floor.

## Honesty rails (do not skip)
- The scorer is **Claude Haiku 4.5**, forced via `model: haiku` on every narrative/score call. Paxel's actual scorer is **gpt-5.5-none** (see `reference/GAPS.md`); Haiku is the closest fast-tier Claude analog, not the same model. Reads are directionally similar, not identical.
- LLM scoring is **nondeterministic**; re-runs vary. Don't present a number as definitive.
- The **overall/band is an approximation**; only per-axis reads and band thresholds are faithful.
- **Not ported in v1** (state if relevant): commit-cluster episode grouping, the deterministic `session_signals` 10-key counts, decision-exchange extraction, code-quality dimensions. These are medium/low impact — see `reference/GAPS.md`.

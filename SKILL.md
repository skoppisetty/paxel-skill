---
name: paxel-skill
description: Score your own Claude Code and Codex CLI sessions on YC Paxel's exact 5-axis rubric, locally, with Claude as the scorer (no upload, no backend). Use when the user wants to see how Paxel would evaluate their work, run a self-assessment on Execution Leverage / Steering / Engineering Quality / Product Thinking / Planning, or check their builder score without sending sessions to YC.
---

# Paxel Skill

Reproduces Paxel's judgment **locally**. The scoring/narrative/classifier prompts and the band cuts are **verbatim from Paxel's client image**, and the episode-construction pipeline (commit grouping, session→episode linking, session signals, decision exchanges, plan files, dispatch stats) is a **faithful port** of the client code. The only model change: every LLM call is **dispatched to Claude Haiku 4.5** (the Agent/Task tool with `model: haiku`) instead of YC's gpt-5.5-none proxy. The orchestrating session model never scores — it only runs the deterministic scripts and relays prompts.

**Read `reference/GAPS.md` before presenting any result.** The per-axis reads are faithful; the overall score + band are a labeled **approximation** of YC's server-side rollup (which is not in the client image). Never present them as YC's actual verdict.

## Pipeline (run in order)

Use a working directory like `/tmp/paxel-run/` for intermediates. All scripts are stdlib-only Python 3.

### 1. Resolve target sessions + repo
Default to the current project's logs from **both** supported CLIs:
- **Claude Code:** `~/.claude/projects/<encoded-cwd>/*.jsonl` where `<encoded-cwd>` is the absolute working directory with every non-alphanumeric char replaced by `-`.
- **Codex CLI:** `~/.codex/sessions/**/*.jsonl` (date-bucketed, not per-project). Keep only sessions whose `cwd` matches the target project (use condense.py's `cwd` field). Skip if absent.

The **git repo** is the current project's working directory (Paxel mounts the real repo; episode linking and LOC need it). If the user names a different project/path, use that. Confirm the session count with the user before scoring many.

### 2. Condense (deterministic — narrative input)
```
python3 scripts/condense.py <dir-or-files...> > condensed.jsonl
```
One JSON per session: `condensed_text`, `token_estimate`, `too_short`, `agent_type`, `cwd`, `facts`. **Skip sessions where `too_short` is true.** Codex rollouts are auto-normalized (CodexNormalizer port). This is load-bearing: it drops tool-output bodies and file contents to byte markers exactly as Paxel does.

### 3. Extract events + signals (deterministic)
```
python3 scripts/events.py <session.jsonl ...> > sessions.jsonl
```
Run this on the SAME session set that survived the `too_short` filter in step 2 — a too-short session must not enter episode linking either (mirrors Paxel's discovery-time exclusion; otherwise it joins an episode with no narrative behind it).
Per-session events (git commits/SHAs/branches, test runs, subagent dispatch/return, agent proposals, user directives), the 10-key `session_signals`, `user_highlights`, versioned plan files, active time windows, `pr_number` (only from an in-session `gh pr create`).

### 4. Group commits + link episodes (deterministic)
```
python3 scripts/gitdata.py --repo <repo> --sessions sessions.jsonl --out gitdata.json
```
Ports CommitGrouper (PR groups → 2h-gap clusters → singles) and EpisodeLinker (pr 1.0 / sha 0.9 / branch 0.7 / ±1h timestamp 0.5; first-match-wins; orphans → `session_only` at 0.3). Episodes carry real `added_lines`/`deleted_lines` from numstat.

### 5. Decision exchanges (deterministic extract → Haiku classify → deterministic finalize)
```
python3 scripts/decisions.py extract --sessions sessions.jsonl --out candidates.json --batches batches.json
```
Then for **each batch**, dispatch a Haiku subagent (`model: haiku`): governing instruction = full text of `prompts/decision_classifier.md` **verbatim**; input = the batch's `input_text`. It must return ONLY the JSON array (`[{index, is_decision, decision_type, confidence, narrative, law_key}]`). Collect per-batch arrays **in batches.json order** into `cls.json` (a JSON list of lists; use `[]` for a failed batch — the script falls back to regex classification per session). Then:
```
python3 scripts/decisions.py finalize --candidates candidates.json --classifications cls.json --sessions sessions.jsonl --out decisions.json
```
For large runs, batch multiple classifier calls per subagent or run them via a Workflow — but each classification must be a Haiku call with the verbatim prompt.

### 6. Narrate each session (Haiku)
For each scored session, dispatch a Haiku subagent: governing instruction = full text of `prompts/session_narrative.md` **verbatim**, input = the session's `condensed_text`. Save each result (5-section note + trailing `<session_intent>` tag) to `narratives/<session_id>.md`.
- **Large sessions:** if `token_estimate > 60000`, split on `USER:` boundaries into <60k chunks, one Haiku call per chunk, then one Haiku merge call (same five headers, under 520 words, one intent tag).

### 7. Assemble episode inputs (deterministic)
```
python3 scripts/episodes.py --sessions sessions.jsonl --episodes gitdata.json --narratives narratives/ --decisions decisions.json --out-dir inputs/
```
Byte-faithful `build_episode_input` port: header + Code volume (only when commits carry numstat), Session intent (session_only majority), First prompts (first 5, deduped), `## Session Narratives` (50K cap), `## User Highlights` (10K), `## Decision Exchanges`, `## Plan Files` (5K/file), `## Session Signals`, `## Subagent Dispatch Activity` (real committed-return ratio). `## Code Reviews` is absent because Paxel's own client never populates it locally (server-side only). Produces `inputs/episodes_manifest.json`.

### 8. Score each episode (Haiku)
For each manifest entry, dispatch a Haiku subagent: governing instruction = full text of `prompts/episode_scoring.md` **verbatim**; input = the episode's `inputs/<episode_id>.txt` content. Output is the rubric JSON: `title, facts, interpretation, counterweight, confidence, scores{...}`. Honor **axis omission** — for `session_only` episodes omit `execution_leverage` and `engineering_quality`; omit any axis with no evidence.

### 9. Aggregate
Collect per-episode `{scores, confidence}` into a JSON list and run:
```
python3 scripts/aggregate.py episodes.json
```
Returns `axes_APPROX`, `overall_score_APPROX`, `band_APPROX` (WEAK/LIMITED/STRONG/ELITE/EXEMPLAR). Band cuts verbatim; rollup is a confidence-weighted mean (labeled approximation — YC's rule is server-side).

### 10. Present (+ optional analytics report)
Lead with the **per-axis profile** and notable episodes (title + counterweight). Show `overall_score_APPROX` + `band_APPROX` only with the explicit caveat that the composite approximates YC's server-side number (±1 band floor). Optionally run the upload-payload transparency report (what Paxel's server would receive; it does NOT affect scores):
```
python3 scripts/analytics.py --repo <repo> --sessions sessions.jsonl --md report.md
```

## Honesty rails (do not skip)
- The scorer is **Claude Haiku 4.5**, forced via `model: haiku` on every narrative/classifier/score call. Paxel's actual scorer is **gpt-5.5-none** (see `reference/GAPS.md`); reads are directionally similar, not identical.
- LLM scoring is **nondeterministic**; re-runs vary. Don't present a number as definitive.
- The **overall/band is an approximation**; only per-axis reads and band thresholds are faithful.
- **Local deviations** (state if relevant): decision exchange **chains** are not detected (they need YC's embedding service; the client itself degrades identically when embeddings fail); `pr_number` comes from in-session `gh pr create` evidence instead of Paxel's gh-CLI sidecar; committed-return uses the parent-commit-after-return branch (no child subagent session records locally); `## Code Reviews` is absent exactly as in Paxel's local pipeline. See `reference/GAPS.md` for the full register.

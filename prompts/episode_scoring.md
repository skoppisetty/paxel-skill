You are scoring a developer's work episode on 5 axes.

IMPORTANT FRAMING:
- This developer uses Claude Code (an AI coding agent). Score the HUMAN's engineering
  judgment: their architectural decisions, problem identification, quality standards,
  and technical direction.
- Score the builder's JUDGMENT about calibrating effort to the task's demands.
  Efficiently delegating small well-scoped work quickly is good judgment.
  Thoroughly speccing and exploring before a sweeping feature or refactor is
  also good judgment. Both should score well. Penalize miscalibration:
  over-planning trivial tasks or under-planning complex ones.
- If agent configuration files (CLAUDE.md, .cursorrules, AGENTS.md, etc.) have explicit constraints, this demonstrates engineering discipline.
- If there's eval infrastructure, this shows testing sophistication beyond typical developers.
- User-directed pivots show active oversight, not scope creep.
- High total output (LOC) is NOT by itself evidence of execution leverage. Execution leverage
  measures outcome per input, not throughput — volume without sustained outcome does not score
  high. Large AI-driven output earns credit ONLY when the evidence shows the human held the
  line: caught AI mistakes, kept the architecture coherent, and closed outcome loops at scale.
  Score the outcomes that shipped, not the line count. Do NOT cite a specific lines-of-code
  figure unless it appears verbatim in the provided input; never estimate one or round to a
  headline number like "100K".

## Axis Anchors
### Execution Leverage (EL): Does their effort close outcome loops?
1-2: No outcome leverage. Work started but never closed a loop — reverts, abandoned approaches, or open-ended exploration with no decision or commit. Volume without outcome.
3-4: Low leverage. Some commits land but the ratio of ship to effort is poor. Frequent re-work on the same files without learning. Subagent dispatches (if any) don't reliably produce committed code.
5-6: Moderate leverage. Regular commits proportional to session time. Plan files when present mostly ship. If subagents are used, roughly half produce code that makes it into a commit.
7-8: Strong leverage. Plan-then-ship loops close cleanly on non-trivial work. Subagent dispatches produce committed output more often than not. A short session that ships a hard surgical fix scores here — the signal is outcome per input, not volume.
9-10: Exceptional leverage. The operator's harness is doing real work: most plan files ship, most dispatched subagents return usable commits, and the code-to-effort ratio stays high across the upload. Volume WITH outcome sustained.

### Steering (S): Do they control the AI?
1-2: Passive acceptance. Takes whatever the AI generates.
3-4: Minimal steering. Occasionally rejects bad output.
5-6: Moderate steering. Sets constraints, rejects bad suggestions, gives reasonable direction.
7-8: Strong steering. Calibrates direction to the task — terse precise prompts for small fixes, detailed specs for complex work. Rejects bad paths, forms hypotheses, makes nuanced decisions connecting local choices to broader architecture.
9-10: Exceptional. Calibrates AI collaboration style to each task's demands. Decision exchanges show deep reasoning with high hit rate on irreversible decisions. May include agent config infrastructure or delegation patterns, but these are evidence of sophistication, not requirements.

### Engineering Quality (EQ): Are they a good engineer?
1-2: Poor engineering judgment. No tests where tests matter, no error handling where failures are likely, no architecture for complex systems. Code works by accident.
3-4: Basic structure. Tests and error handling are absent or generic even when the task demanded them. No security awareness on security-relevant changes.
5-6: Reasonable engineering for the task's demands. Has tests for complex logic but may skip them for trivial changes (which is fine). Error handling is present where it matters. Some technical debt accumulation.
7-8: Good engineering judgment calibrated to the task. Tests where they matter, not boilerplate tests on config changes. Error handling targets specific failure modes. Code evolves — dead code deleted, complexity managed. Security awareness on relevant changes. The key signal: do they apply engineering rigor WHERE it counts?
9-10: Excellent calibration. Comprehensive tests on complex systems, lightweight verification on simple changes. Defense-in-depth security where warranted. Active simplification — the codebase gets better over time. Engineering effort is proportional to risk and complexity.

### Product Thinking (PT): Do they understand users?
1-2: Missing product awareness on user-facing work. Makes UX-impacting changes without considering the user experience. (Infrastructure-only work without product concerns is NOT a 1-2 — omit this axis instead.)
3-4: Occasionally considers users but doesn't drive decisions from user needs on work that affects them.
5-6: Considers user-facing impact when relevant. Makes reasonable product decisions on user-facing work. Doesn't over-explain straightforward config or infra changes.
7-8: Drives user-facing decisions from user needs. Iterates on UX. Calibrates product thinking to the task — deep UX consideration for user-facing features, minimal ceremony for config and infrastructure.
9-10: Product innovation as engineering output. Every user-facing decision grounded in user impact. Builds methodology or tooling that multiplies effectiveness. Recognizes when infrastructure work IS product work.

### Planning (P): Do they think before they build?
1-2: Poor calibration of planning effort. No forethought on complex work that needed it, OR wastes time over-planning trivial tasks.
3-4: Inconsistent. Sometimes plans complex work, sometimes doesn't. Plans lack substance when present.
5-6: Reasonable judgment about when to plan. Plans when the task warrants it, skips for straightforward changes. Plans are adequate but not detailed.
7-8: Good calibration. Jumps straight into small fixes (correct — that IS good planning judgment). Brings substantive forethought to complex work, whether a written plan, a design sketch, or clear pre-commit reasoning. Plans have clear steps and scope awareness.
9-10: Excellent calibration of planning to complexity. Detailed plans with verification steps and alternatives for complex work. Quick decisive action on simple tasks. Demonstrates clear judgment about what level of planning each task demands.

## Calibration — use the full 1-10 scale
Calibrate every score against the full population of skilled operators who drive AI
coding agents — not against an absolute ideal. Use the WHOLE 1-10 range. Do not default to 7-8.
- 7 is the MEDIAN competent operator. Solid, unremarkable-for-the-task work is a 7 — a
  genuinely good score, but not a high one. Most episodes land 5-8.
- Score each axis ONLY on its own evidence. The most common scoring error is a HALO:
  letting a generally-impressive episode lift all five axes together. Resist it — most
  axes in most episodes are 6-8, even when the episode is strong overall.
- Reserve 8 for a clearly-above-median axis, and 9-10 for the ONE or TWO axes (rarely
  more) where the episode is genuinely exemplary — the kind of judgment you would hold up
  as an example to other strong engineers. Grant 9-10 for that standout axis when the
  evidence supports it — do NOT withhold it out of caution; the top of the scale must be reachable.
- Every OTHER axis stays at its honest median (typically 6-7). Do NOT park non-standout
  axes at 8. A strong episode's profile is UNEVEN — a 9 on the standout axis sitting next
  to a 6 on an axis the episode barely touched is normal and correct. A flat 8-8-8-8 is the
  halo error, not excellence.
- Use 3-5 when an axis is clearly below median for what the task demanded. Both tails are
  real and should be populated — not every axis is a 7.
- Before finalizing, COUNT your axes at 8 or above. More than two is almost always a halo:
  go back and put the merely-solid (not exemplary) axes at 6-7 where the per-axis evidence
  actually lands. Ask: is this score driven by evidence specific to THIS axis, or by the
  episode's overall vibe?

Score this episode on all 5 axes (1.0-10.0). Output ONLY a JSON object:
{
  "title": "What the developer did in <=140 chars. Action-oriented. Example: 'Built OAuth login flow with Google SSO and session management'",
  "facts": "2-3 sentences: what specifically happened in this episode",
  "interpretation": "1-2 sentences: what this reveals about the developer",
  "counterweight": "1 sentence: what might argue against a high score",
  "confidence": 0.8,
  "scores": {
    "execution_leverage": 7.0,
    "steering": 6.5,
    "engineering_quality": 7.0,
    "product_thinking": 5.5,
    "planning": 6.0
  }
}

Rules:
- Score the HUMAN's judgment, not the AI's code generation.
- Use the axis anchors to calibrate. A 7 should match the 7-8 anchor description.
- confidence: 0.0-1.0 based on evidence available for this episode.
- AXIS OMISSION (critical): If an axis has no direct evidence in this episode, DO NOT include it in the scores object. Do not score an axis at 1-3 as a "low default" — that's worse than omitting, because it claims you measured something you didn't. Omit the key entirely. A missing axis means "insufficient evidence," not "bad performance."
- EFFORT CALIBRATION: Score the builder's judgment about matching effort to the task. Quickly delegating a small fix with a terse prompt is high-quality work if the fix lands clean. Thoroughly exploring and planning before a major refactor is high-quality work even if no code ships in that session. Penalize miscalibration (over-engineering trivial tasks, under-planning complex ones), not the task's inherent scope.
- For session_only episodes (no commits, no code changes):
  - OMIT execution_leverage and engineering_quality — these axes require code artifacts.
  - If session intent is "exploration": The developer was intentionally learning or investigating. This is often good judgment — understanding a system before changing it. Score the QUALITY of their exploration: good questions, systematic investigation, architectural understanding. Do NOT penalize for lack of code output.
  - If session intent is "shipping": The developer intended to produce code but didn't. Consider whether they showed good judgment despite incomplete output (correctly diagnosing a problem, forming a plan) vs. being stuck.
  - If session intent is "ambiguous" or absent: Score only steering, product_thinking, and planning if you have specific evidence.
- For bugfix episodes: execution_leverage should reflect outcome leverage, not LOC. A surgical 50-LOC fix for a complex race condition IS high execution_leverage — the loop closed efficiently. What scores low is scope without outcome (lots of edits, no commit; long session, no resolution).
- Cross-tool dispatches embedded in parent narratives: a parent session's narrative may end with a "## Cross-tool dispatches" block listing dispatched Codex (or other) review/exec calls. Treat these blocks as SUPPORTING EVIDENCE under the parent's event — the dispatched work is part of the parent's loop, not a separate session. Do not score the dispatch as if it were independent work; do not double-count its output. The dispatch is signal of seeking second opinions / reviewer separation (engineering judgment), which feeds Steering and Engineering Quality where relevant. The parent's commits that followed the dispatch ARE outcome leverage; the dispatch alone is not.

Writing rules for title, facts, and interpretation (these are shown to the user):
- Use plain English. The user is a developer, so standard engineering terms are fine.
- NEVER state a lines-of-code (LOC) figure in the title, facts, or interpretation unless it
  appears verbatim in the input (a "Code volume:" line, or a number inside the Code Reviews).
  Do not estimate, round, or extrapolate LOC — "~100K LOC shipped" is FORBIDDEN when no LOC
  figure was provided. Describe scope qualitatively (surgical fix / broad change) instead.
- Never use Paxel-internal terms: cross-modal, spread drag, human-AI pair,
  V3, calibration signal, scoring context, decision intelligence graph.
- Agent config files (CLAUDE.md, .cursorrules, etc.), plan mode, eval infrastructure, bisectable commits are fine to mention
  since the user knows these concepts from their own workflow.

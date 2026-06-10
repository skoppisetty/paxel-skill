#!/usr/bin/env python3
"""
episodes.py — assemble per-episode scorer inputs (EpisodeSummarizer port).

Faithful port of the episode-input half of Paxel's EpisodeSummarizer
(rails/app/services/episode_summarizer.rb): preload_episode_data (:183-261),
compute_dispatch_stats (:279-295), build_episode_input (:356-426) and
summarize_signals (:456-467). The LLM scoring call itself is dispatched by
the orchestrating Claude session, not here — this module produces the exact
input text the scorer reads.

Local fidelity notes (vs the Ruby):
  - code_reviews (CommitGroup#code_review) is NEVER populated client-side
    (server fills it post-upload), so the "## Code Reviews" block is absent
    here exactly as it is in Paxel's own local pipeline.
  - dispatch_with_committed_return_ratio delegates to ParallelismAnalyzer in
    Ruby; locally there are no child subagent session records, so the
    operative branch is the parent-commit-after-return fallback
    (parallelism_analyzer.rb:81-87) — reused from analytics.py.
  - Sessions have no is_subagent/triggered_by_id locally (events.py only
    emits logical-root project transcripts), so the logical-root filter at
    :237 is a no-op here.

Inputs:
  --sessions   sessions.jsonl from scripts/events.py
  --episodes   JSON from scripts/gitdata.py ({commit_groups, episodes})
  --narratives directory of <session_id>.md narrative notes (each ending
               with the <session_intent>...</session_intent> tag)
  --decisions  decisions.json from scripts/decisions.py finalize (optional)
  --out-dir    output directory: one <episode_id>.txt per episode plus
               episodes_manifest.json

Usage:
  python3 scripts/episodes.py --sessions s.jsonl --episodes g.json \
      --narratives narrs/ --decisions d.json --out-dir inputs/
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decisions import ruby_truncate, summarize_decisions  # noqa: E402
from analytics import parent_committed_after_return, ruby_round  # noqa: E402

# episode_summarizer.rb:19
PLAN_FILE_CONTENT_LIMIT = 5_000

# episode_summarizer.rb:460-462 — aggregation key order is the literal list
# order (Hash.new(0) inserts keys on first `+=`, so rendering preserves it).
SIGNAL_KEYS = [
    "kill_decisions", "self_corrections", "hypothesis_driven",
    "domain_corrections", "debugging_messages", "architecture_discussions",
    "product_references", "imperative_prompts", "review_checks", "critiques",
]

INTENT_TAG = re.compile(
    r"<session_intent>\s*(shipping|exploration|ambiguous)\s*</session_intent>",
    re.IGNORECASE)


def blank(text):
    """Rails String#blank? — nil, empty, or whitespace-only."""
    return text is None or str(text).strip() == ""


def load_sessions(path):
    sessions = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            sessions[rec["session_id"]] = rec
    return sessions


def load_narrative(narratives_dir, session_id):
    """Return (narrative_text, session_intent) for a session, (None, None)
    when no note exists. The intent tag is parsed out and stripped, mirroring
    SessionNarrativeAnalyzer's extraction into separate columns."""
    path = os.path.join(narratives_dir, "%s.md" % session_id)
    if not os.path.exists(path):
        return None, None
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = INTENT_TAG.search(text)
    intent = m.group(1).lower() if m else None
    narrative = INTENT_TAG.sub("", text).strip()
    return (narrative if narrative else None), intent


def latest_plan_files(session):
    """PlanFile.latest_versions scoped per-session: max version per filename,
    version_count = that session's version count for the filename
    (episode_summarizer.rb:218-224)."""
    by_name = {}
    for pf in session.get("plan_files") or []:
        name = pf.get("filename")
        cur = by_name.setdefault(name, {"latest": pf, "count": 0})
        cur["count"] += 1
        if (pf.get("version") or 0) >= (cur["latest"].get("version") or 0):
            cur["latest"] = pf
    out = []
    for name, cur in by_name.items():
        out.append({
            "filename": name,
            "version_count": cur["count"],
            "content": ruby_truncate(str(cur["latest"].get("content") or ""),
                                     PLAN_FILE_CONTENT_LIMIT),
        })
    return out


def compute_dispatch_stats(main_sessions):
    """episode_summarizer.rb:279-295 (v9 semantics). Returns
    [dispatch_count, ratio-or-None]. The committed-return predicate is the
    locally operative ParallelismAnalyzer branch (parent commit after a
    subagent_return)."""
    dispatching = [
        s for s in main_sessions
        if any(e.get("type") == "subagent_dispatch"
               for e in s.get("events") or [])
    ]
    if not dispatching:
        return 0, None
    total = sum(
        sum(1 for e in s.get("events") or []
            if e.get("type") == "subagent_dispatch")
        for s in dispatching)
    if total == 0:
        return 0, None
    with_committed = sum(
        1 for s in dispatching
        if parent_committed_after_return(s.get("events") or []))
    ratio = ruby_round(with_committed / len(dispatching), 2)
    return total, ratio


def summarize_signals(signals_list):
    """episode_summarizer.rb:456-467 — sum the ten keys across sessions,
    keep >0, render 'key: value' joined with ', ' in canonical order."""
    aggregated = {}
    for signals in signals_list:
        if not isinstance(signals, dict):
            continue
        for key in SIGNAL_KEYS:
            val = signals.get(key)
            try:
                val = int(val)
            except (TypeError, ValueError):
                val = 0
            aggregated[key] = aggregated.get(key, 0) + val
    return ", ".join("%s: %d" % (k, aggregated[k])
                     for k in SIGNAL_KEYS if aggregated.get(k, 0) > 0)


def preload_episode_data(episode, sessions_by_id, commit_groups_by_id,
                         narratives_dir, decisions):
    """preload_episode_data (:183-261) for one episode."""
    sessions = [sessions_by_id[sid] for sid in episode["session_ids"]
                if sid in sessions_by_id]
    commit_groups = [commit_groups_by_id[cid]
                     for cid in episode.get("commit_group_ids") or []
                     if cid in commit_groups_by_id]

    narratives, intents = [], []
    for s in sessions:
        narrative, intent = load_narrative(narratives_dir, s["session_id"])
        if narrative is not None:
            narratives.append(narrative)
        if intent is not None:
            intents.append(intent)

    session_ids = {s["session_id"] for s in sessions}
    episode_decisions = sorted(
        (d for d in decisions
         if d.get("session_id") in session_ids
         and d.get("significance") != "tactical"),
        key=lambda d: d.get("event_index") or 0)

    plan_file_data, seen_plans = [], set()
    for s in sessions:
        for pf in latest_plan_files(s):
            if pf["filename"] in seen_plans:
                continue
            seen_plans.add(pf["filename"])
            plan_file_data.append(pf)

    tally = {}
    for intent in intents:
        tally[intent] = tally.get(intent, 0) + 1

    dispatch_count, dispatch_ratio = compute_dispatch_stats(sessions)

    return {
        "episode_id": episode["episode_id"],
        "episode_type": episode["episode_type"],
        "narratives": ruby_truncate("\n---\n".join(narratives), 50_000),
        "signals": [s.get("session_signals") for s in sessions
                    if s.get("session_signals") is not None],
        "user_highlights": ruby_truncate(
            "\n---\n".join(s["user_highlights"] for s in sessions
                           if s.get("user_highlights")), 10_000),
        "first_prompts": [s["first_prompt"] for s in sessions
                          if s.get("first_prompt")],
        "session_count": len(sessions),
        "commit_group_count": len(commit_groups),
        "added_lines": sum(int(cg.get("insertions") or 0)
                           for cg in commit_groups),
        "deleted_lines": sum(int(cg.get("deletions") or 0)
                             for cg in commit_groups),
        "decision_summary": summarize_decisions(episode_decisions),
        "plan_files": plan_file_data,
        "session_intents": tally,
        "dispatch_count": dispatch_count,
        "dispatch_with_committed_return_ratio": dispatch_ratio,
    }


def build_episode_input(data):
    """Verbatim port of build_episode_input (:356-426)."""
    parts = []
    parts.append("Episode type: %s" % data["episode_type"])
    parts.append("Sessions: %d, Commit groups: %d"
                 % (data["session_count"], data["commit_group_count"]))

    added = int(data.get("added_lines") or 0)
    deleted = int(data.get("deleted_lines") or 0)
    if int(data.get("commit_group_count") or 0) > 0 and (added + deleted) > 0:
        parts.append(
            "Code volume: +%d/-%d lines (from this episode's commits)"
            % (added, deleted))

    if data["episode_type"] == "session_only" and data.get("session_intents"):
        tally = data["session_intents"]
        max_count = max(tally.values())
        winners = [k for k, c in tally.items() if c == max_count]
        dominant = winners[0] if len(winners) == 1 else "ambiguous"
        parts.append("Session intent: %s" % dominant)

    clean_first_prompts = []
    for p in data.get("first_prompts") or []:
        if str(p).lstrip().startswith("You are Codex"):
            continue
        if p not in clean_first_prompts:
            clean_first_prompts.append(p)
    if clean_first_prompts:
        parts.append("First prompts: %s" % " | ".join(clean_first_prompts[:5]))

    if not blank(data.get("narratives")):
        parts.append("## Session Narratives\n%s" % data["narratives"])

    if not blank(data.get("code_reviews")):
        parts.append("## Code Reviews\n%s" % data["code_reviews"])

    if not blank(data.get("user_highlights")):
        parts.append("## User Highlights\n%s" % data["user_highlights"])

    if not blank(data.get("decision_summary")):
        parts.append("## Decision Exchanges\n%s" % data["decision_summary"])

    if data.get("plan_files"):
        plan_parts = [
            "### %s (%d version(s))\n%s"
            % (pf["filename"], pf["version_count"], pf["content"])
            for pf in data["plan_files"]
        ]
        parts.append("## Plan Files\n%s" % "\n\n".join(plan_parts))

    if data.get("signals"):
        signals_summary = summarize_signals(data["signals"])
        if signals_summary:
            parts.append("## Session Signals\n%s" % signals_summary)

    if int(data.get("dispatch_count") or 0) > 0:
        ratio = data.get("dispatch_with_committed_return_ratio")
        ratio_str = "n/a" if ratio is None else "%.2f" % ratio
        parts.append(
            "## Subagent Dispatch Activity\nDispatches: %d | "
            "Committed-return ratio: %s (fraction of dispatching main "
            "sessions where the dispatch→return→ship loop closed "
            "at least once; 1.0 = all, 0.0 = none)"
            % (data["dispatch_count"], ratio_str))

    return "\n\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--sessions", required=True)
    ap.add_argument("--episodes", required=True)
    ap.add_argument("--narratives", required=True)
    ap.add_argument("--decisions", default=None)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    sessions_by_id = load_sessions(args.sessions)
    with open(args.episodes, encoding="utf-8") as f:
        gitdata = json.load(f)
    commit_groups_by_id = {cg["id"]: cg
                           for cg in gitdata.get("commit_groups") or []}
    decisions = []
    if args.decisions and os.path.exists(args.decisions):
        with open(args.decisions, encoding="utf-8") as f:
            decisions = json.load(f)

    os.makedirs(args.out_dir, exist_ok=True)
    manifest = []
    for episode in gitdata.get("episodes") or []:
        data = preload_episode_data(episode, sessions_by_id,
                                    commit_groups_by_id, args.narratives,
                                    decisions)
        text = build_episode_input(data)
        input_path = os.path.join(args.out_dir,
                                  "%s.txt" % episode["episode_id"])
        with open(input_path, "w", encoding="utf-8") as f:
            f.write(text)
        manifest.append({
            "episode_id": episode["episode_id"],
            "episode_type": episode["episode_type"],
            "session_count": data["session_count"],
            "commit_group_count": data["commit_group_count"],
            "session_ids": episode["session_ids"],
            "first_prompt": (data["first_prompts"][0]
                             if data["first_prompts"] else ""),
            "input_path": os.path.abspath(input_path),
        })

    manifest_path = os.path.join(args.out_dir, "episodes_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    print("wrote %d episode inputs to %s" % (len(manifest), args.out_dir))


if __name__ == "__main__":
    main()

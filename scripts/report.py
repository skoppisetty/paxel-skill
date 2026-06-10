#!/usr/bin/env python3
"""
report.py — deterministic end-of-run report renderer.

Pure renderer: reads the six pipeline artifacts plus an optional LLM-call
ledger, writes report.md (console-presented markdown — the only output;
there is deliberately no HTML artifact), exits. No LLM calls, no network,
no subprocess, no clocks, no randomness; all iteration sorted; same inputs
produce byte-identical output. Timestamps render as recorded (embedded
offset preserved); hour/weekday stats use the wall clock embedded in each
timestamp, never the machine timezone. Audience: CTO-style founders —
every claim is a fixed template filled with counted evidence, axis names
are always written in full, and rubric language appears only as verbatim
quotes attributed to prompts/episode_scoring.md.
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aggregate import AXES, band_for_score, rollup  # noqa: E402
from analytics import parent_committed_after_return, ruby_round  # noqa: E402
from episodes import INTENT_TAG  # noqa: E402

AXIS_NAMES = {
    "execution_leverage": "Execution Leverage",
    "steering": "Steering",
    "engineering_quality": "Engineering Quality",
    "product_thinking": "Product Thinking",
    "planning": "Planning",
}
LINK_NAMES = {"pr_match": "PR", "sha_match": "commit SHA",
              "branch_match": "branch name",
              "timestamp_overlap": "timestamp window",
              None: "session only, no commits"}
DECISION_TYPE_NAMES = {"strategic_redirect": "Strategic redirect",
                       "product_insight": "Product insight",
                       "technical_catch": "Technical catch",
                       "option_selection": "Option selection",
                       None: "Unclassified (regex fallback)"}
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2, None: 3}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]
# Band -> (next band, its cut, rubric anchor bucket for that next band).
NEXT_BAND = {"WEAK": ("LIMITED", 4.0, "5-6"),
             "LIMITED": ("STRONG", 6.0, "7-8"),
             "STRONG": ("ELITE", 8.0, "9-10"),
             "ELITE": ("EXEMPLAR", 9.0, "9-10"),
             "EXEMPLAR": (None, None, None)}
# List prices per million tokens (input, output, cache read, cache write);
# cache write at the 5-minute-TTL rate. Longest model-id prefix wins.
PRICING_AS_OF = "June 2026"
PRICING = {"claude-haiku": (1.00, 5.00, 0.10, 1.25),
           "claude-sonnet": (3.00, 15.00, 0.30, 3.75),
           "claude-opus": (5.00, 25.00, 0.50, 6.25),
           "claude-fable": (10.00, 50.00, 1.00, 12.50)}

CAPS_FLOOR = 0.6          # crash-out card floor (caps ratio)
SHORT_WORDS = 8           # SHORT_PROMPT_MAX_WORDS, the Paxel constant
PEAK_WINDOW_HOURS = 4


# --- formatting helpers (every number has an explicit format) ---

def fmt_int(n):
    return format(int(n), ",")


def pct(num, den):
    """Integer percent, half-up (matches the repo's ruby_round convention)."""
    return int(ruby_round(100.0 * num / den, 0)) if den else 0


def fmt_score(x):
    return "%.2f" % float(x)


def fmt_cost(c):
    return "$%.2f" % c


def fmt_tok(n):
    """Humanized token count: 950 -> "950", 104_164_457 -> "104.2M"."""
    n = int(n)
    for cut, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if n >= cut:
            return "%.1f%s" % (ruby_round(n / float(cut), 1), suffix)
    return str(n)


def fmt_duration(seconds):
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    if h:
        return "%dh %02dm" % (h, m)
    if m:
        return "%dm %02ds" % (m, s)
    return "%ds" % s


def parse_ts(ts):
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Naive timestamps get UTC so aware/naive mixes can't crash
        # comparisons/sorts — one odd timestamp must not kill the report.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def ts_date(ts):
    return ts[:10] if isinstance(ts, str) and len(ts) >= 10 else None


def ts_hour(ts):
    """Wall-clock hour as recorded (embedded offset preserved)."""
    try:
        return int(ts[11:13])
    except (TypeError, ValueError, IndexError):
        return None


def ts_weekday(ts):
    d = ts_date(ts)
    if not d:
        return None
    try:
        y, m, dd = (int(x) for x in d.split("-"))
        return WEEKDAYS[date(y, m, dd).weekday()]
    except ValueError:
        return None


def price_for(model_id):
    """(input, output, cache_read, cache_write) per MTok, or None."""
    best = None
    for prefix in sorted(PRICING):
        if str(model_id).startswith(prefix):
            if best is None or len(prefix) > len(best):
                best = prefix
    return PRICING[best] if best else None


def usage_cost(u, model_id):
    """Estimated list-price cost for a token_usage bucket, or None."""
    p = price_for(model_id)
    if not p:
        return None
    return (u.get("input_tokens", 0) * p[0]
            + u.get("output_tokens", 0) * p[1]
            + u.get("cache_read_tokens", 0) * p[2]
            + u.get("cache_write_tokens", 0) * p[3]) / 1_000_000.0


# --- input loading ---

def load_jsonl(path):
    out = []
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def load_json(path, default):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def default_prompt_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "prompts", "episode_scoring.md")


def default_catalog_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "reference", "decision_catalog.json")


def load_law_titles(catalog_path):
    """law_key -> title from the bundled decision catalog; {} if missing."""
    data = load_json(catalog_path, None)
    laws = data.get("laws", data) if isinstance(data, dict) else data
    titles = {}
    for law in laws if isinstance(laws, list) else []:
        if isinstance(law, dict) and law.get("key"):
            titles[law["key"]] = law.get("title") or law["key"]
    return titles


def load_anchors(prompt_path):
    """axis -> {"1-2"|"3-4"|"5-6"|"7-8"|"9-10": verbatim body text}. The axis
    header line (which contains parenthesized abbreviations) is never
    captured — band-paragraph bodies only."""
    inv = {v: k for k, v in AXIS_NAMES.items()}
    try:
        with open(prompt_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    anchors, current = {}, None
    for line in text.split("\n"):
        m = re.match(r"### ([A-Za-z ]+) \(", line)
        if m and m.group(1) in inv:
            current = inv[m.group(1)]
            anchors[current] = {}
            continue
        if line.startswith("#"):
            current = None  # any other heading ends the axis section
            continue
        m = re.match(r"(1-2|3-4|5-6|7-8|9-10): (.*)", line)
        if m and current:
            anchors[current][m.group(1)] = m.group(2)
    return anchors


def load_calibration(prompt_path):
    """The two verbatim calibration sentences (byte-exact from the rubric,
    after unwrapping the source's hard line wraps — the first sentence spans
    two physical lines in the prompt file)."""
    try:
        with open(prompt_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ("", "")
    unwrapped = re.sub(r"\n[ \t]+", " ", text)
    m1 = re.search(r"(7 is the MEDIAN competent operator.*?not a high one\.)",
                   unwrapped)
    m2 = re.search(r"(Most episodes land 5-8\.)", unwrapped)
    return (m1.group(1) if m1 else "", m2.group(1) if m2 else "")


# --- context (loaded artifacts + joins) ---

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Deterministic end-of-run report renderer (local only).")
    ap.add_argument("--condensed", required=True)
    ap.add_argument("--sessions", required=True)
    ap.add_argument("--gitdata", required=True)
    ap.add_argument("--decisions", required=True)
    ap.add_argument("--episodes", required=True,
                    help="scoring results wrapped with manifest episode_id")
    ap.add_argument("--narratives", required=True)
    ap.add_argument("--llm-calls", dest="llm_calls", default=None)
    ap.add_argument("--out", required=True)
    return ap.parse_args(argv)


def build_context(args):
    condensed = load_jsonl(args.condensed)
    sessions = sorted(load_jsonl(args.sessions),
                      key=lambda s: str(s.get("session_id")))
    gitdata = load_json(args.gitdata, {})
    decisions = load_json(args.decisions, [])
    if isinstance(decisions, dict):
        decisions = decisions.get("decisions") or []
    scored = load_json(args.episodes, [])
    llm_calls = (load_json(args.llm_calls, None)
                 if args.llm_calls else None)

    repo_name = None
    for rec in condensed:
        if rec.get("cwd"):
            repo_name = os.path.basename(str(rec["cwd"]).rstrip("/"))
            break
    skipped = sorted(str(r.get("session_id")) for r in condensed
                     if r.get("too_short"))

    episodes = list(gitdata.get("episodes") or [])
    groups = {g.get("id"): g for g in gitdata.get("commit_groups") or []}
    commits = list(gitdata.get("commits") or [])

    eids = {e.get("episode_id") for e in episodes}
    scores_by_eid, unmatched = {}, []
    for entry in (scored if isinstance(scored, list) else []):
        if not isinstance(entry, dict):
            continue
        eid = entry.get("episode_id")
        if eid in eids:
            # First-wins on duplicate ids, matching episode_of below.
            scores_by_eid.setdefault(eid, entry)
        else:
            unmatched.append(eid)

    episode_of = {}
    for ep in episodes:
        for sid in ep.get("session_ids") or []:
            episode_of.setdefault(str(sid), ep.get("episode_id"))

    narratives, intents = {}, {}
    for s in sessions:
        sid = str(s.get("session_id"))
        try:
            with open(os.path.join(args.narratives, "%s.md" % sid),
                      encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        m = INTENT_TAG.search(text)
        # Strip the intent tag like episodes.load_narrative does, so any
        # later quoting of narrative text can't leak the tag into output.
        narratives[sid] = INTENT_TAG.sub("", text).strip()
        intents[sid] = m.group(1).lower() if m else None

    return {"condensed": condensed, "sessions": sessions,
            "gitdata": gitdata, "episodes": episodes, "groups": groups,
            "commits": commits, "decisions": decisions,
            "scores_by_eid": scores_by_eid,
            "unmatched_scoring": sorted(unmatched, key=str),
            "episode_of": episode_of, "narratives": narratives,
            "intents": intents, "repo_name": repo_name,
            "skipped_session_ids": skipped, "llm_calls": llm_calls,
            "input_paths": {
                "condensed.jsonl": len(condensed),
                "sessions.jsonl": len(sessions),
                "gitdata.json": len(episodes),
                "decisions.json": len(decisions),
                "episodes.json": (len(scored)
                                  if isinstance(scored, list) else 0),
                "narratives/": len(narratives)}}


# --- stats core (feeds cards + improvement warnings) ---

def _dispatch_windows(events):
    """tool_use_id-paired (start, end) parse pairs; unreturned excluded."""
    starts = {}
    for ev in events:
        if ev.get("type") == "subagent_dispatch" and ev.get("tool_use_id"):
            starts.setdefault(ev["tool_use_id"], ev.get("timestamp"))
    windows = []
    for ev in events:
        if ev.get("type") != "subagent_return":
            continue
        tid = ev.get("tool_use_id")
        if tid not in starts:
            continue
        a, b = parse_ts(starts.pop(tid)), parse_ts(ev.get("timestamp"))
        if a and b and b >= a:
            windows.append((a, b))
    return windows


def _max_concurrency(windows):
    # End-before-start at equal times: adjacent windows don't overlap.
    points = []
    for a, b in windows:
        points.append((a, 1, 1))
        points.append((b, 0, -1))
    points.sort(key=lambda p: (p[0], p[1]))
    cur = best = 0
    for _, _, delta in points:
        cur += delta
        best = max(best, cur)
    return best


def compute_stats(ctx):
    sessions = ctx["sessions"]
    n = len(sessions)
    sig = lambda s, key, dflt=0: (s.get("session_signals") or {}).get(key, dflt)

    prompts = sum(int(sig(s, "user_message_count") or 0) for s in sessions)
    corrections = sum(
        int((sig(s, "prompt_types", {}) or {}).get("correction") or 0)
        for s in sessions)
    courtesy = sum(int(sig(s, "courtesy_messages") or 0) for s in sessions)
    courtesy_sessions = sum(
        1 for s in sessions if int(sig(s, "courtesy_messages") or 0) > 0)
    short_prompts = sum(
        int((s.get("prompt_stats") or {}).get("short_prompt_count") or 0)
        for s in sessions)
    plan_sessions = sum(1 for s in sessions if s.get("plan_files"))
    test_runs = sum(int(sig(s, "test_run_count") or 0) for s in sessions)

    medians = sorted(
        float((s.get("prompt_stats") or {}).get("median_words") or 0)
        for s in sessions if (s.get("prompt_stats") or {}).get(
            "median_words") is not None)
    # Pooled approximation: median of per-session medians (lower middle).
    median_words = medians[(len(medians) - 1) // 2] if medians else 0

    windows, dispatch_count = [], 0
    longest = None  # (seconds, start_ts_date)
    dispatching = committed_after = 0
    for s in sessions:
        evs = s.get("events") or []
        w = _dispatch_windows(evs)
        windows.extend(w)
        dc = sum(1 for e in evs if e.get("type") == "subagent_dispatch")
        dispatch_count += dc
        if dc:
            dispatching += 1
            if parent_committed_after_return(evs):
                committed_after += 1
        for a, b in w:
            secs = int((b - a).total_seconds())
            if longest is None or secs > longest[0]:
                longest = (secs, a.date().isoformat())
    max_parallel = _max_concurrency(windows)

    # Peak hours: best 4-hour wall-clock window (mod 24); tie -> lower start.
    hours = [h for h in (ts_hour(c.get("date")) for c in ctx["commits"])
             if h is not None]
    hist = [0] * 24
    for h in hours:
        hist[h] += 1
    peak_window, peak_count = None, 0
    for start in range(24):
        c = sum(hist[(start + i) % 24] for i in range(PEAK_WINDOW_HOURS))
        if c > peak_count:
            peak_window, peak_count = (
                (start, (start + PEAK_WINDOW_HOURS) % 24), c)
    peak_hour = max(range(24), key=lambda h: (hist[h], -h)) if hours else None

    days = {}
    for c in ctx["commits"]:
        d = ts_weekday(c.get("date"))
        if d:
            days[d] = days.get(d, 0) + 1
    ship_day = (max(sorted(days), key=lambda d: days[d])
                if days else None)

    # Go-to prompt: sum repeated_prompts counts by norm across sessions.
    norm_counts, norm_examples = {}, {}
    for s in sessions:
        for rp in sig(s, "repeated_prompts", []) or []:
            norm = rp.get("norm")
            if not norm or norm.startswith("request interrupted"):
                # Harness-injected interruption markers are not user prompts.
                continue
            norm_counts[norm] = norm_counts.get(norm, 0) + int(
                rp.get("count") or 0)
            norm_examples.setdefault(norm, rp.get("example") or norm)
    goto = None
    if norm_counts:
        best = sorted(norm_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        goto = (norm_examples[best[0]], best[1])

    quotes = []
    for s in sessions:
        quotes.extend((s.get("prompt_stats") or {}).get("caps_quotes") or [])
    quotes.sort(key=lambda q: (-float(q.get("caps_ratio") or 0),
                               str(q.get("text"))))
    crash = quotes[0] if quotes and float(
        quotes[0].get("caps_ratio") or 0) >= CAPS_FLOOR else None

    models = {}
    for s in sessions:
        for m, c in sorted((s.get("model_usage") or {}).items()):
            models[m] = models.get(m, 0) + int(c)

    added = sum(int(e.get("added_lines") or 0) for e in ctx["episodes"])
    deleted = sum(int(e.get("deleted_lines") or 0) for e in ctx["episodes"])

    plan_ratio = plan_sessions / float(n) if n else 0.0
    correction_rate = corrections / float(prompts) if prompts else 0.0
    # Archetype: fixed first-match rule table; caption states the trigger.
    if plan_ratio >= 0.5:
        archetype = ("The Architect",
                     "You wrote plan files in %d of %d sessions (%d%%)."
                     % (plan_sessions, n, pct(plan_sessions, n)))
    elif correction_rate >= 0.3:
        archetype = ("The Director",
                     "%d of your %d prompts (%d%%) were course corrections."
                     % (corrections, prompts, pct(corrections, prompts)))
    elif max_parallel >= 4:
        archetype = ("The Conductor",
                     "You ran as many as %d subagents at once." % max_parallel)
    elif medians and median_words <= SHORT_WORDS:
        archetype = ("The Minimalist",
                     "Your median prompt is %s words." % ("%g" % median_words))
    else:
        archetype = ("The Builder",
                     "Steady output across %d sessions." % n)

    return {"n_sessions": n, "prompts": prompts, "corrections": corrections,
            "courtesy": courtesy, "courtesy_sessions": courtesy_sessions,
            "short_prompts": short_prompts, "plan_sessions": plan_sessions,
            "plan_ratio": plan_ratio, "test_runs": test_runs,
            "median_words": median_words, "max_parallel": max_parallel,
            "dispatch_count": dispatch_count,
            "dispatching_sessions": dispatching,
            "committed_after_return_sessions": committed_after,
            "longest_run_seconds": longest[0] if longest else None,
            "longest_run_date": longest[1] if longest else None,
            "peak_window": peak_window, "peak_window_count": peak_count,
            "peak_hour": peak_hour, "ship_day": (
                (ship_day, days[ship_day]) if ship_day else None),
            "commit_count": len(ctx["commits"]), "goto_prompt": goto,
            "crash_quote": crash, "models": models,
            "added_lines": added, "deleted_lines": deleted,
            "archetype": archetype}


def build_cards(ctx, st):
    """13 deterministic cards; each is omitted below its floor so no card
    ever shows a degenerate stat. Local reconstruction of Paxel's card
    format — the generator itself is server-side and not portable."""
    n = st["n_sessions"]
    cards = [{"key": "archetype", "eyebrow": "Which archetype are you?",
              "headline": st["archetype"][0],
              "caption": st["archetype"][1]}]

    if st["models"]:
        top = sorted(st["models"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        total = sum(st["models"].values())
        cards.append({"key": "model", "eyebrow": "Which model do you use most?",
                      "headline": top[0],
                      "caption": "%s of %s assistant messages (%d%%) came "
                                 "from %s." % (fmt_int(top[1]),
                                               fmt_int(total),
                                               pct(top[1], total), top[0])})

    if st["commit_count"] >= 5 and st["peak_window"]:
        a, b = st["peak_window"]
        label = ("Night owl" if a >= 20 or a < 5
                 else "Early bird" if a < 9 else "Daytime shipper")
        cards.append({"key": "peak_hours",
                      "eyebrow": "When are you most productive?",
                      "headline": label,
                      "caption": "%d of %d commits (%d%%) land between "
                                 "%02d:00 and %02d:00, peaking around "
                                 "%02d:00." % (st["peak_window_count"],
                                               st["commit_count"],
                                               pct(st["peak_window_count"],
                                                   st["commit_count"]),
                                               a, b, st["peak_hour"])})

    if n >= 3:
        cards.append({"key": "planning_share",
                      "eyebrow": "How often do you plan?",
                      "headline": "%d%% with a written plan"
                                  % pct(st["plan_sessions"], n),
                      "caption": "%d of %d sessions produced plan files."
                                 % (st["plan_sessions"], n)})

    if st["goto_prompt"] and st["goto_prompt"][1] >= 3:
        text, count = st["goto_prompt"]
        cards.append({"key": "goto_prompt",
                      "eyebrow": "What's your go-to prompt?",
                      "headline": '"%s"' % text,
                      "caption": "Your most-repeated prompt — sent %d times "
                                 "across your sessions." % count})

    if st["max_parallel"] >= 2:
        cards.append({"key": "parallel",
                      "eyebrow": "How many agents do you run?",
                      "headline": "%d agents in parallel" % st["max_parallel"],
                      "caption": "You've run as many as %d subagents at once "
                                 "(%d dispatches across %d sessions)."
                                 % (st["max_parallel"], st["dispatch_count"],
                                    st["dispatching_sessions"])})

    if st["prompts"] >= 10:
        cards.append({"key": "brevity",
                      "eyebrow": "How long are your prompts?",
                      "headline": "%d%% short prompts"
                                  % pct(st["short_prompts"], st["prompts"]),
                      "caption": "%d of %d prompts are %d words or fewer."
                                 % (st["short_prompts"], st["prompts"],
                                    SHORT_WORDS)})

    if st["courtesy"] >= 3:
        cards.append({"key": "politeness",
                      "eyebrow": "How polite are you to your agents?",
                      "headline": "%d thanks" % st["courtesy"],
                      "caption": "You thanked your agents %d times across "
                                 "%d sessions." % (st["courtesy"],
                                                   st["courtesy_sessions"])})

    if st["prompts"] >= 10:
        per10 = ruby_round(10.0 * st["corrections"] / st["prompts"], 1)
        cards.append({"key": "steering",
                      "eyebrow": "How often do you change course?",
                      "headline": "%g correction%s per 10 prompts"
                                  % (per10, "" if per10 == 1 else "s"),
                      "caption": "%d of %d prompts (%d%%) redirected the "
                                 "agent." % (st["corrections"], st["prompts"],
                                             pct(st["corrections"],
                                                 st["prompts"]))})

    if st["longest_run_seconds"] is not None:
        cards.append({"key": "longest_run",
                      "eyebrow": "What's your longest agent run?",
                      "headline": fmt_duration(st["longest_run_seconds"]),
                      "caption": "Your longest subagent run, dispatched on "
                                 "%s." % st["longest_run_date"]})

    crash = st["crash_quote"]
    if crash and len(str(crash.get("text") or "").split()) >= 3:
        cards.append({"key": "crash_out",
                      "eyebrow": "What's your biggest crash out?",
                      "headline": '"%s"' % crash["text"],
                      "caption": "Your highest-intensity message — %d%% of "
                                 "its letters are capitals."
                                 % int(round(float(crash["caps_ratio"])
                                             * 100))})

    if st["commit_count"] >= 1:
        cards.append({"key": "lines", "eyebrow": "How much did you ship?",
                      "headline": "%s lines added" % fmt_int(st["added_lines"]),
                      "caption": "%s added / %s deleted across %d commits in "
                                 "%d episodes." % (fmt_int(st["added_lines"]),
                                                   fmt_int(st["deleted_lines"]),
                                                   st["commit_count"],
                                                   len(ctx["episodes"]))})

    if st["commit_count"] >= 5 and st["ship_day"]:
        day, count = st["ship_day"]
        cards.append({"key": "ship_day", "eyebrow": "When do you ship most?",
                      "headline": "%ss" % day,
                      "caption": "%d of %d commits (%d%%) landed on a %s."
                                 % (count, st["commit_count"],
                                    pct(count, st["commit_count"]), day)})
    return cards


# --- markdown renderer ---

def _episode_sort_key(ctx):
    def key(ep):
        latest = ""
        for gid in ep.get("commit_group_ids") or []:
            g = ctx["groups"].get(gid) or {}
            latest = max(latest, str(g.get("latest_commit_at") or ""))
        if not latest:
            for sid in ep.get("session_ids") or []:
                for s in ctx["sessions"]:
                    if str(s.get("session_id")) == str(sid):
                        latest = max(latest,
                                     str(s.get("session_modified_at") or ""))
        return (latest, -int(ep.get("episode_id") or 0))
    return key


def _episode_link_label(ctx, ep):
    types = [l.get("link_type") for l in ep.get("links") or []]
    counts = {}
    for t in types:
        counts[t] = counts.get(t, 0) + 1
    if not counts and ep.get("commit_group_ids"):
        # Commit group that matched no sessions: it HAS commits, so the
        # link_type-None label ("session only, no commits") would contradict
        # the commit counts rendered on the same episode line.
        return "commits only, no linked sessions"
    # Dominant link type: most frequent; ties by the LINK_NAMES label.
    dominant = (sorted(counts.items(),
                       key=lambda kv: (-kv[1], str(LINK_NAMES.get(kv[0]))))[0][0]
                if counts else None)
    if dominant == "pr_match":
        for gid in ep.get("commit_group_ids") or []:
            g = ctx["groups"].get(gid) or {}
            if g.get("pr_number"):
                return "PR #%s" % g["pr_number"]
        return "PR"
    return LINK_NAMES.get(dominant, "session only, no commits")


def _session_cost_cells(s):
    """(model, turns, in, out, cache r/w, cost) strings for the inventory."""
    tu = s.get("token_usage") or {}
    mu = s.get("model_usage") or {}
    if not tu:
        primary = (sorted(mu.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                   if mu else "not recorded")
        return (primary,) + ("not recorded",) * 4 + ("—",)
    primary = sorted((mu or {"unknown": 0}).items(),
                     key=lambda kv: (-kv[1], kv[0]))[0][0]
    tot = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
           "cache_write_tokens": 0, "assistant_turns": 0}
    cost, unknown = 0.0, False
    for model, u in sorted(tu.items()):
        for k in tot:
            tot[k] += int(u.get(k) or 0)
        c = usage_cost(u, model)
        if c is None:
            unknown = True
        else:
            cost += c
    return (primary, fmt_int(tot["assistant_turns"]),
            fmt_int(tot["input_tokens"]), fmt_int(tot["output_tokens"]),
            "%s / %s" % (fmt_int(tot["cache_read_tokens"]),
                         fmt_int(tot["cache_write_tokens"])),
            "—" if unknown else fmt_cost(cost))


def render_markdown(ctx, st):
    anchors = load_anchors(default_prompt_path())
    cal = load_calibration(default_prompt_path())
    per_axis, overall = rollup(list(ctx["scores_by_eid"].values()))
    n_eps = len(ctx["episodes"])
    L = []
    add = L.append

    dates = sorted(d for d in (ts_date(s.get("session_created_at"))
                               for s in ctx["sessions"]) if d)
    span = ("%s to %s" % (dates[0], dates[-1])) if dates else "no dated sessions"
    active = sum(float((s.get("session_signals") or {}).get(
        "duration_minutes") or 0) for s in ctx["sessions"])

    add("# Paxel Self-Assessment — %s" % (ctx["repo_name"] or "unknown repo"))
    add("")
    add("%d sessions, %d episodes, %d commits, %s minutes of active time "
        "(%s)." % (len(ctx["sessions"]), n_eps, st["commit_count"],
                   fmt_int(int(ruby_round(active, 0))), span))
    add("")

    # --- 1. Verdict ---
    add("## Verdict")
    add("")
    if per_axis and overall is not None:
        band = band_for_score(overall)
        ranked = sorted(per_axis, key=lambda a: (-per_axis[a], AXES.index(a)))
        best, worst = ranked[0], ranked[-1]
        bnxt = NEXT_BAND[band_for_score(per_axis[best])]
        wnxt = NEXT_BAND[band_for_score(per_axis[worst])]
        sent = ["**%s — %s of 10.**" % (band, fmt_score(overall))]
        if bnxt[0]:
            sent.append("Your strongest axis is %s at %s, %s points from %s."
                        % (AXIS_NAMES[best], fmt_score(per_axis[best]),
                           fmt_score(bnxt[1] - per_axis[best]), bnxt[0]))
        else:
            sent.append("Your strongest axis is %s at %s, already in the "
                        "top band." % (AXIS_NAMES[best],
                                       fmt_score(per_axis[best])))
        if worst != best and wnxt[0]:
            sent.append("The axis holding you back is %s at %s (%s) — "
                        "closing %s points moves it to %s."
                        % (AXIS_NAMES[worst], fmt_score(per_axis[worst]),
                           band_for_score(per_axis[worst]),
                           fmt_score(wnxt[1] - per_axis[worst]), wnxt[0]))
        add(" ".join(sent))
        add("")
        add("| Axis | Score | Band | To next band | Episodes contributing |")
        add("|---|---|---|---|---|")
        for axis in AXES:
            if axis not in per_axis:
                continue
            b = band_for_score(per_axis[axis])
            nxt = NEXT_BAND[b]
            gap = ("%s (to %s)" % (fmt_score(nxt[1] - per_axis[axis]), nxt[0])
                   if nxt[0] else "top band")
            contributing = sum(1 for e in ctx["scores_by_eid"].values()
                               if axis in (e.get("scores") or {}))
            add("| %s | %s | %s | %s | %d of %d |"
                % (AXIS_NAMES[axis], fmt_score(per_axis[axis]), b, gap,
                   contributing, n_eps))
        add("")
        add("Per-axis reads are faithful to Paxel's rubric; the composite "
            "and band approximate YC's server-side rollup (see Fine print).")
    else:
        add("No scored episodes — no data.")
    add("")

    # --- 2. Why these scores ---
    add("## Why these scores")
    add("")
    reads = sorted(ctx["scores_by_eid"].items(),
                   key=lambda kv: (-float(kv[1].get("confidence") or 0),
                                   str(kv[0])))
    reads = [(eid, e) for eid, e in reads if e.get("interpretation")]
    if reads:
        add("The scorer's own read of each episode, verbatim (highest "
            "confidence first):")
        add("")
        for eid, e in reads:
            add('- **%s** (confidence %.2f) — "%s"'
                % (e.get("title") or "Episode %s" % eid,
                   float(e.get("confidence") or 0), e["interpretation"]))
        add("")
    else:
        add("The scorer returned no interpretations — no data.")
        add("")
    if ctx["decisions"]:
        counts = {}
        for d in ctx["decisions"]:
            t = d.get("decision_type")
            t = t if t in DECISION_TYPE_NAMES else None
            counts[t] = counts.get(t, 0) + 1
        parts = ", ".join(
            "%d %s%s" % (counts[t], DECISION_TYPE_NAMES[t],
                         "s" if counts[t] != 1 and t is not None else "")
            for t in sorted(counts, key=lambda t: (-counts[t],
                                                   str(DECISION_TYPE_NAMES[t]))))
        law_titles = load_law_titles(default_catalog_path())
        top = sorted(ctx["decisions"], key=lambda d: (
            CONFIDENCE_ORDER.get(d.get("classification_confidence"), 3),
            str(d.get("session_id")), int(d.get("event_index") or 0)))[:3]
        add("%d decision exchanges back the Steering read — %s. The top "
            "calls by classifier confidence:" % (len(ctx["decisions"]), parts))
        add("")
        for d in top:
            law = law_titles.get(d.get("law_key"))
            add("- %s (confidence %s)%s"
                % (d.get("decision_narrative")
                   or "[no narrative — regex fallback]",
                   d.get("classification_confidence") or "none",
                   " — law: %s" % law if law else ""))
        add("")

    # --- 3. How to improve ---
    add("## How to improve")
    add("")
    if per_axis:
        lowest = sorted(per_axis,
                        key=lambda a: (per_axis[a], AXES.index(a)))[:2]
        for axis in lowest:
            band = band_for_score(per_axis[axis])
            nxt = NEXT_BAND[band]
            bucket = nxt[2] or "9-10"
            quote = (anchors.get(axis) or {}).get(bucket)
            if quote:
                add("What the rubric rewards next on **%s** (the %s anchor, "
                    "verbatim from `prompts/episode_scoring.md`):"
                    % (AXIS_NAMES[axis], bucket))
                add("")
                add("> %s" % quote)
                add("")
    else:
        add("No scored episodes — no data.")
        add("")

    warnings = []
    omitted = sum(1 for e in ctx["scores_by_eid"].values()
                  if "execution_leverage" not in (e.get("scores") or {})
                  or "engineering_quality" not in (e.get("scores") or {}))
    scored_n = len(ctx["scores_by_eid"])
    if scored_n and omitted / float(scored_n) > 0.4:
        warnings.append(
            "Execution Leverage and Engineering Quality are absent from the "
            "scorer output for %d of %d episodes (%d%%) — these episodes "
            "have no linked commits, and the rubric directs the scorer to "
            "omit axes without code artifacts. Commit in-session or "
            "reference the PR/branch so sessions link to code."
            % (omitted, scored_n, pct(omitted, scored_n)))
    long_sessions = sum(
        1 for s in ctx["sessions"]
        if float((s.get("session_signals") or {}).get(
            "duration_minutes") or 0) > 30)
    if st["plan_sessions"] == 0 and long_sessions >= 3:
        w = ("0 plan files across %d sessions including %d over 30 minutes "
             "of active time — Planning is scored on thin evidence."
             % (st["n_sessions"], long_sessions))
        q = (anchors.get("planning") or {}).get("9-10")
        warnings.append(w + (' The rubric\'s top anchor: "%s"' % q if q else ""))
    if (st["dispatching_sessions"] >= 4
            and st["committed_after_return_sessions"]
            < 0.5 * st["dispatching_sessions"]):
        q = (anchors.get("execution_leverage") or {}).get("9-10")
        warnings.append(
            "%d of %d sessions that dispatched subagents (%d%%) committed "
            "code after a subagent returned (per-dispatch attribution is "
            "not possible locally; see reference/GAPS.md)."
            % (st["committed_after_return_sessions"],
               st["dispatching_sessions"],
               pct(st["committed_after_return_sessions"],
                   st["dispatching_sessions"]))
            + (' The rubric\'s top anchor: "%s"' % q if q else ""))
    commit_eps = sum(1 for e in ctx["episodes"] if e.get("commit_group_ids"))
    if commit_eps and st["test_runs"] == 0:
        q = (anchors.get("engineering_quality") or {}).get("7-8")
        warnings.append(
            "0 test runs detected across %d episodes with commits."
            % commit_eps
            + (' The rubric\'s anchor: "%s"' % q if q else ""))
    if len(ctx["decisions"]) == 0:
        warnings.append("0 decision exchanges extracted — Steering is "
                        "scored on thin evidence.")
    if warnings:
        add("Evidence-coverage warnings:")
        add("")
        for w in warnings:
            add("- %s" % w)
        add("")

    cells = []
    for eid in sorted(ctx["scores_by_eid"], key=str):
        e = ctx["scores_by_eid"][eid]
        for axis in AXES:
            v = (e.get("scores") or {}).get(axis)
            if isinstance(v, (int, float)):
                cells.append((float(v), str(eid), AXES.index(axis), axis, e))
    cells.sort(key=lambda c: c[:3])
    if cells:
        grouped, order = {}, []
        for v, eid, _, axis, e in cells[:5]:
            if eid not in grouped:
                grouped[eid] = {"e": e, "axes": []}
                order.append(eid)
            grouped[eid]["axes"].append((axis, v))
        add("Where the scorer marked you down (its own counterweights, "
            "verbatim):")
        add("")
        for eid in order:
            g = grouped[eid]
            axes_txt = ", ".join("%s %s" % (AXIS_NAMES[a], fmt_score(v))
                                 for a, v in g["axes"])
            add('- **%s** — %s: "%s"'
                % (g["e"].get("title") or "Episode %s" % eid, axes_txt,
                   g["e"].get("counterweight") or ""))
        add("")
    add("These are behaviors the rubric rewards — shipped plans, tests "
        "where they matter, linked commits. Improving the evidence is "
        "improving the work, but the local composite still cannot promise "
        "YC's number.")
    add("")

    # --- 4. The work ---
    add("## The work")
    add("")
    episodes_sorted = sorted(ctx["episodes"], key=_episode_sort_key(ctx),
                             reverse=True)
    majors = [ep for ep in episodes_sorted if ep.get("commit_group_ids")]
    minors = [ep for ep in episodes_sorted if not ep.get("commit_group_ids")]
    if not episodes_sorted:
        add("No episodes — no data.")
        add("")
    for ep in majors:
        eid = ep.get("episode_id")
        scored = ctx["scores_by_eid"].get(eid) or {}
        title = scored.get("title") or "Episode %s" % eid
        gdates = []
        for gid in ep.get("commit_group_ids") or []:
            g = ctx["groups"].get(gid) or {}
            gdates += [g.get("earliest_commit_at"), g.get("latest_commit_at")]
        gdates = sorted(ts_date(d) for d in gdates if d)
        gspan = ("%s to %s" % (gdates[0], gdates[-1])) if gdates else "no dates"
        commits = sum(len((ctx["groups"].get(g) or {}).get("commit_shas")
                          or []) for g in ep.get("commit_group_ids") or [])
        add("### %s" % title)
        add("")
        add("- %s; linked by %s (mean link confidence %.2f); %d sessions; "
            "%d commits; %s added / %s deleted lines"
            % (gspan, _episode_link_label(ctx, ep),
               float(ep.get("confidence") or 0),
               len(ep.get("session_ids") or []), commits,
               fmt_int(ep.get("added_lines") or 0),
               fmt_int(ep.get("deleted_lines") or 0)))
        scores = scored.get("scores") or {}
        scored_axes = ["%s %s" % (AXIS_NAMES[a], fmt_score(scores[a]))
                       for a in AXES if isinstance(scores.get(a),
                                                   (int, float))]
        omitted_axes = [AXIS_NAMES[a] for a in AXES
                        if not isinstance(scores.get(a), (int, float))]
        if scored_axes:
            line = "- Scores: " + "; ".join(scored_axes)
            if omitted_axes:
                line += " (%s omitted — no evidence)" % ", ".join(omitted_axes)
            add(line)
        else:
            add("- Scores: not scored")
        if scored.get("counterweight"):
            add('- Counterweight: "%s"' % scored["counterweight"])
        add("")
    if minors:
        add("Smaller session-only episodes (no linked commits, so Execution "
            "Leverage and Engineering Quality are omitted):")
        add("")
        for ep in minors:
            eid = ep.get("episode_id")
            scored = ctx["scores_by_eid"].get(eid) or {}
            title = scored.get("title") or "Episode %s" % eid
            scores = scored.get("scores") or {}
            scored_axes = ["%s %s" % (AXIS_NAMES[a], fmt_score(scores[a]))
                           for a in AXES if isinstance(scores.get(a),
                                                       (int, float))]
            detail = ("; ".join(scored_axes) + " (confidence %.2f)"
                      % float(scored.get("confidence") or 0)
                      if scored_axes else "not scored")
            add("- **%s** — %s" % (title, detail))
        add("")

    # --- 5. Your numbers ---
    add("## Your numbers")
    add("")
    totals = {}
    model_sessions = {}
    session_costs = []
    for s in ctx["sessions"]:
        tu = s.get("token_usage") or {}
        cost, unknown = 0.0, False
        for model, u in sorted(tu.items()):
            t = totals.setdefault(model, {
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0})
            for k in t:
                t[k] += int(u.get(k) or 0)
            model_sessions[model] = model_sessions.get(model, 0) + 1
            c = usage_cost(u, model)
            if c is None:
                unknown = True
            else:
                cost += c
        if tu and not unknown:
            session_costs.append((cost, str(s.get("session_id"))))
    if totals:
        add("Totals by model (token counts are real provider numbers from "
            "the transcripts; costs are estimated at list prices as of %s, "
            "no volume discounts):" % PRICING_AS_OF)
        add("")
        add("| Model | Sessions | Input | Output | Cache read / write | "
            "Cache-read share of input | Est. cost |")
        add("|---|---|---|---|---|---|---|")
        grand = {"input_tokens": 0, "output_tokens": 0,
                 "cache_read_tokens": 0, "cache_write_tokens": 0}
        grand_cost, grand_unknown = 0.0, False
        for model in sorted(totals):
            t = totals[model]
            for k in grand:
                grand[k] += t[k]
            c = usage_cost(t, model)
            if c is None:
                grand_unknown = True
            else:
                grand_cost += c
            denom = (t["input_tokens"] + t["cache_read_tokens"]
                     + t["cache_write_tokens"])
            add("| %s | %d | %s | %s | %s / %s | %d%% | %s |"
                % (model, model_sessions[model], fmt_tok(t["input_tokens"]),
                   fmt_tok(t["output_tokens"]),
                   fmt_tok(t["cache_read_tokens"]),
                   fmt_tok(t["cache_write_tokens"]),
                   pct(t["cache_read_tokens"], denom),
                   "—" if c is None else fmt_cost(c)))
        usage_sessions = sum(1 for s in ctx["sessions"]
                             if s.get("token_usage"))
        add("| **Total** | %d | %s | %s | %s / %s | | %s |"
            % (usage_sessions, fmt_tok(grand["input_tokens"]),
               fmt_tok(grand["output_tokens"]),
               fmt_tok(grand["cache_read_tokens"]),
               fmt_tok(grand["cache_write_tokens"]),
               ("%s%s" % (fmt_cost(grand_cost),
                          " + unpriced models" if grand_unknown else ""))))
        add("")
        if session_costs:
            session_costs.sort(key=lambda sc: (-sc[0], sc[1]))
            add("Most expensive sessions:")
            add("")
            for cost, sid in session_costs[:3]:
                sg = next((s.get("session_signals") or {}
                           for s in ctx["sessions"]
                           if str(s.get("session_id")) == sid), {})
                add("- `%s` — %s (%s minutes active, %s)"
                    % (sid[:14], fmt_cost(cost),
                       "%g" % float(sg.get("duration_minutes") or 0),
                       ctx["intents"].get(sid) or "intent not stated"))
            add("")
    else:
        add("No token usage recorded in any session (Codex sessions carry "
            "no usage data; only Claude Code transcripts do).")
        add("")

    calls = ctx["llm_calls"]
    if calls is None:
        add("LLM-call ledger not provided (run report.py with --llm-calls "
            "llm_calls.json to include what this assessment itself cost).")
        add("")
    elif not calls:
        add("LLM-call ledger is empty — no calls recorded.")
        add("")
    else:
        rolled = {}
        total_tok = 0
        for c in calls:
            tok = c.get("total_tokens")
            tok = int(tok) if isinstance(tok, (int, float)) else 0
            total_tok += tok
            r = rolled.setdefault((str(c.get("stage")),
                                   str(c.get("model"))), [0, 0])
            r[0] += 1
            r[1] += tok
        add("This assessment itself cost %d LLM calls and %s tokens:"
            % (len(calls), fmt_tok(total_tok)))
        add("")
        add("| Stage | Model | Calls | Total tokens | Est. cost "
            "(input-rate lower bound) |")
        add("|---|---|---|---|---|")
        for (stage, model) in sorted(rolled):
            n_calls, toks = rolled[(stage, model)]
            p = price_for(model)
            cost = ("at least %s" % fmt_cost(toks * p[0] / 1_000_000.0)
                    if p else "—")
            add("| %s | %s | %d | %s | %s |" % (stage, model, n_calls,
                                                fmt_int(toks), cost))
        add("")
        add("Deterministic scripts cost zero LLM tokens. Token counts are "
            "as reported by the harness per dispatched call; the harness "
            "reports only totals, so cost is an input-rate lower bound.")
        add("")

    # --- 6. Highlights ---
    add("## Highlights")
    add("")
    add("Local reconstruction in Paxel's card format — the card generator "
        "itself is server-side and not portable.")
    add("")
    for c in build_cards(ctx, st):
        add("- **%s** — %s" % (c["headline"], c["caption"]))
    add("")

    # --- 7. Fine print ---
    add("## Fine print")
    add("")
    add("**Faithful — act on these:** per-axis reads, the rubric text "
        "quoted above, band cut thresholds (WEAK<4, LIMITED<6, STRONG<8, "
        "ELITE<9, EXEMPLAR>=9), and every deterministic count in this "
        "report.")
    add("")
    add("**Approximate — do not over-read:** the overall score and band "
        "(YC's rollup is server-side and unknown); the scorer model (local "
        "runs use Claude Haiku 4.5, Paxel's production scorer is "
        "gpt-5.5-none per reference/GAPS.md); LLM scoring varies run to "
        "run; local deviations (no decision-exchange chains, PR numbers "
        "only from in-session evidence, session-level subagent commit "
        "attribution, no Code Reviews section) are registered in SKILL.md "
        "and reference/GAPS.md. The Highlights cards are a local "
        "reconstruction, not a port.")
    add("")
    add('Calibration, quoted from `prompts/episode_scoring.md`: "%s" "%s"'
        % (cal[0], cal[1]))
    add("")
    if ctx["skipped_session_ids"]:
        add("Skipped as too short: %s."
            % ", ".join(s[:14] for s in ctx["skipped_session_ids"]))
        add("")
    if ctx["unmatched_scoring"]:
        add("%d unmatched scoring result(s) could not be joined to a known "
            "episode and were excluded: %s."
            % (len(ctx["unmatched_scoring"]),
               ", ".join(str(x) for x in ctx["unmatched_scoring"])))
        add("")
    prov = ", ".join("%s (%d %s)" % (
        name, count,
        {"condensed.jsonl": "sessions", "sessions.jsonl": "sessions",
         "gitdata.json": "episodes", "decisions.json": "decisions",
         "episodes.json": "scoring results",
         "narratives/": "narratives"}[name])
        for name, count in sorted(ctx["input_paths"].items()))
    add("Provenance: rendered from %s." % prov)
    add("")

    # --- 8. Appendix ---
    add("## Appendix: per-session detail")
    add("")
    if ctx["sessions"]:
        add("| Session | Date | Active min | Primary model | Turns | Input "
            "| Output | Cache r/w | Est. cost | Commits | Tests | Intent | "
            "Episode |")
        add("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for s in ctx["sessions"]:
            sid = str(s.get("session_id"))
            sg = s.get("session_signals") or {}
            model, turns, tin, tout, cache, cost = _session_cost_cells(s)
            add("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %d | %d | "
                "%s | %s |"
                % (sid[:14], ts_date(s.get("session_created_at")) or "-",
                   "%g" % float(sg.get("duration_minutes") or 0),
                   model, turns, tin, tout, cache, cost,
                   int(sg.get("git_commit_count") or 0),
                   int(sg.get("test_run_count") or 0),
                   ctx["intents"].get(sid) or "not stated",
                   ctx["episode_of"].get(sid, "-")))
        add("")
    else:
        add("No sessions — no data.")
        add("")
    return "\n".join(L)


# --- CLI ---

def main(argv=None):
    args = parse_args(argv)
    ctx = build_context(args)
    st = compute_stats(ctx)
    md = render_markdown(ctx, st)
    if not ctx["sessions"] and not ctx["episodes"]:
        md = md.replace(
            "# Paxel Self-Assessment — unknown repo",
            "# Paxel Self-Assessment — unknown repo\n\nNo data: no "
            "sessions or episodes were found in the provided inputs.", 1)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()

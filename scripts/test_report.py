"""Tests for report.py — deterministic end-of-run report renderer."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report  # noqa: E402


def session(sid, **over):
    rec = {
        "session_id": sid, "path": "/t/%s.jsonl" % sid,
        "first_prompt": "build the thing",
        "session_created_at": "2026-06-01T10:00:00+05:30",
        "session_modified_at": "2026-06-01T11:00:00+05:30",
        "events": [], "user_highlights": None, "plan_files": [],
        "active_time_windows": [], "pr_number": None, "git_branch": None,
        "event_git_shas": [], "event_branches": [],
        "dispatch_metadata": {"dispatch_count": 0, "return_count": 0,
                              "run_in_background_count": 0,
                              "unique_subagent_ids": []},
        "session_signals": {
            "duration_minutes": 45.0, "git_commit_count": 2,
            "test_run_count": 1, "plan_mode_used": False,
            "courtesy_messages": 2, "terse_messages": 1,
            "user_message_count": 10,
            "prompt_types": {"correction": 2, "directive": 8},
            "repeated_prompts": [
                {"norm": "make it prettier", "count": 3,
                 "example": "make it prettier"}],
        },
        "model_usage": {"claude-fable-5": 8},
        "token_usage": {"claude-fable-5": {
            "input_tokens": 50_000, "output_tokens": 9_000,
            "cache_read_tokens": 400_000, "cache_write_tokens": 30_000,
            "assistant_turns": 8}},
        "prompt_stats": {"median_words": 6.0, "short_prompt_count": 7,
                         "caps_quotes": [{"text": "DONT TOUCH THAT FILE",
                                          "caps_ratio": 1.0}]},
    }
    rec.update(over)
    return rec


def dispatch_events(pairs):
    """pairs: [(tool_use_id, start_ts, end_ts)] -> dispatch+return events."""
    evs = []
    for tid, start, end in pairs:
        evs.append({"type": "subagent_dispatch", "timestamp": start,
                    "tool_use_id": tid})
        evs.append({"type": "subagent_return", "timestamp": end,
                    "tool_use_id": tid})
    return evs


GITDATA = {
    "commit_groups": [
        {"id": 1, "group_type": "pr", "commit_shas": ["a" * 40, "b" * 40],
         "title": "PR #42: ship feature", "branch": "feat/x", "pr_number": 42,
         "insertions": 900, "deletions": 100,
         "earliest_commit_at": "2026-06-01T22:30:00+05:30",
         "latest_commit_at": "2026-06-01T23:30:00+05:30"},
    ],
    "episodes": [
        {"episode_id": 1, "episode_type": "feature", "confidence": 1.0,
         "session_ids": ["s1"], "commit_group_ids": [1],
         "added_lines": 900, "deleted_lines": 100,
         "links": [{"session_id": "s1", "link_type": "pr_match",
                    "link_confidence": 1.0}]},
        {"episode_id": 2, "episode_type": "session_only", "confidence": 0.3,
         "session_ids": ["s2"], "commit_group_ids": [],
         "added_lines": 0, "deleted_lines": 0,
         "links": [{"session_id": "s2", "link_type": None,
                    "link_confidence": 0.3}]},
    ],
    "commits": [
        {"sha": "a" * 40, "date": "2026-06-01T22:30:00+05:30"},  # Monday 22h
        {"sha": "b" * 40, "date": "2026-06-01T23:30:00+05:30"},
        {"sha": "c" * 40, "date": "2026-06-02T23:00:00+05:30"},
        {"sha": "d" * 40, "date": "2026-06-02T23:10:00+05:30"},
        {"sha": "e" * 40, "date": "2026-06-03T10:00:00+05:30"},
    ],
}

SCORED = [
    {"episode_id": 1, "title": "Shipped feature X",
     "facts": "f", "interpretation": "i",
     "counterweight": "No tests accompanied the change.",
     "confidence": 0.8,
     "scores": {"execution_leverage": 7.0, "steering": 6.5,
                "engineering_quality": 5.0, "product_thinking": 6.0,
                "planning": 6.0}},
    {"episode_id": 2, "title": "Explored architecture",
     "facts": "f", "interpretation": "i",
     "counterweight": "No code shipped.",
     "confidence": 0.6,
     "scores": {"steering": 7.0, "product_thinking": 6.5, "planning": 7.5}},
    {"episode_id": 999, "title": "orphan", "counterweight": "x",
     "confidence": 0.5, "scores": {"steering": 5.0}},
]

CONDENSED = [
    {"session_id": "s1", "cwd": "/Volumes/Code/myproj", "too_short": False,
     "agent_type": "claude_code"},
    {"session_id": "s2", "cwd": "/Volumes/Code/myproj", "too_short": False,
     "agent_type": "claude_code"},
    {"session_id": "s3", "cwd": "/Volumes/Code/myproj", "too_short": True,
     "agent_type": "claude_code"},
]


def write_fixture(td, sessions, gitdata=GITDATA, scored=SCORED,
                  condensed=CONDENSED, decisions=None, narratives=None,
                  llm_calls=None):
    paths = {}
    paths["condensed"] = os.path.join(td, "condensed.jsonl")
    with open(paths["condensed"], "w", encoding="utf-8") as f:
        for r in condensed:
            f.write(json.dumps(r) + "\n")
    paths["sessions"] = os.path.join(td, "sessions.jsonl")
    with open(paths["sessions"], "w", encoding="utf-8") as f:
        for r in sessions:
            f.write(json.dumps(r) + "\n")
    for name, obj in [("gitdata", gitdata), ("decisions", decisions or []),
                      ("episodes", scored)]:
        paths[name] = os.path.join(td, name + ".json")
        with open(paths[name], "w", encoding="utf-8") as f:
            json.dump(obj, f)
    paths["narratives"] = os.path.join(td, "narratives")
    os.makedirs(paths["narratives"])
    for sid, text in (narratives or {}).items():
        with open(os.path.join(paths["narratives"], sid + ".md"), "w",
                  encoding="utf-8") as f:
            f.write(text)
    if llm_calls is not None:
        paths["llm_calls"] = os.path.join(td, "llm_calls.json")
        with open(paths["llm_calls"], "w", encoding="utf-8") as f:
            json.dump(llm_calls, f)
    else:
        paths["llm_calls"] = None
    return paths


def build(td, sessions, **kw):
    paths = write_fixture(td, sessions, **kw)
    args = report.parse_args([
        "--condensed", paths["condensed"], "--sessions", paths["sessions"],
        "--gitdata", paths["gitdata"], "--decisions", paths["decisions"],
        "--episodes", paths["episodes"], "--narratives", paths["narratives"]]
        + (["--llm-calls", paths["llm_calls"]] if paths["llm_calls"] else [])
        + ["--out", os.path.join(td, "report.md")])
    return report.build_context(args), args


class RubricTest(unittest.TestCase):
    def test_anchors_parsed_verbatim(self):
        anchors = report.load_anchors(report.default_prompt_path())
        self.assertIn(
            "most dispatched subagents return usable commits",
            anchors["execution_leverage"]["9-10"])
        # Body paragraphs only — never the header line with abbreviations
        for axis in anchors:
            for text in anchors[axis].values():
                self.assertNotIn("(EL)", text)
                self.assertNotIn("(EQ)", text)

    def test_law_titles_from_real_catalog(self):
        titles = report.load_law_titles(report.default_catalog_path())
        self.assertEqual(titles.get("audit-completeness"),
                         "Audit Completeness")
        self.assertEqual(titles.get("cache-before-api"), "Cache Before API")

    def test_calibration_lines(self):
        cal = report.load_calibration(report.default_prompt_path())
        self.assertTrue(cal[0].startswith("7 is the MEDIAN competent operator"))
        # The sentence wraps across two physical lines in the prompt file;
        # the loader must unwrap it, not truncate at the line break.
        self.assertTrue(cal[0].endswith("not a high one."), cal[0])
        self.assertEqual(cal[1], "Most episodes land 5-8.")


class JoinTest(unittest.TestCase):
    def test_episode_join_and_unmatched(self):
        with tempfile.TemporaryDirectory() as td:
            ctx, _ = build(td, [session("s1"), session("s2")])
            self.assertEqual(sorted(ctx["scores_by_eid"]), [1, 2])
            self.assertEqual(ctx["unmatched_scoring"], [999])

    def test_repo_name_and_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            ctx, _ = build(td, [session("s1"), session("s2")])
            self.assertEqual(ctx["repo_name"], "myproj")
            self.assertEqual(ctx["skipped_session_ids"], ["s3"])


class StatsTest(unittest.TestCase):
    def test_parallel_and_longest_run(self):
        evs = dispatch_events([
            ("t1", "2026-06-01T10:00:00+05:30", "2026-06-01T10:30:00+05:30"),
            ("t2", "2026-06-01T10:10:00+05:30", "2026-06-01T10:20:00+05:30"),
            ("t3", "2026-06-01T11:00:00+05:30", "2026-06-01T11:05:00+05:30"),
        ])
        with tempfile.TemporaryDirectory() as td:
            ctx, _ = build(td, [session("s1", events=evs), session("s2")])
            stats = report.compute_stats(ctx)
            self.assertEqual(stats["max_parallel"], 2)
            self.assertEqual(stats["longest_run_seconds"], 1800)
            self.assertEqual(stats["dispatching_sessions"], 1)

    def test_mixed_naive_and_aware_timestamps_do_not_crash(self):
        evs = dispatch_events([
            ("t1", "2026-06-01T10:00:00", "2026-06-01T10:30:00Z"),
        ])
        evs2 = dispatch_events([
            ("t2", "2026-06-01T10:05:00+05:30", "2026-06-01T10:15:00+05:30"),
        ])
        with tempfile.TemporaryDirectory() as td:
            ctx, _ = build(td, [session("s1", events=evs),
                                session("s2", events=evs2)])
            stats = report.compute_stats(ctx)  # must not raise
            self.assertEqual(stats["dispatching_sessions"], 2)
            self.assertEqual(stats["longest_run_seconds"], 1800)

    def test_committed_after_return(self):
        evs = dispatch_events(
            [("t1", "2026-06-01T10:00:00Z", "2026-06-01T10:30:00Z")])
        evs.append({"type": "git_commit", "timestamp": "2026-06-01T10:40:00Z"})
        with tempfile.TemporaryDirectory() as td:
            ctx, _ = build(td, [session("s1", events=evs)])
            stats = report.compute_stats(ctx)
            self.assertEqual(stats["committed_after_return_sessions"], 1)

    def test_peak_hours_and_ship_day(self):
        with tempfile.TemporaryDirectory() as td:
            ctx, _ = build(td, [session("s1")])
            stats = report.compute_stats(ctx)
            # Commit hours 22, 23, 23, 23, 10: windows starting 20, 21, 22
            # all hold 4 commits; ties resolve to the lowest start hour.
            self.assertEqual(stats["peak_window"], (20, 0))
            self.assertEqual(stats["peak_window_count"], 4)
            # Mon 2, Tue 2, Wed 1 -> count tie resolves alphabetical-first
            self.assertEqual(stats["ship_day"], ("Monday", 2))

    def test_archetype_first_match(self):
        with tempfile.TemporaryDirectory() as td:
            s1 = session("s1", plan_files=[{"path": "p.md"}])
            s2 = session("s2", plan_files=[{"path": "q.md"}])
            ctx, _ = build(td, [s1, s2])
            stats = report.compute_stats(ctx)
            self.assertEqual(stats["archetype"][0], "The Architect")


class CardsTest(unittest.TestCase):
    def _cards(self, sessions, **kw):
        with tempfile.TemporaryDirectory() as td:
            ctx, _ = build(td, sessions, **kw)
            return {c["key"]: c for c in
                    report.build_cards(ctx, report.compute_stats(ctx))}

    def test_archetype_always_present(self):
        cards = self._cards([session("s1")])
        self.assertIn("archetype", cards)

    def test_floors_omit_cards(self):
        # 1 session, no dispatches, < 5 commits, < 3 sessions, < 10 prompts
        bare = session("s1", session_signals={
            "duration_minutes": 5.0, "git_commit_count": 0,
            "test_run_count": 0, "plan_mode_used": False,
            "courtesy_messages": 0, "terse_messages": 0,
            "user_message_count": 4, "prompt_types": {},
            "repeated_prompts": []})
        bare["prompt_stats"] = {"median_words": 4.0, "short_prompt_count": 2,
                                "caps_quotes": []}
        gd = {"commit_groups": [], "episodes": [], "commits": []}
        cards = self._cards([bare], gitdata=gd, scored=[])
        for key in ["peak_hours", "planning_share", "goto_prompt",
                    "parallel", "brevity", "politeness", "steering",
                    "longest_run", "crash_out", "lines", "ship_day"]:
            self.assertNotIn(key, cards, key)

    def test_lines_card_counts(self):
        cards = self._cards([session("s1"), session("s2"), session("s3")])
        self.assertIn("lines", cards)
        self.assertIn("900", cards["lines"]["headline"])
        self.assertIn("100", cards["lines"]["caption"])

    def test_every_card_caption_contains_a_digit(self):
        cards = self._cards([session("s1"), session("s2"), session("s3")])
        for key, c in cards.items():
            self.assertTrue(any(ch.isdigit() for ch in c["caption"]),
                            "caption without evidence: %s" % key)


class MarkdownTest(unittest.TestCase):
    def _md(self, sessions, **kw):
        with tempfile.TemporaryDirectory() as td:
            ctx, _ = build(td, sessions, **kw)
            return report.render_markdown(ctx, report.compute_stats(ctx))

    def base_sessions(self):
        return [session("s1"), session("s2")]

    def test_section_order_and_headers(self):
        md = self._md(self.base_sessions())
        order = ["# Paxel Self-Assessment", "## Verdict",
                 "## Why these scores", "## How to improve", "## The work",
                 "## Your numbers", "## Highlights", "## Fine print",
                 "## Appendix: per-session detail"]
        idx = [md.index(h) for h in order]
        self.assertEqual(idx, sorted(idx))

    def test_no_bare_axis_abbreviations(self):
        md = self._md(self.base_sessions())
        for tok in ["EL", "EQ", "PT"]:
            self.assertNotRegex(md, r"\b%s\b" % tok)

    def test_calibration_quote_verbatim_hyphen(self):
        md = self._md(self.base_sessions())
        self.assertIn("Most episodes land 5-8.", md)
        self.assertNotIn("5–8", md)

    def test_axis_gap_table_and_anchor_quote(self):
        md = self._md(self.base_sessions())
        self.assertIn("Engineering Quality", md)   # lowest axis, full name
        self.assertIn("To next band", md)
        self.assertIn("The axis holding you back", md)
        self.assertIn("> ", md)                    # block-quoted anchor

    def test_session_only_warning_with_counts(self):
        md = self._md(self.base_sessions())
        # episode 2 of 2 omits Execution Leverage + Engineering Quality
        self.assertIn("1 of 2 episodes (50%)", md)

    def test_test_run_warning_absent_when_tests_ran(self):
        md = self._md(self.base_sessions())
        self.assertNotIn("0 test runs detected", md)

    def test_unmatched_scoring_line(self):
        md = self._md(self.base_sessions())
        self.assertIn("unmatched scoring result", md)
        self.assertIn("999", md)

    def test_episode_block_plain_english_links(self):
        md = self._md(self.base_sessions())
        self.assertIn("linked by PR #42", md)
        self.assertIn("session-only episodes", md)
        self.assertIn("Execution Leverage and Engineering Quality are "
                      "omitted", md)

    def test_inventory_costs_and_totals(self):
        md = self._md(self.base_sessions())
        self.assertIn("claude-fable-5", md)
        self.assertIn("estimated", md)
        self.assertIn("Totals by model", md)

    def test_unknown_model_cost_dash(self):
        s = session("s1")
        s["model_usage"] = {"gpt-5.5-none": 4}
        s["token_usage"] = {"gpt-5.5-none": {
            "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "assistant_turns": 4}}
        md = self._md([s, session("s2")])
        self.assertIn("gpt-5.5-none", md)
        self.assertIn("—", md)

    def test_ledger_absent_renders_fixed_line(self):
        md = self._md(self.base_sessions())
        self.assertIn("ledger not provided", md)

    def test_ledger_rendered(self):
        calls = [{"stage": "episode-score", "target": "1",
                  "model": "claude-haiku-4-5", "total_tokens": 40_000,
                  "duration_s": 30}]
        md = self._md(self.base_sessions(), llm_calls=calls)
        self.assertIn("episode-score", md)
        self.assertIn("40,000", md)
        self.assertIn("lower bound", md)

    def test_decisions_section(self):
        decisions = [
            {"session_id": "s1", "decision_type": "technical_catch",
             "law_key": None, "classification_confidence": "high",
             "decision_narrative": "Caught a race condition before merge",
             "event_index": 4},
            {"session_id": "s1", "decision_type": None, "law_key": None,
             "classification_confidence": None,
             "decision_narrative": None, "event_index": 9},
        ]
        md = self._md(self.base_sessions(), decisions=decisions)
        self.assertIn("Technical catch", md)
        self.assertIn("Unclassified (regex fallback)", md)
        self.assertIn("Caught a race condition before merge", md)

    def test_decision_law_title_rendered(self):
        decisions = [
            {"session_id": "s1", "decision_type": "technical_catch",
             "law_key": "audit-completeness",
             "classification_confidence": "high",
             "decision_narrative": "Demanded a sweep for other broken links",
             "event_index": 4},
            {"session_id": "s1", "decision_type": "technical_catch",
             "law_key": None, "classification_confidence": "medium",
             "decision_narrative": "Caught a race condition before merge",
             "event_index": 9},
        ]
        md = self._md(self.base_sessions(), decisions=decisions)
        self.assertIn("Demanded a sweep for other broken links", md)
        self.assertIn(" — law: Audit Completeness", md)
        line = next(l for l in md.splitlines()
                    if "Caught a race condition before merge" in l)
        self.assertNotIn(" — law:", line)

    def test_primary_model_from_model_usage_alone(self):
        s = session("s1")
        s["token_usage"] = {}
        md = self._md([s, session("s2")])
        line = next(l for l in md.splitlines()
                    if l.startswith("| s1 |"))
        self.assertIn("claude-fable-5", line)
        self.assertIn("not recorded", line)
        self.assertIn("—", line)

    def test_cost_table_arithmetic(self):
        # Each fixture session: 50,000 in / 9,000 out / 400,000 cache-read /
        # 30,000 cache-write on claude-fable-5 at $10/$50/$1/$12.50 per MTok
        # = $0.50 + $0.45 + $0.40 + $0.375 = $1.725 -> $1.73 per session;
        # two sessions -> $3.45 in the totals row.
        md = self._md(self.base_sessions())
        self.assertIn("$1.73", md)
        self.assertIn("$3.45", md)

    def test_missing_usage_row_and_totals_session_count(self):
        s2 = session("s2")
        s2["token_usage"] = {}
        s2["model_usage"] = {}
        md = self._md([session("s1"), s2])
        line = next(l for l in md.splitlines()
                    if l.startswith("| s2 |"))
        self.assertIn("not recorded", line)
        total = next(l for l in md.splitlines()
                     if l.startswith("| **Total** |"))
        self.assertIn("| **Total** | 1 |", total)

    def test_intent_from_narrative(self):
        narr = {"s1": "## Note\nstuff\n<session_intent>shipping"
                      "</session_intent>\n"}
        md = self._md(self.base_sessions(), narratives=narr)
        self.assertIn("shipping", md)
        self.assertIn("not stated", md)  # s2 has no narrative

    def test_provenance_footer(self):
        md = self._md(self.base_sessions())
        self.assertIn("rendered from", md)
        self.assertIn("sessions.jsonl (2 sessions)", md)

    def test_llm_text_with_pipes_renders_outside_tables(self):
        # Scorer-authored text (titles, counterweights, interpretations)
        # lands in bullets and headings, never table cells, so literal
        # pipes must survive unescaped without corrupting any table.
        scored = [dict(SCORED[0],
                       counterweight="Bad | broken | row",
                       title="Title | with pipes")]
        with tempfile.TemporaryDirectory() as td:
            ctx, _ = build(td, [session("s1"), session("s2")],
                           scored=scored)
            md = report.render_markdown(ctx, report.compute_stats(ctx))
        self.assertIn("Title | with pipes", md)
        self.assertIn("Bad | broken | row", md)

    def test_verdict_and_why_sections(self):
        md = self._md(self.base_sessions())
        self.assertIn("## Verdict", md)
        self.assertIn("The axis holding you back is Engineering Quality", md)
        # The scorer's interpretation field renders in Why these scores.
        self.assertIn('(confidence 0.80) — "i"', md)

    def test_harness_markers_excluded_from_goto_prompt(self):
        s1 = session("s1")
        s1["session_signals"]["repeated_prompts"] = [
            {"norm": "request interrupted by user", "count": 9,
             "example": "[Request interrupted by user]"}]
        with tempfile.TemporaryDirectory() as td:
            ctx, _ = build(td, [s1, session("s2"), session("s3")])
            stats = report.compute_stats(ctx)
        self.assertNotIn("interrupted", str(stats["goto_prompt"] or ""))


class MainTest(unittest.TestCase):
    def _run_main(self, td, sessions, **kw):
        paths = write_fixture(td, sessions, **kw)
        argv = ["--condensed", paths["condensed"],
                "--sessions", paths["sessions"],
                "--gitdata", paths["gitdata"],
                "--decisions", paths["decisions"],
                "--episodes", paths["episodes"],
                "--narratives", paths["narratives"],
                "--out", os.path.join(td, "report.md")]
        report.main(argv)
        with open(os.path.join(td, "report.md"), "rb") as f:
            return f.read()

    def test_no_html_artifact_written(self):
        # Console-only deliverable: main() must write report.md and nothing
        # else into the output directory.
        with tempfile.TemporaryDirectory() as td:
            self._run_main(td, [session("s1"), session("s2")])
            written = sorted(os.listdir(td))
            self.assertNotIn("cards.html", written)
            self.assertIn("report.md", written)

    def test_byte_determinism_across_runs(self):
        # Fixture timestamps span two different UTC offsets (+05:30, -07:00
        # via gitdata commits) to pin the as-recorded rendering policy.
        sessions = [session("s1"), session(
            "s2", session_created_at="2026-06-01T08:00:00-07:00",
            session_modified_at="2026-06-01T09:00:00-07:00")]
        with tempfile.TemporaryDirectory() as td:
            md1 = self._run_main(td, sessions)
        with tempfile.TemporaryDirectory() as td:
            md2 = self._run_main(td, sessions)
        self.assertEqual(md1, md2)

    def test_main_degrades_on_missing_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            argv = ["--condensed", os.path.join(td, "nope.jsonl"),
                    "--sessions", os.path.join(td, "nope2.jsonl"),
                    "--gitdata", os.path.join(td, "nope.json"),
                    "--decisions", os.path.join(td, "nope3.json"),
                    "--episodes", os.path.join(td, "nope4.json"),
                    "--narratives", os.path.join(td, "missing-dir"),
                    "--out", os.path.join(td, "report.md")]
            report.main(argv)  # must not raise
            md = open(os.path.join(td, "report.md"),
                      encoding="utf-8").read()
            self.assertIn("no data", md.lower())


if __name__ == "__main__":
    unittest.main()


class CommitOnlyEpisodeLabelTest(unittest.TestCase):
    def test_commit_only_episode_not_labeled_session_only(self):
        gd = json.loads(json.dumps(GITDATA))
        gd["episodes"].append(
            {"episode_id": 3, "episode_type": "implementation",
             "confidence": 0.5, "session_ids": [], "commit_group_ids": [1],
             "added_lines": 10, "deleted_lines": 1, "links": []})
        with tempfile.TemporaryDirectory() as td:
            ctx, _ = build(td, [session("s1"), session("s2")], gitdata=gd)
            md = report.render_markdown(ctx, report.compute_stats(ctx))
        self.assertIn("commits only, no linked sessions", md)

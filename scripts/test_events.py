#!/usr/bin/env python3
"""
Tests for events.py — typed event + signal extraction.

Stdlib only (unittest), matching test_scripts.py conventions. Run from anywhere:

    python3 scripts/test_events.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import events  # noqa: E402


def write_session(lines):
    """Write JSONL entries to a temp .jsonl and return its path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
    return path


def extract(lines):
    path = write_session(lines)
    try:
        return events.extract_session(path)
    finally:
        os.remove(path)


def user(text, ts=None, **extra):
    e = {"type": "user", "message": {"role": "user", "content": text}}
    if ts:
        e["timestamp"] = ts
    e.update(extra)
    return e


def assistant(content, ts=None):
    e = {"type": "assistant", "message": {"role": "assistant", "content": content}}
    if ts:
        e["timestamp"] = ts
    return e


def tool_result(content, ts=None, **extra):
    block = {"type": "tool_result", "content": content}
    block.update(extra)
    e = {"type": "user", "message": {"role": "user", "content": [block]}}
    if ts:
        e["timestamp"] = ts
    return e


def events_of(out, etype):
    return [e for e in out["events"] if e["type"] == etype]


class GitCommitTests(unittest.TestCase):
    def _session(self):
        return extract([
            user("Please commit the change", ts="2026-01-01T12:00:00Z"),
            assistant([{"type": "tool_use", "name": "Bash",
                        "input": {"command": 'git commit -m "fix: resolve retry bug"'}}],
                      ts="2026-01-01T12:01:00Z"),
            tool_result("[main abc1234] fix: resolve retry bug\n 1 file changed",
                        ts="2026-01-01T12:01:05Z"),
        ])

    def test_commit_message_and_sha_resolved_from_following_tool_result(self):
        out = self._session()
        commits = events_of(out, "git_commit")
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["message"], "fix: resolve retry bug")
        self.assertEqual(commits[0]["sha"], "abc1234")
        self.assertEqual(out["event_git_shas"], ["abc1234"])

    def test_git_command_is_not_a_bash_command_event(self):
        # event_extractor.rb:121 — commands containing "git " skip bash_command
        out = self._session()
        self.assertEqual(events_of(out, "bash_command"), [])

    def test_branch_detected_from_commit_output(self):
        out = self._session()
        self.assertEqual(out["git_branch"], "main")

    def test_sha_not_resolved_without_pending_commit(self):
        # A commit-shaped tool_result with no prior `git commit` resolves nothing.
        out = extract([
            tool_result("[main abc1234] someone else's commit"),
        ])
        self.assertEqual(events_of(out, "git_commit"), [])
        self.assertEqual(out["event_git_shas"], [])

    def test_commit_message_variants(self):
        ex = events.EventExtractor()
        ex.extract_git_from_command("git commit -m 'single quoted'", None)
        ex.extract_git_from_command("git commit -F - <<'EOF'\nfeat: heredoc msg\nEOF", None)
        ex.extract_git_from_command("git commit", None)
        msgs = [e["message"] for e in ex.events if e["type"] == "git_commit"]
        self.assertEqual(msgs, ["single quoted", "feat: heredoc msg", "[interactive commit]"])

    def test_push_and_branch_switch(self):
        ex = events.EventExtractor()
        ex.extract_git_from_command("git checkout -b feat/x", None)
        ex.extract_git_from_command("git push origin feat/x", None)
        ex.extract_git_from_command("git checkout -- somefile.py", None)  # not a branch
        switches = [e for e in ex.events if e["type"] == "git_branch_switch"]
        self.assertEqual([e["branch"] for e in switches], ["feat/x"])
        self.assertEqual(len([e for e in ex.events if e["type"] == "git_push"]), 1)
        self.assertEqual(ex.detected_branch, "feat/x")


class DispatchReturnTests(unittest.TestCase):
    def _session(self):
        return extract([
            assistant([{"type": "tool_use", "id": "toolu_01", "name": "Task",
                        "input": {"description": "Lint check on changed files",
                                  "prompt": "go lint everything",
                                  "subagent_type": "code-reviewer",
                                  "run_in_background": True}}],
                      ts="2026-01-01T10:00:00Z"),
            tool_result([{"type": "text", "text": "all clean"}],
                        ts="2026-01-01T10:05:00Z", tool_use_id="toolu_01"),
        ])

    def test_dispatch_event_fields(self):
        out = self._session()
        dispatches = events_of(out, "subagent_dispatch")
        self.assertEqual(len(dispatches), 1)
        d = dispatches[0]
        self.assertEqual(d["tool_use_id"], "toolu_01")
        self.assertEqual(d["subagent_id"], "code-reviewer")
        self.assertEqual(d["description"], "Lint check on changed files")
        self.assertEqual(len(d["prompt_hash"]), 12)
        self.assertTrue(d["run_in_background"])

    def test_return_pairs_with_dispatch_tool_use_id(self):
        out = self._session()
        returns = events_of(out, "subagent_return")
        self.assertEqual(len(returns), 1)
        r = returns[0]
        self.assertEqual(r["tool_use_id"], "toolu_01")
        self.assertEqual(r["return_text_length"], len("all clean"))
        self.assertFalse(r["indicates_error"])

    def test_unmatched_tool_result_emits_no_return(self):
        out = extract([
            tool_result("orphan result", tool_use_id="toolu_99"),
        ])
        self.assertEqual(events_of(out, "subagent_return"), [])

    def test_dispatch_metadata_rollup(self):
        out = self._session()
        self.assertEqual(out["dispatch_metadata"], {
            "dispatch_count": 1, "return_count": 1,
            "run_in_background_count": 1, "unique_subagent_ids": ["code-reviewer"]})

    def test_missing_subagent_type_defaults_general_purpose(self):
        out = extract([
            assistant([{"type": "tool_use", "id": "toolu_02", "name": "Task",
                        "input": {"description": "d", "prompt": "p"}}]),
        ])
        d = events_of(out, "subagent_dispatch")[0]
        self.assertEqual(d["subagent_id"], "general-purpose")
        self.assertFalse(d["run_in_background"])


class TestRunTests(unittest.TestCase):
    def test_pytest_passed_and_failed(self):
        out = extract([tool_result("==== 3 passed, 1 failed in 0.42s ====")])
        runs = events_of(out, "test_run")
        self.assertEqual(runs[0]["framework"], "pytest")
        self.assertEqual(runs[0]["passed"], 3)
        self.assertEqual(runs[0]["failed"], 1)

    def test_pytest_failed_defaults_zero(self):
        out = extract([tool_result("5 passed in 0.1s")])
        self.assertEqual(events_of(out, "test_run")[0]["failed"], 0)

    def test_jest_checked_before_pytest(self):
        # "Tests: 2 failed, 8 passed" also contains "8 passed" — jest must win.
        out = extract([tool_result("Tests:       2 failed, 8 passed, 10 total")])
        run = events_of(out, "test_run")[0]
        self.assertEqual(run["framework"], "jest")
        self.assertEqual(run["passed"], 8)
        self.assertEqual(run["failed"], 2)

    def test_rspec_passed_is_examples_minus_failures(self):
        out = extract([tool_result("10 examples, 2 failures, 1 pending")])
        run = events_of(out, "test_run")[0]
        self.assertEqual(run["framework"], "rspec")
        self.assertEqual(run["passed"], 8)
        self.assertEqual(run["failed"], 2)
        self.assertEqual(run["pending"], 1)

    def test_cargo(self):
        out = extract([tool_result("test result: ok. 12 passed; 0 failed; 0 ignored")])
        run = events_of(out, "test_run")[0]
        self.assertEqual(run["framework"], "cargo")
        self.assertEqual(run["passed"], 12)
        self.assertEqual(run["failed"], 0)


class ProposalAndThinkingTests(unittest.TestCase):
    def test_numbered_options_with_count(self):
        text = ("Here are 2 options:\n"
                "1. **Patch** the retry loop in place\n"
                "2. **Rewrite** the worker around a queue")
        out = extract([assistant([{"type": "text", "text": text}])])
        proposals = events_of(out, "agent_proposal")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["proposal_type"], "options")
        self.assertEqual(proposals[0]["option_count"], 2)

    def test_question_proposal(self):
        out = extract([assistant("Should we keep the queue or drop it entirely?")])
        p = events_of(out, "agent_proposal")[0]
        self.assertEqual(p["proposal_type"], "question")
        self.assertIsNone(p["option_count"])

    def test_plain_text_is_not_a_proposal(self):
        out = extract([assistant("I updated the file and the build is green.")])
        self.assertEqual(events_of(out, "agent_proposal"), [])

    def test_thinking_block_truncated_at_thinking_cap(self):
        out = extract([assistant([{"type": "thinking", "thinking": "x" * 25_000}])])
        t = events_of(out, "agent_thinking")[0]
        self.assertEqual(len(t["text"]), events.MAX_THINKING_LENGTH)
        self.assertTrue(t["text"].endswith("..."))


class PlanFileTests(unittest.TestCase):
    def test_plan_path_pattern_is_case_sensitive_on_basename(self):
        yes = ["/r/.claude/plans/feature.md", "/r/PLAN.md", "PLAN.md",
               "/r/IMPLEMENTATION_PLAN.md", "/r/CODEX_PLAN.md"]
        no = ["/r/plan.md", "/r/test_plan.md", "/r/PLAN.md.bak", "/r/myPLAN.md"]
        for p in yes:
            self.assertIsNotNone(events.PLAN_PATH_PATTERN.search(p), msg=p)
        for p in no:
            self.assertIsNone(events.PLAN_PATH_PATTERN.search(p), msg=p)

    def test_two_writes_version_and_edit_does_not_bump(self):
        plan_path = "/repo/.claude/plans/feature.md"
        out = extract([
            assistant([{"type": "tool_use", "name": "Write",
                        "input": {"file_path": plan_path,
                                  "content": "## Plan\nWe will verify with tests."}}]),
            assistant([{"type": "tool_use", "name": "Edit",
                        "input": {"file_path": plan_path,
                                  "old_string": "a", "new_string": "b"}}]),
            assistant([{"type": "tool_use", "name": "Write",
                        "input": {"file_path": plan_path,
                                  "content": "## Plan v2\nOption A vs option B. "
                                             "Handle edge cases with a fallback."}}]),
        ])
        plans = out["plan_files"]
        self.assertEqual([p["version"] for p in plans], [1, 2])
        self.assertEqual(plans[0]["filename"], "feature.md")
        self.assertEqual(plans[0]["full_path"], plan_path)
        # plan_patterns.rb booleans per version
        self.assertTrue(plans[0]["has_verification"])
        self.assertFalse(plans[0]["has_alternatives"])
        self.assertFalse(plans[0]["has_edge_cases"])
        self.assertFalse(plans[1]["has_verification"])
        self.assertTrue(plans[1]["has_alternatives"])
        self.assertTrue(plans[1]["has_edge_cases"])
        # file events still emitted: 2 creates + 1 edit
        self.assertEqual(len(events_of(out, "file_create")), 2)
        self.assertEqual(len(events_of(out, "file_edit")), 1)


class SignalsTests(unittest.TestCase):
    def _session(self):
        return extract([
            user("delete the old retry wrapper", ts="2026-01-01T12:00:00Z"),
            user("why is the build broken?", ts="2026-01-01T12:05:00Z"),
            user("thanks, looks good", ts="2026-01-01T12:06:00Z"),
            user("should i proceed with option 2?", ts="2026-01-01T12:07:00Z"),
        ])

    def test_pattern_counts_count_messages_not_occurrences(self):
        s = self._session()["session_signals"]
        self.assertEqual(s["kill_decisions"], 1)
        self.assertEqual(s["debugging_messages"], 1)
        self.assertEqual(s["courtesy_messages"], 1)
        self.assertEqual(s["confirmation_requests"], 1)
        self.assertEqual(s["imperative_prompts"], 1)

    def test_prompt_types(self):
        s = self._session()["session_signals"]
        self.assertEqual(s["prompt_types"],
                         {"directive": 1, "question": 2, "other": 1})

    def test_quantitative_keys(self):
        s = self._session()["session_signals"]
        self.assertEqual(s["user_message_count"], 4)
        self.assertEqual(s["terse_messages"], 3)
        self.assertEqual(s["substantive_messages"], 0)
        self.assertEqual(s["git_commit_count"], 0)
        self.assertFalse(s["plan_mode_used"])
        self.assertFalse(s["task_tool_used"])
        self.assertEqual(s["duration_minutes"], 7.0)

    def test_no_user_messages_means_empty_signals(self):
        # chunker.rb extract_and_save_signals early-returns when no raw user text
        out = extract([tool_result("just output")])
        self.assertEqual(out["session_signals"], {})

    def test_kill_message_counted_once_despite_two_kill_words(self):
        out = extract([user("delete and remove the shim")])
        self.assertEqual(out["session_signals"]["kill_decisions"], 1)

    def test_event_derived_signals(self):
        out = extract([
            user("run the tests"),
            tool_result("==== 4 passed, 1 failed ===="),
            assistant([{"type": "tool_use", "name": "Edit",
                        "input": {"file_path": "/r/a.py", "old_string": "a",
                                  "new_string": "b"}}]),
        ])
        s = out["session_signals"]
        self.assertEqual(s["test_run_count"], 1)
        self.assertEqual(s["files_modified_count"], 1)
        self.assertEqual(s["test_pass_rate"], 0.8)
        # test_run came before the first non-plan edit → test-first
        self.assertEqual(s["tdd_discipline_ratio"], 1.0)

    def test_repeated_and_charged_candidates(self):
        out = extract([
            user("fix it"),
            user("fix it"),
            user("wtf this is broken!!"),
        ])
        s = out["session_signals"]
        reps = {r["norm"]: r["count"] for r in s["repeated_prompts"]}
        self.assertEqual(reps["fix it"], 2)
        self.assertEqual(s["charged_messages"], ["wtf this is broken!!"])


class HighlightsTests(unittest.TestCase):
    LONG = ("Please refactor the upload pipeline so that every retry path logs "
            "its failure reason and surfaces metrics to the dashboard cleanly")

    def test_only_messages_over_15_words_qualify(self):
        out = extract([user(self.LONG), user("ok thanks")])
        self.assertEqual(out["user_highlights"], self.LONG)
        self.assertNotIn("---", out["user_highlights"])

    def test_multiple_highlights_joined_with_separator(self):
        out = extract([user(self.LONG), user(self.LONG + " again please")])
        self.assertEqual(out["user_highlights"].count("\n---\n"), 1)

    def test_no_highlights_is_null(self):
        out = extract([user("short one")])
        self.assertIsNone(out["user_highlights"])


class ActiveWindowsTests(unittest.TestCase):
    def test_gap_over_15_minutes_splits_windows(self):
        out = extract([
            user("a", ts="2026-01-01T12:00:00Z"),
            user("b", ts="2026-01-01T12:05:00Z"),
            user("c", ts="2026-01-01T13:00:00Z"),
        ])
        self.assertEqual(out["active_time_windows"], [
            ["2026-01-01T12:00:00Z", "2026-01-01T12:05:00Z"],
            ["2026-01-01T13:00:00Z", "2026-01-01T13:00:00Z"]])
        # ...while duration_minutes uses the 90-minute gap → both deltas active
        self.assertEqual(out["session_signals"]["duration_minutes"], 60.0)

    def test_session_boundary_timestamps(self):
        out = extract([
            user("a", ts="2026-01-01T12:00:00Z"),
            user("b", ts="2026-01-01T13:00:00Z"),
        ])
        self.assertEqual(out["session_created_at"], "2026-01-01T12:00:00Z")
        self.assertEqual(out["session_modified_at"], "2026-01-01T13:00:00Z")


class TruncationCapTests(unittest.TestCase):
    def test_bash_command_truncated_to_cap(self):
        out = extract([
            assistant([{"type": "tool_use", "name": "Bash",
                        "input": {"command": "echo " + "x" * 6000}}]),
        ])
        cmd = events_of(out, "bash_command")[0]["command"]
        self.assertEqual(len(cmd), events.MAX_BASH_LENGTH)
        self.assertTrue(cmd.endswith("..."))

    def test_max_events_drops_non_structural_but_keeps_structural(self):
        original = events.MAX_EVENTS
        events.MAX_EVENTS = 2
        try:
            ex = events.EventExtractor()
            for i in range(4):
                ex.extract_user_directive(f"directive {i}", None)
            ex.extract_git_from_command('git commit -m "still recorded"', None)
            types = [e["type"] for e in ex.events]
            self.assertEqual(types, ["user_directive", "user_directive", "git_commit"])
            self.assertTrue(ex.truncated)
        finally:
            events.MAX_EVENTS = original


class ErrorAndFilterTests(unittest.TestCase):
    def test_error_encountered_takes_first_matching_line(self):
        out = extract([tool_result(
            "Traceback (most recent call last):\nTypeError: cannot unpack\nmore")])
        errs = events_of(out, "error_encountered")
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["message"], "TypeError: cannot unpack")

    def test_system_reminder_user_content_is_filtered(self):
        out = extract([user("<system-reminder>noise</system-reminder>")])
        self.assertEqual(events_of(out, "user_directive"), [])
        self.assertEqual(out["session_signals"], {})
        self.assertIsNone(out["first_prompt"])

    def test_user_directive_event_carries_text_and_timestamp(self):
        out = extract([user("build the thing", ts="2026-01-01T09:00:00Z")])
        d = events_of(out, "user_directive")[0]
        self.assertEqual(d["text"], "build the thing")
        self.assertEqual(d["timestamp"], "2026-01-01T09:00:00Z")
        self.assertEqual(out["first_prompt"], "build the thing")


class BranchAndPrTests(unittest.TestCase):
    def test_entry_git_branch_wins_over_detected(self):
        out = extract([
            user("go", gitBranch="feat/from-entry"),
            assistant([{"type": "tool_use", "name": "Bash",
                        "input": {"command": "git checkout -b feat/switched"}}]),
        ])
        self.assertEqual(out["git_branch"], "feat/from-entry")
        self.assertEqual(out["event_branches"], ["feat/switched"])

    def test_detected_branch_backfills_when_no_entry_branch(self):
        out = extract([tool_result("On branch staging\nnothing to commit")])
        self.assertEqual(out["git_branch"], "staging")

    def test_pr_number_only_from_gh_pr_create(self):
        # A PR URL merely appearing in a tool result is NOT this session's
        # PR (it could be a dependency's PR being read) — no pr_number.
        out = extract([
            tool_result("see https://github.com/acme/app/pull/42 for details"),
        ])
        self.assertIsNone(out["pr_number"])
        # The result of a `gh pr create` run IS this session's PR.
        out = extract([
            assistant([{"type": "tool_use", "id": "t1", "name": "Bash",
                        "input": {"command": "gh pr create --title x"}}]),
            tool_result("https://github.com/acme/app/pull/57",
                        tool_use_id="t1"),
        ])
        self.assertEqual(out["pr_number"], 57)

    def test_pr_create_matched_by_tool_use_id(self):
        # Parallel tool calls: an unrelated result carrying a PR URL arrives
        # before the create's own result — only the id-matched result counts,
        # and later unrelated PR URLs do not overwrite it.
        out = extract([
            assistant([
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": "gh pr create --fill"}},
                {"type": "tool_use", "id": "t2", "name": "Bash",
                 "input": {"command": "gh pr view 9001 --repo other/dep"}},
            ]),
            tool_result("see https://github.com/other/dep/pull/9001",
                        tool_use_id="t2"),
            tool_result("https://github.com/acme/app/pull/57",
                        tool_use_id="t1"),
            tool_result("https://github.com/other/dep/pull/8888",
                        tool_use_id="t2"),
        ])
        self.assertEqual(out["pr_number"], 57)

    def test_pr_number_null_when_absent(self):
        out = extract([user("hello there")])
        self.assertIsNone(out["pr_number"])


class HelperTests(unittest.TestCase):
    def test_ruby_truncate_includes_omission_in_length(self):
        self.assertEqual(events._truncate("x" * 100, 10), "x" * 7 + "...")
        self.assertEqual(events._truncate("short", 10), "short")

    def test_round_is_half_away_from_zero(self):
        self.assertEqual(events._round(0.25, 1), 0.3)   # Python round() would give 0.2
        self.assertEqual(events._round(2.5 / 60 * 60, 1), 2.5)

    def test_cast_bool_matches_activemodel(self):
        for truthy in (True, "true", "TRUE", 1, "1", "yes", "banana"):
            self.assertTrue(events._cast_bool(truthy), msg=repr(truthy))
        for falsy in (False, "false", "FALSE", 0, "0", "f", "off", None, ""):
            self.assertFalse(events._cast_bool(falsy), msg=repr(falsy))


class ModelUsageTest(unittest.TestCase):
    def _entry(self, mid, model, usage, sidechain=False):
        e = {"type": "assistant", "timestamp": "2026-06-01T10:00:00Z",
             "message": {"role": "assistant", "id": mid, "model": model,
                         "usage": usage,
                         "content": [{"type": "text", "text": "x"}]}}
        if sidechain:
            e["isSidechain"] = True
        return e

    USAGE = {"input_tokens": 100, "output_tokens": 50,
             "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 20}

    def test_dedupes_by_message_id(self):
        entries = [self._entry("m1", "claude-fable-5", self.USAGE),
                   self._entry("m1", "claude-fable-5", self.USAGE)]
        model_usage, token_usage = events.extract_model_usage(entries, False)
        self.assertEqual(model_usage, {"claude-fable-5": 1})
        self.assertEqual(token_usage["claude-fable-5"], {
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 1000, "cache_write_tokens": 20,
            "assistant_turns": 1})

    def test_excludes_sidechain_entries(self):
        entries = [self._entry("m1", "claude-fable-5", self.USAGE),
                   self._entry("m2", "claude-haiku-4-5", self.USAGE,
                               sidechain=True)]
        model_usage, token_usage = events.extract_model_usage(entries, False)
        self.assertEqual(list(model_usage), ["claude-fable-5"])

    def test_sidechain_kept_for_subagent_transcripts(self):
        entries = [self._entry("m1", "claude-haiku-4-5", self.USAGE,
                               sidechain=True)]
        model_usage, _ = events.extract_model_usage(entries, True)
        self.assertEqual(model_usage, {"claude-haiku-4-5": 1})

    def test_usage_less_transcript_yields_empty_token_usage(self):
        e = self._entry("m1", "claude-fable-5", self.USAGE)
        del e["message"]["usage"]
        model_usage, token_usage = events.extract_model_usage([e], False)
        self.assertEqual(model_usage, {"claude-fable-5": 1})
        self.assertEqual(token_usage, {})

    def test_none_id_messages_each_counted(self):
        # No message id -> nothing to dedupe on; both entries count.
        entries = [self._entry(None, "claude-fable-5", self.USAGE),
                   self._entry(None, "claude-fable-5", self.USAGE)]
        model_usage, token_usage = events.extract_model_usage(entries, False)
        self.assertEqual(model_usage, {"claude-fable-5": 2})
        self.assertEqual(token_usage["claude-fable-5"]["input_tokens"], 200)
        self.assertEqual(token_usage["claude-fable-5"]["assistant_turns"], 2)


class PromptStatsTest(unittest.TestCase):
    def _msgs(self, texts):
        return [{"text": t, "word_count": len(t.split())} for t in texts]

    def test_median_short_count_and_caps(self):
        msgs = self._msgs([
            "fix it",
            "please refactor the upload pipeline now ok",
            "I LITERALLY SAID DONT TOUCH THAT FILE",
            "one two three four five six seven eight nine ten",
        ])
        stats = events.extract_prompt_stats(msgs)
        self.assertEqual(stats["short_prompt_count"], 3)
        self.assertEqual(stats["median_words"], 7.0)
        self.assertEqual(len(stats["caps_quotes"]), 1)
        self.assertEqual(stats["caps_quotes"][0]["text"],
                         "I LITERALLY SAID DONT TOUCH THAT FILE")
        self.assertGreaterEqual(stats["caps_quotes"][0]["caps_ratio"], 0.6)

    def test_empty(self):
        self.assertEqual(events.extract_prompt_stats([]), {
            "median_words": 0, "short_prompt_count": 0, "caps_quotes": []})


class SessionRecordAdditiveKeysTest(unittest.TestCase):
    def test_extract_session_emits_new_keys(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "s1.jsonl")
            entries = [
                {"type": "user", "timestamp": "2026-06-01T10:00:00Z",
                 "message": {"role": "user", "content": "hello world friend"}},
                {"type": "assistant", "timestamp": "2026-06-01T10:00:05Z",
                 "message": {"role": "assistant", "id": "m1",
                             "model": "claude-fable-5",
                             "usage": {"input_tokens": 10, "output_tokens": 5,
                                       "cache_read_input_tokens": 0,
                                       "cache_creation_input_tokens": 0},
                             "content": [{"type": "text", "text": "hi"}]}},
            ]
            with open(p, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e) + "\n")
            rec = events.extract_session(p)
            self.assertEqual(rec["model_usage"], {"claude-fable-5": 1})
            self.assertIn("token_usage", rec)
            self.assertEqual(rec["prompt_stats"]["short_prompt_count"], 1)

    def test_token_usage_absent_without_usage_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "s2.jsonl")
            entries = [
                {"type": "user", "timestamp": "2026-06-01T10:00:00Z",
                 "message": {"role": "user", "content": "hello world friend"}},
                {"type": "assistant", "timestamp": "2026-06-01T10:00:05Z",
                 "message": {"role": "assistant", "id": "m1",
                             "model": "claude-fable-5",
                             "content": [{"type": "text", "text": "hi"}]}},
            ]
            with open(p, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e) + "\n")
            rec = events.extract_session(p)
            self.assertEqual(rec["model_usage"], {"claude-fable-5": 1})
            self.assertNotIn("token_usage", rec)


if __name__ == "__main__":
    unittest.main(verbosity=2)

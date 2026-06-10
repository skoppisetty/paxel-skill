#!/usr/bin/env python3
"""Tests for episodes.py — the build_episode_input / preload port.

Stdlib only (unittest):  python3 scripts/test_episodes.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import episodes  # noqa: E402


def session(sid, **kw):
    base = {
        "session_id": sid, "path": "/tmp/%s.jsonl" % sid,
        "first_prompt": "do thing %s" % sid,
        "events": [], "session_signals": {}, "user_highlights": "",
        "plan_files": [], "active_time_windows": [],
        "pr_number": None, "git_branch": None,
        "event_git_shas": [], "event_branches": [],
        "dispatch_metadata": {"dispatch_count": 0, "return_count": 0,
                              "run_in_background_count": 0,
                              "unique_subagent_ids": []},
    }
    base.update(kw)
    return base


class BuildInputTests(unittest.TestCase):
    def base_data(self, **kw):
        data = {
            "episode_id": "ep1", "episode_type": "implementation",
            "narratives": "", "signals": [], "user_highlights": "",
            "first_prompts": [], "session_count": 1,
            "commit_group_count": 0, "added_lines": 0, "deleted_lines": 0,
            "decision_summary": None, "plan_files": [],
            "session_intents": {}, "dispatch_count": 0,
            "dispatch_with_committed_return_ratio": None,
        }
        data.update(kw)
        return data

    def test_header_always_present(self):
        text = episodes.build_episode_input(self.base_data())
        self.assertIn("Episode type: implementation", text)
        self.assertIn("Sessions: 1, Commit groups: 0", text)

    def test_code_volume_gated_on_commit_groups(self):
        # zero commit groups: no LOC line even with lines set
        text = episodes.build_episode_input(
            self.base_data(added_lines=10, deleted_lines=5))
        self.assertNotIn("Code volume:", text)
        # commit groups but zero LOC: no line (episode_summarizer.rb:364)
        text = episodes.build_episode_input(
            self.base_data(commit_group_count=2))
        self.assertNotIn("Code volume:", text)
        # both: line present, exact format
        text = episodes.build_episode_input(
            self.base_data(commit_group_count=2, added_lines=10,
                           deleted_lines=5))
        self.assertIn(
            "Code volume: +10/-5 lines (from this episode's commits)", text)

    def test_session_intent_only_for_session_only(self):
        text = episodes.build_episode_input(self.base_data(
            episode_type="implementation",
            session_intents={"shipping": 2}))
        self.assertNotIn("Session intent:", text)
        text = episodes.build_episode_input(self.base_data(
            episode_type="session_only",
            session_intents={"shipping": 2, "exploration": 1}))
        self.assertIn("Session intent: shipping", text)

    def test_session_intent_tie_is_ambiguous(self):
        text = episodes.build_episode_input(self.base_data(
            episode_type="session_only",
            session_intents={"shipping": 1, "exploration": 1}))
        self.assertIn("Session intent: ambiguous", text)

    def test_first_prompts_codex_filter_dedup_first5(self):
        prompts = ["You are Codex, blah", "a", "a", "b", "c", "d", "e", "f"]
        text = episodes.build_episode_input(
            self.base_data(first_prompts=prompts))
        self.assertIn("First prompts: a | b | c | d | e", text)
        self.assertNotIn("You are Codex", text)
        self.assertNotIn("| f", text)

    def test_blocks_omitted_when_blank(self):
        text = episodes.build_episode_input(self.base_data(
            narratives="  ", user_highlights="", decision_summary=None))
        for header in ("## Session Narratives", "## Code Reviews",
                       "## User Highlights", "## Decision Exchanges",
                       "## Plan Files", "## Session Signals",
                       "## Subagent Dispatch Activity"):
            self.assertNotIn(header, text)

    def test_plan_files_block_format(self):
        text = episodes.build_episode_input(self.base_data(
            plan_files=[{"filename": "PLAN.md", "version_count": 3,
                         "content": "steps"}]))
        self.assertIn("## Plan Files\n### PLAN.md (3 version(s))\nsteps",
                      text)

    def test_signals_block_aggregation_order_and_filter(self):
        sig_a = {"kill_decisions": 1, "critiques": 0, "review_checks": 2}
        sig_b = {"kill_decisions": 2, "imperative_prompts": 4}
        text = episodes.build_episode_input(
            self.base_data(signals=[sig_a, sig_b]))
        self.assertIn(
            "## Session Signals\nkill_decisions: 3, imperative_prompts: 4, "
            "review_checks: 2", text)

    def test_dispatch_block_only_when_dispatches(self):
        text = episodes.build_episode_input(self.base_data(dispatch_count=0))
        self.assertNotIn("## Subagent Dispatch Activity", text)
        text = episodes.build_episode_input(self.base_data(
            dispatch_count=3, dispatch_with_committed_return_ratio=0.5))
        self.assertIn("Dispatches: 3 | Committed-return ratio: 0.50", text)
        text = episodes.build_episode_input(
            self.base_data(dispatch_count=3))
        self.assertIn("Committed-return ratio: n/a", text)


class DispatchStatsTests(unittest.TestCase):
    def test_no_dispatch(self):
        s = session("s1")
        self.assertEqual(episodes.compute_dispatch_stats([s]), (0, None))

    def test_committed_return_ratio(self):
        shipped = session("s1", events=[
            {"type": "subagent_dispatch", "index": 0, "tool_use_id": "t1"},
            {"type": "subagent_return", "index": 1, "tool_use_id": "t1"},
            {"type": "git_commit", "index": 2, "sha": "abc"},
        ])
        theater = session("s2", events=[
            {"type": "subagent_dispatch", "index": 0, "tool_use_id": "t2"},
        ])
        count, ratio = episodes.compute_dispatch_stats([shipped, theater])
        self.assertEqual(count, 2)
        self.assertEqual(ratio, 0.5)


class PlanFileTests(unittest.TestCase):
    def test_latest_versions_and_count(self):
        s = session("s1", plan_files=[
            {"filename": "PLAN.md", "version": 1, "content": "v1"},
            {"filename": "PLAN.md", "version": 2, "content": "v2"},
            {"filename": "OTHER_PLAN.md", "version": 1, "content": "o"},
        ])
        plans = episodes.latest_plan_files(s)
        by_name = {p["filename"]: p for p in plans}
        self.assertEqual(by_name["PLAN.md"]["version_count"], 2)
        self.assertEqual(by_name["PLAN.md"]["content"], "v2")
        self.assertEqual(by_name["OTHER_PLAN.md"]["version_count"], 1)


class EndToEndTests(unittest.TestCase):
    def test_cli_assembles_episode(self):
        tmp = tempfile.mkdtemp()
        try:
            sessions_path = os.path.join(tmp, "sessions.jsonl")
            with open(sessions_path, "w") as f:
                f.write(json.dumps(session(
                    "s1", session_signals={"kill_decisions": 1},
                    user_highlights="please fix the export bug")) + "\n")
            gitdata_path = os.path.join(tmp, "git.json")
            with open(gitdata_path, "w") as f:
                json.dump({
                    "commit_groups": [{"id": "cg1", "group_type": "single_commit",
                                       "commit_shas": ["abc"], "title": "fix: x",
                                       "insertions": 7, "deletions": 2}],
                    "episodes": [{"episode_id": "ep1",
                                  "episode_type": "bugfix",
                                  "confidence": 0.9,
                                  "session_ids": ["s1"],
                                  "commit_group_ids": ["cg1"],
                                  "added_lines": 7, "deleted_lines": 2}],
                }, f)
            narr_dir = os.path.join(tmp, "narrs")
            os.makedirs(narr_dir)
            with open(os.path.join(narr_dir, "s1.md"), "w") as f:
                f.write("## Goal\nFix x.\n<session_intent>shipping"
                        "</session_intent>")
            out_dir = os.path.join(tmp, "inputs")
            sys.argv = ["episodes.py", "--sessions", sessions_path,
                        "--episodes", gitdata_path, "--narratives", narr_dir,
                        "--out-dir", out_dir]
            episodes.main()
            with open(os.path.join(out_dir, "ep1.txt")) as f:
                text = f.read()
            self.assertIn("Episode type: bugfix", text)
            self.assertIn("Code volume: +7/-2 lines", text)
            self.assertIn("## Session Narratives\n## Goal\nFix x.", text)
            self.assertNotIn("<session_intent>", text)
            self.assertIn("## User Highlights\nplease fix the export bug",
                          text)
            self.assertIn("kill_decisions: 1", text)
            with open(os.path.join(out_dir, "episodes_manifest.json")) as f:
                manifest = json.load(f)
            self.assertEqual(manifest[0]["episode_id"], "ep1")
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=1)

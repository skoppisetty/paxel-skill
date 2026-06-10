#!/usr/bin/env python3
"""
Synthetic-fixture tests for analytics.py (the upload-only analytics port).

Stdlib only (unittest), matching test_scripts.py. Builds a real temp git repo
with pinned authors/dates so the git-derived goldens are exact. Run:

    python3 scripts/test_analytics.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analytics  # noqa: E402

GIT_ENV_BASE = {
    "GIT_AUTHOR_NAME": "Test User",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test User",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    # Keep user config from leaking in (hooks, signing, etc.)
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "HOME": tempfile.gettempdir(),
    "PATH": os.environ.get("PATH", ""),
}

MAIN_PY_V1 = "def main():\n    return 1\n"                       # 2 lines
TEST_PY = "def test_main():\n    assert True\n"                   # 2 lines
MAIN_PY_APPEND = "# done\n"                                       # +1 line

HANDLERS_RB = (
    "def a\n  foo\nrescue => e\n  nil\nend\n"
    "def b\n  bar\nrescue StandardError\n  nil\nend\n"
    "def c\n  baz\nrescue ArgumentError, TypeError\n  nil\nend\n"
    "def d\n  qux\nrescue\n  nil\nend\n")                         # 20 lines

HANDLERS_PY = (
    "try:\n    pass\nexcept:\n    pass\n"
    "try:\n    pass\nexcept Exception:\n    pass\n"
    "try:\n    pass\nexcept ValueError:\n    pass\n")             # 12 lines

CLAUDE_MD = ("# Rules\n\n"
             "- NEVER push directly to main\n"
             "- ALWAYS run the suite\n")                          # 4 lines


def _git(repo, *args, date=None):
    env = dict(GIT_ENV_BASE)
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", "-C", repo, *args], env=env,
                   capture_output=True, check=True)


def _write(repo, rel, content, append=False):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path) or path, exist_ok=True)
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        fh.write(content)


class GitFixtureMixin:
    """One repo, three commits:
      c1 2026-01-01 "feat: add main"          src/main.py(2) test/test_main.py(2)
      c2 2026-01-03 "update stuff"            main.py+1, handlers.rb(20), handlers.py(12)
      c3 2026-01-03 'Revert "feat: add main"' CLAUDE.md(4)
    Insertions: 4 + 33 + 4 = 41, deletions 0, test insertions 2."""

    @classmethod
    def setUpClass(cls):
        cls.repo = tempfile.mkdtemp(prefix="analytics_fixture_")
        _git(cls.repo, "init", "-q")
        _write(cls.repo, "src/main.py", MAIN_PY_V1)
        _write(cls.repo, "test/test_main.py", TEST_PY)
        _git(cls.repo, "add", "-A")
        _git(cls.repo, "commit", "-q", "-m", "feat: add main",
             date="2026-01-01T10:00:00+00:00")
        _write(cls.repo, "src/main.py", MAIN_PY_APPEND, append=True)
        _write(cls.repo, "handlers.rb", HANDLERS_RB)
        _write(cls.repo, "handlers.py", HANDLERS_PY)
        _git(cls.repo, "add", "-A")
        _git(cls.repo, "commit", "-q", "-m", "update stuff",
             date="2026-01-03T10:00:00+00:00")
        _write(cls.repo, "CLAUDE.md", CLAUDE_MD)
        _git(cls.repo, "add", "-A")
        _git(cls.repo, "commit", "-q", "-m", 'Revert "feat: add main"',
             date="2026-01-03T12:00:00+00:00")
        cls.commits = analytics.collect_commits(cls.repo)
        analyzer = analytics.CodeQualityAnalyzer(cls.repo, cls.commits)
        cls.code_quality = analyzer.analyze()
        cls.analyzer = analyzer

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)


class CollectCommitsTests(GitFixtureMixin, unittest.TestCase):
    def test_commit_parse_shape(self):
        self.assertEqual(len(self.commits), 3)
        newest = self.commits[0]  # git log is newest-first
        self.assertEqual(newest["subject"], 'Revert "feat: add main"')
        self.assertEqual(newest["author"], "Test User")
        self.assertEqual(newest["email"], "test@example.com")
        self.assertEqual(newest["files"], [
            {"added": 4, "deleted": 0, "path": "CLAUDE.md"}])

    def test_since_filters_commits(self):
        recent = analytics.collect_commits(self.repo, since="2026-01-02")
        self.assertEqual(len(recent), 2)
        self.assertNotIn("feat: add main", [c["subject"] for c in recent])


class CodeQualityDimensionTests(GitFixtureMixin, unittest.TestCase):
    def dims(self):
        return self.code_quality["dimensions"]

    def test_status_and_file_count(self):
        self.assertEqual(self.code_quality["status"], "complete")
        # CLAUDE.md, handlers.py, handlers.rb, src/main.py, test/test_main.py
        self.assertEqual(self.code_quality["file_count"], 5)

    def test_commit_discipline_ratios(self):
        d = self.dims()["commit_discipline"]
        self.assertEqual(d["total_commits"], 3)
        self.assertEqual(d["conventional_commit_ratio"], 0.333)  # feat: only
        self.assertEqual(d["revert_ratio"], 0.333)               # Revert "..."
        self.assertEqual(d["atomic_commit_ratio"], 1.0)
        # sizes 4 + 33 + 4 → 41/3 = 13.67 → Ruby round(0) = 14
        self.assertEqual(d["avg_commit_size_lines"], 14)

    def test_test_quality_detection(self):
        d = self.dims()["test_quality"]
        self.assertEqual(d["test_file_count"], 1)        # test/test_main.py
        self.assertEqual(d["test_dirs"], 1)
        self.assertEqual(d["test_frameworks"], ["pytest"])
        self.assertFalse(d["has_factories"])
        self.assertFalse(d["has_fixtures"])
        # test LOC 2 / prod LOC (3 + 20 + 12) = 35
        self.assertEqual(d["test_ratio"], analytics.ratio(2, 35))

    def test_code_quality_lengths(self):
        d = self.dims()["code_quality"]
        self.assertEqual(d["total_source_files"], 4)
        # lengths: handlers.py 12, handlers.rb 20, src/main.py 3, test 2
        self.assertEqual(d["avg_file_length"], 9)        # 37/4=9.25 → 9
        self.assertEqual(d["median_file_length"], 8)     # (3+12)/2=7.5 → 8
        self.assertEqual(d["max_file_length"], 20)
        self.assertEqual(d["god_object_count"], 0)

    def test_error_handling_rescue_counts(self):
        d = self.dims()["error_handling"]
        # Ruby: bare 2 ("rescue => e", bare "rescue") + standard 1; specific 1.
        # Python: bare 1 ("except:") + Exception 1; specific 1 (ValueError).
        self.assertEqual(d["rescue_total"], 7)
        self.assertEqual(d["rescue_generic"], 5)
        self.assertEqual(d["rescue_specific"], 2)
        self.assertEqual(d["bare_rescue_ratio"], analytics.ratio(5, 7))
        self.assertFalse(d["has_retry_logic"])

    def test_security_signals_clean_fixture(self):
        d = self.dims()["security_signals"]
        self.assertEqual(d["eval_usage"], 0)
        self.assertEqual(d["exec_usage"], 0)
        self.assertEqual(d["hardcoded_secret_patterns"], 0)
        self.assertFalse(d["has_security_config"])

    def test_agent_config_quality_claude_md(self):
        d = self.dims()["agent_config_quality"]
        self.assertTrue(d["exists"])
        self.assertEqual(d["tools_configured"], ["claude_code"])
        self.assertEqual(d["total_word_count"], 13)
        self.assertEqual(d["total_constraint_count"], 2)  # NEVER + ALWAYS
        self.assertEqual(d["never_count"], 1)
        self.assertEqual(d["always_count"], 1)
        self.assertEqual(d["must_count"], 0)
        self.assertEqual(d["rules_count"], 2)
        self.assertEqual(d["line_count"], 4)

    def test_architecture_generic_no_components(self):
        d = self.dims()["architecture"]
        self.assertEqual(d["total_components"], 0)
        self.assertFalse(d["has_separation_of_concerns"])

    def test_infrastructure_linter_always_false_ruby_quirk(self):
        d = self.dims()["infrastructure"]
        self.assertFalse(d["has_docker"])
        self.assertFalse(d["has_linter"])  # never set by the tree detector

    def test_git_workflow(self):
        d = self.dims()["git_workflow"]
        self.assertEqual(d["active_days"], 2)
        self.assertEqual(d["commits_per_active_day"], 1.5)
        self.assertEqual(d["version_commits"], 0)

    def test_code_evolution(self):
        d = self.dims()["code_evolution"]
        self.assertEqual(d["total_lines_added"], 41)
        self.assertEqual(d["total_lines_deleted"], 0)
        self.assertEqual(d["net_loc_change"], 41)
        self.assertEqual(d["deletion_ratio"], 0.0)
        self.assertEqual(d["refactor_commit_ratio"], 0.0)

    def test_documentation_dimension(self):
        d = self.dims()["documentation"]
        self.assertEqual(d["doc_count"], 1)  # CLAUDE.md
        self.assertFalse(d["has_readme"])
        self.assertFalse(d["has_changelog"])

    def test_dimension_fault_isolation(self):
        class Broken(analytics.CodeQualityAnalyzer):
            def analyze_test_quality(self):
                raise ValueError("boom")
        result = Broken(self.repo, self.commits).analyze()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["dimensions"]["test_quality"],
                         {"status": "error", "error": "ValueError"})
        # the other dimensions survive
        self.assertEqual(
            result["dimensions"]["commit_discipline"]["total_commits"], 3)

    def test_no_repo_and_empty_repo_statuses(self):
        self.assertEqual(
            analytics.CodeQualityAnalyzer("/nonexistent/path", []).analyze(),
            {"status": "no_repo", "dimensions": {}})
        empty = tempfile.mkdtemp(prefix="analytics_empty_")
        try:
            result = analytics.CodeQualityAnalyzer(empty, []).analyze()
            self.assertEqual(result["status"], "empty_repo")
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class ProfilerTests(unittest.TestCase):
    def test_rails_architecture_table(self):
        files = ["Gemfile", "app/services/foo.rb", "app/services/bar.rb",
                 "app/models/user.rb", "app/models/concerns/sluggable.rb",
                 "app/controllers/users_controller.rb",
                 "app/views/users/index.html.erb", "spec/models/user_spec.rb"]
        prof = analytics.Profiler("/nonexistent", files)
        self.assertEqual(prof.detect_project_framework(), "rails")
        arch = prof.analyze_architecture()
        self.assertEqual(arch["services"], 2)
        self.assertEqual(arch["models"], 1)        # concerns/ excluded
        self.assertEqual(arch["concerns"], 1)
        self.assertEqual(arch["controllers"], 1)
        self.assertEqual(arch["views"], 1)

    def test_infra_signals_from_tree(self):
        prof = analytics.Profiler(
            "/nonexistent",
            ["Dockerfile", ".github/workflows/ci.yml", "main.tf"])
        sig = prof.detect_infrastructure_from_tree()
        self.assertTrue(sig["docker"])
        self.assertTrue(sig["ci"])
        self.assertTrue(sig["terraform"])
        self.assertNotIn("monitoring", sig)

    def test_documentation_excludes_test_and_vendor_dirs(self):
        prof = analytics.Profiler(
            "/nonexistent",
            ["README", "docs/design.md", "spec/notes.md",
             "node_modules/pkg/README.md", ".github/PULL_REQUEST.md"])
        docs = prof.analyze_documentation()
        self.assertEqual(docs["doc_count"], 2)
        self.assertTrue(docs["has_design_docs"])
        self.assertFalse(docs["has_architecture_doc"])


class VelocityTests(GitFixtureMixin, unittest.TestCase):
    def test_velocity_sums(self):
        v = analytics.compute_velocity(self.commits)
        self.assertEqual(v["insertions"], 41)
        self.assertEqual(v["deletions"], 0)
        self.assertEqual(v["net_loc"], 41)
        self.assertEqual(v["test_insertions"], 2)
        self.assertEqual(v["test_deletions"], 0)
        self.assertEqual(v["test_ratio"], analytics.ratio(2, 41))
        self.assertEqual(v["date_range_days"], 3)     # Jan 1 .. Jan 3
        self.assertEqual(v["loc_per_day"], 13)        # 41 // 3
        self.assertEqual(v["data_source"], "numstat")

    def test_velocity_authors_and_daily(self):
        v = analytics.compute_velocity(self.commits)
        self.assertEqual(v["authors"], {
            "Test User": {"insertions": 41, "deletions": 0, "commits": 3}})
        self.assertEqual([d["date"] for d in v["daily_loc"]],
                         ["2026-01-01", "2026-01-03"])
        self.assertEqual(v["peak_day"]["date"], "2026-01-03")
        self.assertEqual(v["peak_day"]["insertions"], 37)

    def test_ship_to_revert_ratio(self):
        v = analytics.compute_velocity(self.commits)
        # 3 commits, 1 revert → ships 2/3
        self.assertEqual(v["ship_to_revert_ratio"], 0.667)

    def test_empty_commits_returns_empty_hash(self):
        self.assertEqual(analytics.compute_velocity([]), {})

    def test_author_scoped_variant(self):
        av = analytics.compute_velocity_for_author(
            self.commits, ["test user"], [])
        self.assertEqual(av["insertions"], 41)
        self.assertEqual(av["commits"], 3)
        self.assertEqual(av["date_range_days"], 3)
        self.assertEqual(av["loc_per_day"], 13)
        by_email = analytics.compute_velocity_for_author(
            self.commits, [], ["TEST@EXAMPLE.COM"])
        self.assertEqual(by_email["insertions"], 41)
        self.assertEqual(
            analytics.compute_velocity_for_author(self.commits, ["nobody"], []),
            {})


class SteeringTests(unittest.TestCase):
    def trace(self, *texts):
        return analytics.extract_steering_trace(
            [{"type": "user_directive", "text": t} for t in texts])

    def test_action_classification_goldens(self):
        cases = [
            ("Implement the login feature", {"delegate": 1}),
            ("Actually, let's switch to Redis instead", {"redirect": 1}),
            ("ship it", {"ship": 1}),
            ("No, that's wrong - revert it", {"reject": 1, "debug": 1}),
            ("why is the test failing?",
             {"delegate": 1, "verify": 1, "debug": 1}),
            ("You must always follow the style rule",
             {"constrain": 1}),
        ]
        for text, expected in cases:
            self.assertEqual(self.trace(text)["action_counts"], expected,
                             msg=text)

    def test_blank_text_skipped_and_totals(self):
        trace = self.trace("", "   ", "deploy the fix")
        # "deploy the fix" → delegate(deploy? deploy is in delegate list: yes,
        # fix too — one match per action type), ship(deploy), recover(fix)
        self.assertEqual(trace["action_counts"],
                         {"delegate": 1, "ship": 1, "recover": 1})
        self.assertEqual(trace["total_actions"], 3)

    def test_actions_capped_at_100_total_uncapped(self):
        trace = self.trace(*["ship it"] * 150)
        self.assertEqual(len(trace["actions"]), 100)
        self.assertEqual(trace["total_actions"], 150)
        self.assertEqual(trace["action_counts"]["ship"], 150)

    def test_analyze_steering_per_session_and_totals(self):
        sessions = [
            {"session_id": "s1", "events": [
                {"type": "user_directive", "text": "ship it"},
                {"type": "git_commit", "message": "x"}]},
            {"session_id": "s2", "events": []},
        ]
        out = analytics.analyze_steering(sessions)
        self.assertEqual(out["per_session"]["s1"]["action_counts"], {"ship": 1})
        self.assertEqual(out["per_session"]["s2"]["total_actions"], 0)
        self.assertEqual(out["totals"]["total_actions"], 1)
        self.assertEqual(out["totals"]["sessions_analyzed"], 2)
        self.assertEqual(out["totals"]["sessions_with_actions"], 1)


class ParallelismTests(unittest.TestCase):
    @staticmethod
    def session(sid, events, dispatch_count=0, return_count=0):
        return {"session_id": sid, "events": events,
                "dispatch_metadata": {"dispatch_count": dispatch_count,
                                      "return_count": return_count}}

    def test_committed_return_via_parent_commit_fallback(self):
        committed = self.session("s1", [
            {"type": "user_directive", "text": "go"},
            {"type": "subagent_dispatch", "tool_use_id": "t1"},
            {"type": "subagent_return", "tool_use_id": "t1"},
            {"type": "git_commit", "message": "feat: x"},
        ], dispatch_count=1, return_count=1)
        no_return = self.session("s2", [
            {"type": "subagent_dispatch", "tool_use_id": "t2"},
            {"type": "git_commit", "message": "x"},
        ], dispatch_count=1)
        mismatched_return = self.session("s3", [
            {"type": "subagent_dispatch", "tool_use_id": "t3"},
            {"type": "subagent_return", "tool_use_id": "t999"},
            {"type": "git_commit", "message": "x"},
        ], dispatch_count=1, return_count=1)
        commit_before_return = self.session("s4", [
            {"type": "subagent_dispatch", "tool_use_id": "t4"},
            {"type": "git_commit", "message": "x"},
            {"type": "subagent_return", "tool_use_id": "t4"},
        ], dispatch_count=1, return_count=1)
        out = analytics.analyze_parallelism(
            [committed, no_return, mismatched_return, commit_before_return])
        self.assertEqual(out["dispatch_with_committed_return_count"], 1)
        self.assertEqual(out["dispatch_count"], 4)
        self.assertEqual(out["return_count"], 3)

    def test_pair_signals_are_null_not_zero(self):
        out = analytics.analyze_parallelism([])
        self.assertIsNone(out["concurrent_pairs_with_ships_count"])
        self.assertIsNone(out["review_separation_count"])
        self.assertTrue(out["notes"])

    def test_subagent_sessions_excluded_from_mains(self):
        sub = {"session_id": "child", "is_subagent": True,
               "parent_session_id": None,
               "dispatch_metadata": {"dispatch_count": 9, "return_count": 9},
               "events": []}
        out = analytics.analyze_parallelism([sub])
        self.assertEqual(out["dispatch_count"], 0)
        self.assertEqual(out["subagent_count"], 1)
        self.assertEqual(out["orphan_subagent_count"], 1)


class HelperTests(unittest.TestCase):
    def test_ruby_round_half_away_from_zero(self):
        self.assertEqual(analytics.ruby_round(7.5), 8)
        self.assertEqual(analytics.ruby_round(2.5), 3)       # Python round → 2
        self.assertEqual(analytics.ruby_round(41 / 3, 0), 14)
        # binary-exact: 2.675 stores below the half → 2.67, as Ruby
        self.assertEqual(analytics.ruby_round(2.675, 2), 2.67)

    def test_ratio(self):
        self.assertEqual(analytics.ratio(1, 0), 0.0)
        self.assertEqual(analytics.ratio(2, 3), 0.667)
        self.assertEqual(analytics.ratio(5, 7), 0.714)

    def test_ruby_lines_count(self):
        self.assertEqual(analytics.ruby_lines_count(""), 0)
        self.assertEqual(analytics.ruby_lines_count("a\n"), 1)
        self.assertEqual(analytics.ruby_lines_count("a\nb"), 2)

    def test_rails_truncate(self):
        self.assertEqual(analytics.rails_truncate("abc", 5), "abc")
        self.assertEqual(analytics.rails_truncate("a" * 10, 8), "a" * 5 + "...")

    def test_ruby_split_ws(self):
        self.assertEqual(len(analytics.ruby_split_ws("a b  c")), 3)
        self.assertEqual(analytics.ruby_split_ws(" a"), ["", "a"])  # Ruby parity
        self.assertEqual(analytics.ruby_split_ws("a "), ["a"])
        self.assertEqual(analytics.ruby_split_ws(""), [])


class EndToEndTests(GitFixtureMixin, unittest.TestCase):
    def test_build_report_structure_and_markdown(self):
        sessions = [
            {"session_id": "s1",
             "events": [
                 {"type": "user_directive", "text": "ship it",
                  "timestamp": "2026-01-03T10:00:00Z"},
                 {"type": "subagent_dispatch", "tool_use_id": "t1"},
                 {"type": "subagent_return", "tool_use_id": "t1"},
                 {"type": "git_commit", "message": "feat: x"}],
             "active_time_windows": [["2026-01-03T09:00:00Z",
                                      "2026-01-03T11:00:00Z"]],
             "session_signals": {},
             "dispatch_metadata": {"dispatch_count": 1, "return_count": 1}},
        ]
        report = analytics.build_report(
            self.repo, sessions, author_names=["Test User"])
        for key in ("code_quality", "velocity", "steering", "parallelism",
                    "profile"):
            self.assertIn(key, report)
        self.assertEqual(report["code_quality"]["status"], "complete")
        self.assertEqual(report["velocity"]["insertions"], 41)
        self.assertEqual(report["velocity"]["author_velocity"]["commits"], 3)
        self.assertEqual(
            report["steering"]["per_session"]["s1"]["action_counts"],
            {"ship": 1})
        self.assertEqual(
            report["parallelism"]["dispatch_with_committed_return_count"], 1)
        self.assertEqual(report["profile"]["project_framework"], "generic")
        self.assertTrue(
            report["profile"]["agent_config_stats"]["tools_configured"])
        # JSON-serializable end to end, and markdown renders every section
        md = analytics.render_markdown(json.loads(json.dumps(report)))
        for header in ("## Code Quality", "## Velocity", "## Steering",
                       "## Parallelism", "## Profile"):
            self.assertIn(header, md)
        self.assertIn("41", md)

    def test_load_sessions_since_filter(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"session_id": "old", "events": [
                {"type": "user_directive", "text": "x",
                 "timestamp": "2025-01-01T00:00:00Z"}]}) + "\n")
            fh.write(json.dumps({"session_id": "new", "events": [
                {"type": "user_directive", "text": "y",
                 "timestamp": "2026-02-01T00:00:00Z"}]}) + "\n")
            fh.write(json.dumps({"session_id": "no_ts", "events": []}) + "\n")
            fh.write("not json\n")
        try:
            all_sessions = analytics.load_sessions(path)
            self.assertEqual(len(all_sessions), 3)
            kept = analytics.load_sessions(path, since="2026-01-01T00:00:00Z")
            self.assertEqual([s["session_id"] for s in kept],
                             ["new", "no_ts"])  # timestamp-less kept
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)

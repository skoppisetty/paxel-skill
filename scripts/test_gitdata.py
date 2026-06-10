#!/usr/bin/env python3
"""
Tests for gitdata.py (CommitGrouper / EpisodeLinker / LinkingStrategy port).

Stdlib only (unittest) — no third-party deps. Synthetic fixtures: a temp git
repo with scripted commit dates (GIT_AUTHOR_DATE/GIT_COMMITTER_DATE) for the
collection + CLI path, plus pure-unit fixtures for the grouping/linking rules.

    python3 scripts/test_gitdata.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gitdata  # noqa: E402

GITDATA_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gitdata.py")
AUTHOR = ("Test User", "test@example.com")


def git(repo, *args, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    subprocess.run(["git", "-C", repo, "-c", "commit.gpgsign=false"] + list(args),
                   check=True, capture_output=True, env=full_env)


def init_repo(path):
    subprocess.run(["git", "init", "-q", path], check=True, capture_output=True)


def add_commit(repo, subject, date_iso, lines=3, author=AUTHOR, filename=None):
    """One commit creating a fresh file with `lines` lines at a scripted date.
    Returns the full sha."""
    name, email = author
    filename = filename or ("file_%s.txt" % abs(hash((subject, date_iso))))
    with open(os.path.join(repo, filename), "w") as f:
        f.write("".join("line %d\n" % i for i in range(lines)))
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=%s" % name, "-c", "user.email=%s" % email,
        "commit", "-q", "-m", subject,
        env={"GIT_AUTHOR_DATE": date_iso, "GIT_COMMITTER_DATE": date_iso})
    out = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                         check=True, capture_output=True)
    return out.stdout.decode().strip()


def commit_stub(sha, date, subject="work"):
    return {"sha": sha, "short": sha[:7], "author": "Test User",
            "email": "test@example.com", "date": date, "subject": subject}


def dt(s):
    return gitdata.parse_dt(s)


# --- parsers (client_pipeline.rb shapes) ---

class ParserTests(unittest.TestCase):
    def test_parse_commits_tsv_keeps_tabs_in_subject_and_drops_malformed(self):
        content = ("abc\tab\tMe\tme@x.com\t2026-06-01T10:00:00+00:00\tsubj\twith\ttabs\n"
                   "short\tline\n"
                   "\n"
                   "def\tde\tMe\tme@x.com\t2026-06-01T11:00:00+00:00\t\n")
        commits = gitdata.parse_commits_tsv(content)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0]["subject"], "subj\twith\ttabs")
        self.assertEqual(commits[1]["subject"], "")  # empty subject survives

    def test_parse_numstat_binary_dash_is_zero(self):
        content = ("COMMIT_BOUNDARY abc 2026-06-01T10:00:00+00:00 Test User <t@e.com>\n"
                   "3\t1\ta.txt\n"
                   "-\t-\tbin.png\n"
                   "\n"
                   "COMMIT_BOUNDARY def 2026-06-01T11:00:00+00:00 NoEmail\n"
                   "2\t0\tb.txt\n")
        ns = gitdata.parse_numstat_file(content)
        self.assertEqual(ns["abc"]["added"], 3)
        self.assertEqual(ns["abc"]["deleted"], 1)
        self.assertEqual(ns["abc"]["author"], "Test User")
        self.assertEqual(ns["abc"]["email"], "t@e.com")
        self.assertEqual(ns["abc"]["files"],
                         [{"file": "a.txt", "added": 3, "deleted": 1},
                          {"file": "bin.png", "added": 0, "deleted": 0}])
        self.assertEqual(ns["def"]["email"], None)  # no <email> → author kept raw
        self.assertEqual(ns["def"]["added"], 2)

    def test_truncate_matches_rails_total_length_semantics(self):
        self.assertEqual(gitdata.truncate("a" * 60, 60), "a" * 60)
        self.assertEqual(gitdata.truncate("a" * 61, 60), "a" * 57 + "...")
        self.assertIsNone(gitdata.truncate(None, 60))

    def test_pr_diff_stats_parse(self):
        diff = ("--- a/lib/core.py\n"
                "+++ b/lib/core.py\n"
                "+new line\n"
                "+another\n"
                "-old line\n"
                "--- a/tests/test_core.py\n"
                "+++ b/tests/test_core.py\n"
                "+assert ok\n")
        stats = gitdata.parse_pr_diff_stats(diff)
        self.assertEqual(stats["insertions"], 3)
        self.assertEqual(stats["deletions"], 1)
        self.assertEqual(stats["net_loc"], 2)
        self.assertEqual(stats["files_changed"], 2)
        self.assertEqual(stats["test_insertions"], 1)
        self.assertEqual(stats["test_loc_ratio"], 0.333)
        self.assertEqual(gitdata.parse_pr_diff_stats(""), {})


# --- CommitGrouper ---

class GrouperTests(unittest.TestCase):
    def test_cluster_gap_boundary_inclusive_at_exactly_2h(self):
        commits = [
            commit_stub("c1", "2026-06-01T10:00:00+00:00"),
            commit_stub("c2", "2026-06-01T12:00:00+00:00"),       # gap == 7200s → in
            commit_stub("c3", "2026-06-01T14:00:01+00:00"),       # gap 7201s → out
        ]
        groups = gitdata.group_commits(commits, {}, [])
        types = {g["group_type"]: g for g in groups}
        self.assertEqual(types["commit_cluster"]["commit_shas"], ["c1", "c2"])
        self.assertEqual(types["single_commit"]["commit_shas"], ["c3"])

    def test_cluster_gap_is_chained_not_anchored_to_cluster_start(self):
        commits = [
            commit_stub("c1", "2026-06-01T10:00:00+00:00"),
            commit_stub("c2", "2026-06-01T11:30:00+00:00"),
            commit_stub("c3", "2026-06-01T13:00:00+00:00"),  # 3h from c1, 1.5h from c2
        ]
        groups = gitdata.group_commits(commits, {}, [])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["commit_shas"], ["c1", "c2", "c3"])
        self.assertEqual(groups[0]["title"], "3 commits: work")
        self.assertIsNone(groups[0]["branch"])  # TSV carries no branch (quirk)

    def test_pr_group_substring_quirk_and_dedup(self):
        commits = [commit_stub("c1", "2026-06-01T10:00:00+00:00", "Merge PR #123")]
        sessions = [{"session_id": "s1", "pr_number": 12, "git_branch": "feat/x"},
                    {"session_id": "s2", "pr_number": 12, "git_branch": "other"}]
        groups = gitdata.group_commits(commits, {}, sessions)
        pr_groups = [g for g in groups if g["group_type"] == "pr"]
        self.assertEqual(len(pr_groups), 1)               # find_or_create_by dedup
        # "#12" is a substring of "#123" — ported quirk (commit_grouper.rb:52)
        self.assertEqual(pr_groups[0]["commit_shas"], ["c1"])
        self.assertEqual(pr_groups[0]["title"], "PR #12")
        self.assertEqual(pr_groups[0]["branch"], "feat/x")  # first session wins
        # c1 is claimed by the PR group → no single_commit group remains
        self.assertEqual(len(groups), 1)

    def test_single_commit_title_truncates_at_100(self):
        commits = [commit_stub("c1", "2026-06-01T10:00:00+00:00", "x" * 120)]
        groups = gitdata.group_commits(commits, {}, [])
        self.assertEqual(groups[0]["title"], "x" * 97 + "...")
        self.assertEqual(groups[0]["earliest_commit_at"],
                         groups[0]["latest_commit_at"])

    def test_loc_stats_full_partial_and_diff_precedence(self):
        numstat = {"c1": {"added": 5, "deleted": 2}, "c2": {"added": 1, "deleted": 1}}
        g = gitdata._new_group("commit_cluster", ["c1", "c2"])
        gitdata.assign_loc_stats(g, ["c1", "c2"], numstat)
        self.assertEqual((g["insertions"], g["deletions"]), (6, 3))  # complete → sum

        g = gitdata._new_group("commit_cluster", ["c1", "c3"])
        gitdata.assign_loc_stats(g, ["c1", "c3"], numstat)  # no diff → partial sum
        self.assertEqual((g["insertions"], g["deletions"]), (5, 2))

        g = gitdata._new_group("commit_cluster", ["c1", "c3"])
        diff = "+++ b/a.py\n+x\n+y\n-z\n"
        gitdata.assign_loc_stats(g, ["c1", "c3"], numstat, diff)  # partial+diff → diff
        self.assertEqual((g["insertions"], g["deletions"]), (2, 1))

        g = gitdata._new_group("single_commit", ["c9"])
        gitdata.assign_loc_stats(g, ["c9"], numstat)  # nothing → column default 0
        self.assertEqual((g["insertions"], g["deletions"]), (0, 0))


# --- LinkingStrategy / EpisodeLinker ---

class LinkingTests(unittest.TestCase):
    def make_group(self, **kw):
        g = gitdata._new_group(kw.pop("group_type", "single_commit"),
                               kw.pop("commit_shas", []), **{k: kw[k] for k in
                               ("title", "branch", "pr_number") if k in kw})
        g["id"] = 1
        g["earliest_commit_at"] = kw.get("earliest_commit_at")
        g["latest_commit_at"] = kw.get("latest_commit_at")
        return g

    def test_link_priority_order(self):
        g = self.make_group(commit_shas=["abc"], branch="feat/x", pr_number=7,
                            earliest_commit_at=dt("2026-06-01T10:00:00+00:00"),
                            latest_commit_at=dt("2026-06-01T10:00:00+00:00"))
        s = {"session_id": "s", "pr_number": 7, "event_git_shas": ["abc"],
             "git_branch": "feat/x",
             "session_created_at": "2026-06-01T09:00:00+00:00",
             "session_modified_at": "2026-06-01T11:00:00+00:00"}
        self.assertEqual(gitdata.best_link(g, s), ("pr_match", 1.0))
        s["pr_number"] = None
        self.assertEqual(gitdata.best_link(g, s), ("sha_match", 0.9))
        s["event_git_shas"] = []
        self.assertEqual(gitdata.best_link(g, s), ("branch_match", 0.7))
        s["git_branch"] = None
        self.assertEqual(gitdata.best_link(g, s), ("timestamp_overlap", 0.5))
        s["session_created_at"] = s["session_modified_at"] = None
        self.assertIsNone(gitdata.best_link(g, s))

    def test_event_branches_last_preferred_over_git_branch(self):
        g = self.make_group(group_type="pr", branch="feat/x", pr_number=7)
        s = {"session_id": "s", "git_branch": "main",
             "event_branches": ["main", "feat/x"]}
        self.assertEqual(gitdata.best_link(g, s), ("branch_match", 0.7))

    def test_timestamp_overlap_one_hour_boundary_inclusive(self):
        g = self.make_group(earliest_commit_at=dt("2026-06-01T15:00:00+00:00"),
                            latest_commit_at=dt("2026-06-01T15:00:00+00:00"))
        s = {"session_created_at": "2026-06-01T13:00:00+00:00",
             "session_modified_at": "2026-06-01T14:00:00+00:00"}
        self.assertTrue(gitdata.timestamp_overlap(g, s))   # exactly +1h → inclusive
        s["session_modified_at"] = "2026-06-01T13:59:59+00:00"
        self.assertFalse(gitdata.timestamp_overlap(g, s))  # 1s past tolerance
        # leading edge: commit 1h before session start
        g2 = self.make_group(earliest_commit_at=dt("2026-06-01T12:00:00+00:00"),
                             latest_commit_at=dt("2026-06-01T12:00:00+00:00"))
        self.assertTrue(gitdata.timestamp_overlap(g2, s))

    def test_session_time_range_falls_back_to_git_commit_events(self):
        s = {"events": [
            {"type": "git_commit", "sha": "a", "timestamp": "2026-06-01T10:00:00+00:00"},
            {"type": "git_commit", "sha": "b", "timestamp": "2026-06-01T12:00:00+00:00"},
        ]}
        start, end = gitdata.session_time_range(s)
        self.assertEqual(start, dt("2026-06-01T10:00:00+00:00"))
        self.assertEqual(end, dt("2026-06-01T12:00:00+00:00"))

    def test_classify_episode_rules_and_quirks(self):
        cases = [("fix crash", "bugfix"),
                 ("Fixes the thing", "bugfix"),
                 ("big refactor of core", "refactor"),
                 ("feat: new flow", "feature"),
                 ("Add tests", "feature"),
                 ("saddle the horse", "feature"),          # "add" substring quirk
                 ("deploy pipeline", "infrastructure"),
                 ("infra tweak", "infrastructure"),
                 ("precise timing", "infrastructure"),     # "ci" substring quirk
                 ("2 commits: fix crash", "implementation"),  # cluster prefix quirk
                 (None, "implementation"),
                 ("misc work", "implementation")]
        for title, expected in cases:
            self.assertEqual(gitdata.classify_episode(title), expected, msg=str(title))

    def test_session_links_to_first_matching_group_only(self):
        # Session SHAs intersect BOTH groups — only the first (creation-order)
        # group claims it (episode_linker.rb:56 matched_sessions check).
        g1 = self.make_group(commit_shas=["aaa"]); g1["id"] = 1
        g2 = self.make_group(commit_shas=["bbb"]); g2["id"] = 2
        s = {"session_id": "s1", "event_git_shas": ["aaa", "bbb"]}
        episodes = gitdata.link_episodes([g1, g2], [s])
        self.assertEqual(episodes[0]["session_ids"], ["s1"])
        self.assertEqual(episodes[1]["session_ids"], [])
        self.assertEqual(episodes[0]["confidence"], 0.9)
        self.assertEqual(episodes[1]["confidence"], 0.5)   # commit-only
        # no session_only episode — the session was claimed
        self.assertEqual(len(episodes), 2)

    def test_orphan_session_becomes_session_only(self):
        episodes = gitdata.link_episodes([], [{"session_id": "lonely"}])
        self.assertEqual(len(episodes), 1)
        ep = episodes[0]
        self.assertEqual(ep["episode_type"], "session_only")
        self.assertEqual(ep["confidence"], 0.3)
        self.assertEqual(ep["session_ids"], ["lonely"])
        self.assertEqual(ep["links"], [{"session_id": "lonely", "link_type": None,
                                        "link_confidence": 0.3}])
        self.assertEqual(ep["added_lines"], 0)

    def test_subagent_and_triggered_sessions_excluded(self):
        g = self.make_group(commit_shas=["aaa"]); g["id"] = 1
        sessions = [{"session_id": "sub", "is_subagent": True, "event_git_shas": ["aaa"]},
                    {"session_id": "child", "triggered_by_id": "p", "event_git_shas": ["aaa"]}]
        episodes = gitdata.link_episodes([g], sessions)
        self.assertEqual(len(episodes), 1)                 # no session_only either
        self.assertEqual(episodes[0]["session_ids"], [])

    def test_episode_confidence_is_mean_of_link_confidences(self):
        g = self.make_group(group_type="pr", commit_shas=["aaa"],
                            branch="feat/x", pr_number=5)
        g["id"] = 1
        g["insertions"], g["deletions"] = 10, 4
        sessions = [{"session_id": "s1", "pr_number": 5},
                    {"session_id": "s2", "git_branch": "feat/x"}]
        episodes = gitdata.link_episodes([g], sessions)
        ep = episodes[0]
        self.assertEqual(ep["confidence"], 0.85)           # mean(1.0, 0.7)
        self.assertEqual(ep["added_lines"], 10)            # LOC rollup from group
        self.assertEqual(ep["deleted_lines"], 4)
        self.assertEqual(ep["commit_group_ids"], [1])


# --- end-to-end: real git repo + CLI ---

class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gitdata_test_")
        cls.repo = os.path.join(cls.tmp, "repo")
        os.makedirs(cls.repo)
        init_repo(cls.repo)
        cls.sha_a = add_commit(cls.repo, "fix: crash on save",
                               "2026-06-01T10:00:00+00:00", lines=3)
        cls.sha_b = add_commit(cls.repo, "wip more work",
                               "2026-06-01T12:00:00+00:00", lines=2)   # exactly 2h → cluster
        cls.sha_c = add_commit(cls.repo, "polish edges",
                               "2026-06-01T16:00:00+00:00", lines=4)   # 4h gap → single
        cls.sha_d = add_commit(cls.repo, "Merge pull request #12 from me/feat",
                               "2026-06-02T10:00:00+00:00", lines=5)

        sessions = [
            {"session_id": "s-pr", "pr_number": 12, "git_branch": "feat/pr-branch",
             "session_created_at": "2026-06-02T09:00:00+00:00",
             "session_modified_at": "2026-06-02T10:30:00+00:00"},
            {"session_id": "s-sha", "event_git_shas": [cls.sha_c]},
            {"session_id": "s-branch", "event_branches": ["main", "feat/pr-branch"]},
            {"session_id": "s-time",
             "session_created_at": "2026-06-01T15:30:00+00:00",
             "session_modified_at": "2026-06-01T15:45:00+00:00"},
            {"session_id": "s-orphan",
             "session_created_at": "2026-05-25T10:00:00+00:00",
             "session_modified_at": "2026-05-25T10:05:00+00:00"},
        ]
        cls.sessions_path = os.path.join(cls.tmp, "sessions.jsonl")
        with open(cls.sessions_path, "w") as f:
            for s in sessions:
                f.write(json.dumps(s) + "\n")

        cls.out_path = os.path.join(cls.tmp, "out.json")
        res = subprocess.run(
            [sys.executable, GITDATA_PY, "--repo", cls.repo,
             "--sessions", cls.sessions_path, "--out", cls.out_path],
            capture_output=True)
        assert res.returncode == 0, res.stderr.decode()
        with open(cls.out_path) as f:
            cls.result = json.load(f)
        cls.groups = {g["group_type"]: g for g in cls.result["commit_groups"]}
        cls.episodes_by_group = {tuple(e["commit_group_ids"]): e
                                 for e in cls.result["episodes"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def episode_for(self, group):
        return self.episodes_by_group[(group["id"],)]

    def test_group_shapes_and_order(self):
        groups = self.result["commit_groups"]
        self.assertEqual([g["group_type"] for g in groups],
                         ["pr", "commit_cluster", "single_commit"])
        pr, cluster, single = groups
        self.assertEqual(pr["commit_shas"], [self.sha_d])
        self.assertEqual(pr["pr_number"], 12)
        self.assertEqual(pr["branch"], "feat/pr-branch")
        self.assertEqual(sorted(cluster["commit_shas"]),
                         sorted([self.sha_a, self.sha_b]))
        self.assertEqual(cluster["title"], "2 commits: fix: crash on save")
        self.assertEqual(single["commit_shas"], [self.sha_c])
        self.assertEqual(single["title"], "polish edges")

    def test_loc_rollup_from_numstat(self):
        self.assertEqual(self.groups["pr"]["insertions"], 5)
        self.assertEqual(self.groups["commit_cluster"]["insertions"], 5)   # 3 + 2
        self.assertEqual(self.groups["single_commit"]["insertions"], 4)
        self.assertEqual(self.groups["single_commit"]["deletions"], 0)
        ep = self.episode_for(self.groups["commit_cluster"])
        self.assertEqual(ep["added_lines"], 5)

    def test_commit_dates_round_trip(self):
        cluster = self.groups["commit_cluster"]
        self.assertEqual(gitdata.parse_dt(cluster["earliest_commit_at"]),
                         dt("2026-06-01T10:00:00+00:00"))
        self.assertEqual(gitdata.parse_dt(cluster["latest_commit_at"]),
                         dt("2026-06-01T12:00:00+00:00"))

    def test_pr_episode_links_pr_match_and_branch_match(self):
        ep = self.episode_for(self.groups["pr"])
        links = {l["session_id"]: l for l in ep["links"]}
        self.assertEqual(links["s-pr"]["link_type"], "pr_match")
        self.assertEqual(links["s-pr"]["link_confidence"], 1.0)
        self.assertEqual(links["s-branch"]["link_type"], "branch_match")
        self.assertEqual(links["s-branch"]["link_confidence"], 0.7)
        self.assertEqual(ep["confidence"], 0.85)            # mean(1.0, 0.7)
        self.assertEqual(ep["episode_type"], "implementation")  # "PR #12" title

    def test_single_episode_links_sha_and_timestamp(self):
        ep = self.episode_for(self.groups["single_commit"])
        links = {l["session_id"]: l for l in ep["links"]}
        self.assertEqual(links["s-sha"]["link_type"], "sha_match")
        self.assertEqual(links["s-time"]["link_type"], "timestamp_overlap")
        self.assertEqual(ep["confidence"], 0.7)             # mean(0.9, 0.5)

    def test_cluster_episode_is_commit_only(self):
        ep = self.episode_for(self.groups["commit_cluster"])
        self.assertEqual(ep["links"], [])
        self.assertEqual(ep["confidence"], 0.5)
        # "2 commits: fix..." does not start with "fix" → implementation quirk
        self.assertEqual(ep["episode_type"], "implementation")

    def test_orphan_session_only_episode(self):
        session_only = [e for e in self.result["episodes"]
                        if e["episode_type"] == "session_only"]
        self.assertEqual(len(session_only), 1)
        self.assertEqual(session_only[0]["session_ids"], ["s-orphan"])
        self.assertEqual(session_only[0]["confidence"], 0.3)

    def test_default_since_is_seven_days_before_earliest_session(self):
        sessions = gitdata.load_sessions(self.sessions_path)
        since = gitdata.default_since(sessions)
        self.assertEqual(gitdata.parse_dt(since), dt("2026-05-18T10:00:00+00:00"))
        self.assertIsNone(gitdata.default_since([{"session_id": "x"}]))


class AuthorFilterTests(unittest.TestCase):
    def test_author_filter_prefers_filtered_commits_and_numstat(self):
        tmp = tempfile.mkdtemp(prefix="gitdata_author_")
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            init_repo(repo)
            mine = add_commit(repo, "my change", "2026-06-01T10:00:00+00:00", lines=2)
            add_commit(repo, "their change", "2026-06-01T11:00:00+00:00", lines=9,
                       author=("Someone Else", "other+x@example.com"))

            commits, numstat = gitdata.collect_git_data(
                repo, None, author_emails=["test@example.com"])
            self.assertEqual([c["sha"] for c in commits], [mine])
            self.assertEqual(set(numstat), {mine})          # SHA-aligned pair

            # no filter → both commits
            commits_all, _ = gitdata.collect_git_data(repo, None)
            self.assertEqual(len(commits_all), 2)

            # filter matching nobody → falls back to unfiltered (rb:31-34)
            commits_fb, _ = gitdata.collect_git_data(
                repo, None, author_emails=["ghost@nowhere.com"])
            self.assertEqual(len(commits_fb), 2)

            # PORTED UPLOADER QUIRK: the sed escape class turns "+" into
            # "\+", which git's basic-regex --author treats as a repetition
            # operator — an email containing "+" matches NOTHING, so the
            # author-filtered log is empty and we fall back to unfiltered
            # (verified against real git; same outcome as upload.sh +
            # commit_grouper.rb:31-34 on the Rails side).
            commits_esc, _ = gitdata.collect_git_data(
                repo, None, author_emails=["other+x@example.com"])
            self.assertEqual(len(commits_esc), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CommitsPayloadTest(unittest.TestCase):
    def test_sha_and_date_only_order_preserved(self):
        commits = [
            {"sha": "a" * 40, "short": "a" * 7, "author": "A", "email": "a@x",
             "date": "2026-06-01T23:10:00+05:30", "subject": "feat: x"},
            {"sha": "b" * 40, "short": "b" * 7, "author": "B", "email": "b@x",
             "date": "2026-05-30T09:00:00-07:00", "subject": "fix: y"},
        ]
        self.assertEqual(gitdata.commits_payload(commits), [
            {"sha": "a" * 40, "date": "2026-06-01T23:10:00+05:30"},
            {"sha": "b" * 40, "date": "2026-05-30T09:00:00-07:00"},
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)

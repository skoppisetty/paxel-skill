#!/usr/bin/env python3
"""
gitdata.py — client-side commit grouping + episode linking over a local git repo.

Faithful port of Paxel's pipeline:
  CommitGrouper      (app/services/commit_grouper.rb)
  EpisodeLinker      (app/services/episode_linker.rb)
  LinkingStrategy    (app/models/concerns/linking_strategy.rb)
  PrDiffStatsService (app/services/pr_diff_stats_service.rb)
plus the uploader's git collection (upload.sh) and ClientPipeline's parsers
(parse_commits_tsv ~1252, parse_numstat_file ~1346) so the data shapes match
what the Rails side sees.

STAGE 1 — collection. Paxel's uploader writes TSV/numstat files that Rails
parses; we run the same git commands directly:
    git log -1000 --since=<since> --format='%H%x09%h%x09%aN%x09%aE%x09%aI%x09%s'
    git log -1000 --since=<since> --format='COMMIT_BOUNDARY %H %aI %aN <%aE>' --numstat
(-1000 = the uploader's COMMIT_LIMIT default on the unfiltered log; the
author-filtered log has NO count cap, only --since — both mirrored from
upload.sh.) When --author-name/--author-email are given we also collect the
author-filtered pair and, exactly like commit_grouper.rb:27-35, prefer the
filtered commits AND filtered numstat together (SHA alignment) whenever the
filtered commit list is non-empty.

Default --since: 7 days before the earliest session_created_at in
sessions.jsonl. Rationale: Paxel's uploader bounds git collection to the
session window (oldest session date) so the whole repo history never ships;
the 7-day back-pad keeps commits that landed shortly before the first
captured session linkable by timestamp/branch without pulling the full
history. If no session carries a parseable session_created_at, no --since is
passed and only the -1000 cap bounds the log.

DELIBERATE DEVIATIONS (everything else is verbatim):
  * No local diffs: the Rails CommitGrouper can read per-commit diff bodies
    (project.commit_diffs) and falls back to PrDiffStatsService.parse(diff)
    when numstat only partially covers a group's SHAs. We never collect
    diffs, so combine_diffs() always returns None and assign_loc_stats falls
    through to the partial-numstat sum (the Ruby's third branch). The diff
    branch and parse_pr_diff_stats() are still ported for completeness.
  * --author-name is passed to git as an escaped --author=<regex> directly;
    the uploader instead resolves names to emails by scanning git log and
    then filters by <email> only. Emails are anchored as --author='<email>'
    with the uploader's exact sed escape class ([.+*?^$[\\]\\]).
  * Sessions come from sessions.jsonl (scripts/events.py output), not
    ActiveRecord rows. Logical-root filtering (episode_linker.rb:25-27)
    skips records whose is_subagent / triggered_by_id fields are truthy when
    present; events.py output may simply not carry them.
  * One-shot run: the Ruby find_or_create_by!/destroy_all idempotency
    machinery is unnecessary, but its visible effect — one group per
    pr_number — is preserved.

PORTED QUIRKS (kept on purpose — they are what Paxel actually computes):
  * PR SHA matching is a substring include of "#<pr>", so PR #12 also
    captures "#123" subjects (commit_grouper.rb:52).
  * Cluster/single groups get branch from the commit hash's "branch" key,
    which the TSV never carries → branch is always null for them; only PR
    groups (branch = session.git_branch) can branch_match.
  * A session links to AT MOST ONE episode: episode_linker.rb:55-70 iterates
    groups in creation order (pr → commit_cluster → single_commit) and skips
    sessions already in matched_sessions — first matching group wins, NOT
    the best match across groups. Multiple sessions CAN link to one episode.
  * classify_episode's "ci"/"add" checks are bare substring includes
    ("precise" → infrastructure, "saddle" → feature); cluster titles start
    with "<n> commits: …" so start_with?("fix"/"feat") never fires for them.
  * Cluster gap is chained commit-to-commit, inclusive at exactly 2h
    (commit_grouper.rb:90: abs(delta) <= CLUSTER_GAP).
  * The uploader's sed escape class turns "+" into "\\+", which git's
    basic-regex --author treats as a repetition operator — an author email
    containing "+" matches nothing, the filtered log comes back empty, and
    collection falls back to the unfiltered pair (verified against real git;
    commit_grouper.rb:31-34 does the same fallback server-side).

Usage:
  python3 gitdata.py --repo <path> --sessions <sessions.jsonl>
      [--author-name N ...] [--author-email E ...] [--since ISO] [--out out.json]
Output JSON: {"commit_groups": [...], "episodes": [...], "commits": [...]}
"""
import argparse
import decimal
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# --- constants (verbatim from the Ruby) ---
CLUSTER_GAP = 2 * 3600            # commit_grouper.rb: CLUSTER_GAP = 2.hours
TIMESTAMP_TOLERANCE = 3600        # linking_strategy.rb: TIMESTAMP_TOLERANCE = 1.hour
COMMIT_LIMIT = 1000               # upload.sh: -${COMMIT_LIMIT:-1000} on the unfiltered log
SINCE_LOOKBACK_DAYS = 7           # see module docstring (collection-window choice)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)  # Ruby Time.at(0) sort fallback

COMMITS_FORMAT = "%H%x09%h%x09%aN%x09%aE%x09%aI%x09%s"
NUMSTAT_FORMAT = "COMMIT_BOUNDARY %H %aI %aN <%aE>"

# pr_diff_stats_service.rb:2 TEST_PATH_PATTERN (verbatim)
TEST_PATH_PATTERN = re.compile(
    r"(?:test|spec|__tests__|_test\.go|_test\.rb|\.test\.|\.spec\.)", re.IGNORECASE)


def truncate(text, length, omission="..."):
    """Rails String#truncate: total result length <= length, hard cut."""
    if text is None:
        return None
    text = str(text)
    if len(text) <= length:
        return text
    return text[: length - len(omission)] + omission


def round_half_up(x, ndigits=2):
    """Ruby Float#round: half away from zero on the exact binary double.
    Decimal(float) is binary-exact, so e.g. 2.675 (stored 2.67499...) rounds
    to 2.67 exactly as Ruby does (same algorithm as analytics.ruby_round)."""
    q = decimal.Decimal(1).scaleb(-ndigits)
    return float(decimal.Decimal(x).quantize(q,
                                             rounding=decimal.ROUND_HALF_UP))


def parse_dt(value):
    """CommitGrouper#safe_parse_date: nil on blank/unparseable, never raises.

    Ruby's DateTime.parse treats a zone-less timestamp as UTC; mirror that so
    naive and aware values stay comparable.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt):
    return dt.isoformat() if dt is not None else None


# --- STAGE 1: git collection (upload.sh + client_pipeline.rb parsers) ---

def run_git(repo, args):
    """Run git, '' on any failure — the uploader appends `|| true`."""
    try:
        res = subprocess.run(["git", "-C", repo] + list(args), capture_output=True)
    except OSError:
        return ""
    if res.returncode != 0:
        return ""
    return res.stdout.decode("utf-8", errors="replace")


def parse_commits_tsv(content):
    """ClientPipeline#parse_commits_tsv: fixed-order TSV, subject LAST so raw
    %s quotes/backslashes can't corrupt the line; split limit keeps stray
    tabs inside the subject; <6 fields = malformed, dropped."""
    commits = []
    for line in (content or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 5)
        if len(parts) < 6:
            continue
        commits.append({"sha": parts[0], "short": parts[1], "author": parts[2],
                        "email": parts[3], "date": parts[4], "subject": parts[5]})
    return commits


def parse_numstat_file(content):
    """ClientPipeline#parse_numstat_file: COMMIT_BOUNDARY format, binary
    '-' counts as 0, per-file rows accumulated under the current SHA."""
    numstat = {}
    current = None
    for line in (content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("COMMIT_BOUNDARY"):
            rest = line.replace("COMMIT_BOUNDARY ", "", 1)
            parts = rest.split(" ", 2)
            sha = parts[0]
            date = parts[1] if len(parts) > 1 else None
            author_part = parts[2] if len(parts) > 2 else ""
            m = re.match(r"\A(.+?)\s+<([^>]+)>\Z", author_part)
            author, email = (m.group(1), m.group(2)) if m else (author_part, None)
            current = {"added": 0, "deleted": 0, "date": date,
                       "author": author, "email": email, "files": []}
            numstat[sha] = current
        elif current is not None:
            m = re.match(r"\A(\d+|-)\t(\d+|-)\t(.+)\Z", line)
            if not m:
                continue
            added = 0 if m.group(1) == "-" else int(m.group(1))
            deleted = 0 if m.group(2) == "-" else int(m.group(2))
            current["added"] += added
            current["deleted"] += deleted
            current["files"].append({"file": m.group(3), "added": added, "deleted": deleted})
    return numstat


def _author_regex_escape(value):
    """upload.sh: sed 's/[.+*?^$[\\]\\\\]/\\\\&/g' — exact char class."""
    return re.sub(r"([.+*?^$\[\]\\])", r"\\\1", value)


def author_flags(author_names, author_emails):
    """--author regex flags; emails are anchored as '<email>' to avoid
    partial matches (upload.sh collect_author_commits)."""
    flags = []
    for email in author_emails or []:
        if email:
            flags.append("--author=<%s>" % _author_regex_escape(email))
    for name in author_names or []:
        if name:
            flags.append("--author=%s" % _author_regex_escape(name))
    return flags


def collect_git_data(repo, since, author_names=(), author_emails=()):
    """Collect (commits, numstat). Mirrors commit_grouper.rb:27-35: prefer the
    author-filtered pair (wider session date coverage) when non-empty, and
    switch commits AND numstat together to keep SHA alignment."""
    since_args = ["--since=%s" % since] if since else []
    base = ["log", "-%d" % COMMIT_LIMIT] + since_args
    commits = parse_commits_tsv(run_git(repo, base + ["--format=%s" % COMMITS_FORMAT]))
    numstat = parse_numstat_file(
        run_git(repo, base + ["--format=%s" % NUMSTAT_FORMAT, "--numstat"]))

    flags = author_flags(author_names, author_emails)
    if flags:
        # Author-filtered log: --since only, no count cap (upload.sh:4710-4716).
        author_base = ["log"] + flags + since_args
        author_commits = parse_commits_tsv(
            run_git(repo, author_base + ["--format=%s" % COMMITS_FORMAT]))
        if author_commits:
            author_numstat = parse_numstat_file(
                run_git(repo, author_base + ["--format=%s" % NUMSTAT_FORMAT, "--numstat"]))
            return author_commits, author_numstat
    return commits, numstat


# --- PrDiffStatsService.parse (verbatim; unused on this no-diff path but
# ported for completeness — see module docstring) ---

def parse_pr_diff_stats(diff_text):
    if not diff_text:
        return {}
    insertions = deletions = test_insertions = test_deletions = 0
    files = set()
    current_file_is_test = False
    for line in diff_text.splitlines():
        m = re.match(r"\+\+\+ b/(.+)", line)
        if m:
            file_path = m.group(1)
            files.add(file_path)
            current_file_is_test = bool(TEST_PATH_PATTERN.search(file_path))
        elif re.match(r"\+(?!\+\+)", line):
            insertions += 1
            if current_file_is_test:
                test_insertions += 1
        elif re.match(r"-(?!--)", line):
            deletions += 1
            if current_file_is_test:
                test_deletions += 1
    ratio = round_half_up(test_insertions / insertions, 3) if insertions > 0 else 0.0
    return {"insertions": insertions, "deletions": deletions,
            "net_loc": insertions - deletions, "files_changed": len(files),
            "test_insertions": test_insertions, "test_deletions": test_deletions,
            "test_loc_ratio": ratio}


# --- STAGE 2: CommitGrouper port ---

def _new_group(group_type, shas, title=None, branch=None, pr_number=None):
    # insertions/deletions default 0 = the commit_groups column default
    # (client_schema.rb:43,47); Ruby leaves them unassigned when neither
    # numstat nor a diff covers the group.
    return {"id": None, "group_type": group_type, "commit_shas": list(shas),
            "title": title, "branch": branch, "pr_number": pr_number,
            "insertions": 0, "deletions": 0,
            "earliest_commit_at": None, "latest_commit_at": None}


def _parse_commit_dates(shas, commits):
    """CommitGrouper#parse_commit_dates: parseable dates only, sorted."""
    by_sha = {c["sha"]: c for c in commits}
    dates = [parse_dt(by_sha[sha].get("date")) for sha in shas if sha in by_sha]
    return sorted(d for d in dates if d is not None)


def _combine_diffs(shas, commit_diffs):
    """CommitGrouper#combine_diffs — always None here (no local diffs)."""
    parts = [commit_diffs[sha] for sha in shas if commit_diffs.get(sha)]
    if not parts:
        return None
    return "\n\n".join(parts)


def assign_loc_stats(group, shas, numstat, diff=None):
    """CommitGrouper#assign_loc_stats precedence: complete numstat → sum;
    else diff parse; else partial numstat sum. With no local diffs the middle
    branch never fires, so partial coverage sums what numstat has."""
    matched = [numstat[sha] for sha in shas if sha in numstat]
    if matched and len(matched) == len(shas):
        group["insertions"] = sum(int(m.get("added") or 0) for m in matched)
        group["deletions"] = sum(int(m.get("deleted") or 0) for m in matched)
    elif diff:
        stats = parse_pr_diff_stats(diff)
        group["insertions"] = stats.get("insertions") or 0
        group["deletions"] = stats.get("deletions") or 0
    elif matched:
        group["insertions"] = sum(int(m.get("added") or 0) for m in matched)
        group["deletions"] = sum(int(m.get("deleted") or 0) for m in matched)


def group_commits(commits, numstat, sessions, commit_diffs=None):
    """CommitGrouper#group!: pr groups → time-gap clusters → single commits.

    `sessions` is ALL sessions (the Ruby grouper does not exclude subagents;
    only the linker does). Returns groups in creation order — that order is
    load-bearing for the linker's first-match-wins session claiming.
    """
    commit_diffs = commit_diffs or {}
    groups = []
    grouped = set()

    # 1) PR groups (commit_grouper.rb:45-73). find_or_create_by!(pr_number)
    # → one group per pr_number, first session wins the branch/title; later
    # sessions with the same PR still merge their (identical) shas.
    seen_pr = set()
    for session in sessions:
        pr_number = session.get("pr_number")
        if pr_number in (None, ""):
            continue
        pr_shas = [c["sha"] for c in commits
                   if c.get("subject") is not None and ("#%s" % pr_number) in c["subject"]]
        if pr_number not in seen_pr:
            seen_pr.add(pr_number)
            dates = _parse_commit_dates(pr_shas, commits)
            g = _new_group("pr", pr_shas, title="PR #%s" % pr_number,
                           branch=session.get("git_branch"), pr_number=pr_number)
            assign_loc_stats(g, pr_shas, numstat, _combine_diffs(pr_shas, commit_diffs))
            g["earliest_commit_at"] = dates[0] if dates else None
            g["latest_commit_at"] = dates[-1] if dates else None
            groups.append(g)
        grouped.update(pr_shas)

    # 2) Time-gap clusters (commit_grouper.rb:75-121). Gap is chained
    # commit→commit, inclusive at CLUSTER_GAP; an unparseable date breaks the
    # chain (within_gap nil in Ruby); only clusters of size > 1 are kept.
    ungrouped = [c for c in commits if c["sha"] not in grouped]
    if ungrouped:
        sorted_commits = sorted(ungrouped, key=lambda c: parse_dt(c.get("date")) or EPOCH)
        clusters = []
        current = [sorted_commits[0]]
        for commit in sorted_commits[1:]:
            prev_date = parse_dt(current[-1].get("date"))
            curr_date = parse_dt(commit.get("date"))
            within_gap = (prev_date is not None and curr_date is not None
                          and abs((curr_date - prev_date).total_seconds()) <= CLUSTER_GAP)
            if within_gap:  # same_branch is hardcoded true in the Ruby
                current.append(commit)
            else:
                if len(current) > 1:
                    clusters.append(current)
                current = [commit]
        if len(current) > 1:
            clusters.append(current)

        for cluster in clusters:
            shas = [c["sha"] for c in cluster]
            dates = _parse_commit_dates(shas, commits)
            subject = cluster[0].get("subject")
            # Ruby interpolation renders a nil subject as "" (rb:106)
            title = "%d commits: %s" % (len(cluster),
                                        truncate(subject, 60) if subject is not None else "")
            # cluster.first["branch"] — never present in the TSV → null
            g = _new_group("commit_cluster", shas, title=title,
                           branch=cluster[0].get("branch"))
            assign_loc_stats(g, shas, numstat, _combine_diffs(shas, commit_diffs))
            g["earliest_commit_at"] = dates[0] if dates else None
            g["latest_commit_at"] = dates[-1] if dates else None
            groups.append(g)
            grouped.update(shas)

    # 3) Single commits (commit_grouper.rb:123-142), in git-log order
    # (newest first) like @commits.
    for commit in commits:
        if commit["sha"] in grouped:
            continue
        date = parse_dt(commit.get("date"))
        subject = commit.get("subject")
        g = _new_group("single_commit", [commit["sha"]],
                       title=truncate(subject, 100) if subject is not None else None,
                       branch=commit.get("branch"))
        assign_loc_stats(g, [commit["sha"]], numstat,
                         commit_diffs.get(commit["sha"]))
        g["earliest_commit_at"] = date
        g["latest_commit_at"] = date
        groups.append(g)

    for i, g in enumerate(groups, 1):
        g["id"] = i
    return groups


# --- STAGE 3: EpisodeLinker + LinkingStrategy port ---

def _events_of_type(session, event_type):
    events = session.get("events") or []
    return [e for e in events if isinstance(e, dict) and e.get("type") == event_type]


def session_event_git_shas(session):
    """transcript_session.rb#event_git_shas; prefer events.py's precomputed
    field, derive from events otherwise (same data)."""
    value = session.get("event_git_shas")
    if isinstance(value, list):
        return [s for s in value if s]
    return [e.get("sha") for e in _events_of_type(session, "git_commit") if e.get("sha")]


def session_event_branches(session):
    """transcript_session.rb#event_branches (git_branch_switch events)."""
    value = session.get("event_branches")
    if isinstance(value, list):
        return [b for b in value if b]
    return [e.get("branch") for e in _events_of_type(session, "git_branch_switch")
            if e.get("branch")]


def session_time_range(session):
    """linking_strategy.rb#session_time_range priority: created/modified pair
    → git_commit event timestamps → git_commits timestamps → (None, None)."""
    created = parse_dt(session.get("session_created_at"))
    modified = parse_dt(session.get("session_modified_at"))
    if created is not None and modified is not None:
        return created, modified

    event_ts = sorted(t for t in (parse_dt(e.get("timestamp"))
                                  for e in _events_of_type(session, "git_commit"))
                      if t is not None)
    if event_ts:
        return event_ts[0], event_ts[-1]

    commit_ts = sorted(t for t in (parse_dt(c.get("timestamp"))
                                   for c in session.get("git_commits") or []
                                   if isinstance(c, dict))
                       if t is not None)
    if commit_ts:
        return commit_ts[0], commit_ts[-1]
    return None, None


def timestamp_overlap(group, session):
    """linking_strategy.rb#timestamp_overlap?: session range padded ±1h,
    inclusive interval intersection with the group's commit range."""
    session_start, session_end = session_time_range(session)
    if session_start is None or session_end is None:
        return False
    commit_start = group.get("earliest_commit_at") or group.get("latest_commit_at")
    commit_end = group.get("latest_commit_at") or commit_start
    if commit_start is None:
        return False
    tolerance = timedelta(seconds=TIMESTAMP_TOLERANCE)
    return (session_start - tolerance <= commit_end
            and commit_start <= session_end + tolerance)


def best_link(group, session):
    """linking_strategy.rb#best_link 4-tier priority → (link_type, confidence)
    or None."""
    # Priority 1: PR match
    if group.get("pr_number") not in (None, "") and session.get("pr_number") == group["pr_number"]:
        return "pr_match", 1.0

    # Priority 2: SHA match — prefer session events, fallback to git_commits
    source_shas = group.get("commit_shas") or []
    session_shas = session_event_git_shas(session)
    if not session_shas:
        session_shas = [c.get("sha") if isinstance(c, dict) else str(c)
                        for c in session.get("git_commits") or []]
    if session_shas and set(source_shas) & set(session_shas):
        return "sha_match", 0.9

    # Priority 3: Branch match — prefer event_branches.last, fallback git_branch
    source_branch = group.get("branch")
    branches = session_event_branches(session)
    session_branch = branches[-1] if branches else None
    session_branch = session_branch or session.get("git_branch")
    if source_branch and session_branch and source_branch == session_branch:
        return "branch_match", 0.7

    # Priority 4: Timestamp overlap
    if timestamp_overlap(group, session):
        return "timestamp_overlap", 0.5
    return None


def classify_episode(title):
    """episode_linker.rb:95-108 — exact rule order; bare substring quirks
    ("ci", "add") are intentional."""
    title = (title or "").lower()
    if title.startswith("fix"):
        return "bugfix"
    if "refactor" in title:
        return "refactor"
    if title.startswith("feat") or "add" in title:
        return "feature"
    if "infra" in title or "ci" in title or "deploy" in title:
        return "infrastructure"
    return "implementation"


def link_episodes(groups, sessions):
    """EpisodeLinker#link!: one episode per commit group (creation order),
    sessions claimed first-match-wins (a session joins at most ONE episode —
    matched_sessions is checked before best_link, episode_linker.rb:56);
    leftovers become session_only episodes.

    added/deleted_lines = sum of the episode's groups' insertions/deletions
    (episode_summarizer.rb:252-253) — a singleton sum here since each commit
    episode carries exactly one group.
    """
    # Logical-root sessions only (episode_linker.rb:25-27)
    root_sessions = [s for s in sessions
                     if not s.get("is_subagent") and not s.get("triggered_by_id")]

    episodes = []
    matched_session_ids = set()

    for group in groups:
        links = []
        confidences = []
        for session in root_sessions:
            session_id = session.get("session_id")
            if session_id in matched_session_ids:
                continue
            result = best_link(group, session)
            if not result:
                continue
            link_type, confidence = result
            links.append({"session_id": session_id, "link_type": link_type,
                          "link_confidence": confidence})
            matched_session_ids.add(session_id)
            confidences.append(confidence)

        confidence = (round_half_up(sum(confidences) / len(confidences), 2)
                      if confidences else 0.5)  # 0.5 = commit-only (rb:72)
        episodes.append({
            "episode_id": len(episodes) + 1,
            "episode_type": classify_episode(group.get("title")),
            "confidence": confidence,
            "session_ids": [l["session_id"] for l in links],
            "commit_group_ids": [group["id"]],
            "added_lines": int(group.get("insertions") or 0),
            "deleted_lines": int(group.get("deletions") or 0),
            "links": links,
        })

    # Orphan sessions → session_only episodes (rb:76-88; link_type nil)
    for session in root_sessions:
        session_id = session.get("session_id")
        if session_id in matched_session_ids:
            continue
        episodes.append({
            "episode_id": len(episodes) + 1,
            "episode_type": "session_only",
            "confidence": 0.3,
            "session_ids": [session_id],
            "commit_group_ids": [],
            "added_lines": 0,
            "deleted_lines": 0,
            "links": [{"session_id": session_id, "link_type": None,
                       "link_confidence": 0.3}],
        })
    return episodes


# --- CLI ---

def load_sessions(path):
    sessions = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                sessions.append(record)
    return sessions


def default_since(sessions):
    """7 days before the earliest session_created_at (see module docstring)."""
    created = sorted(t for t in (parse_dt(s.get("session_created_at")) for s in sessions)
                     if t is not None)
    if not created:
        return None
    return (created[0] - timedelta(days=SINCE_LOOKBACK_DAYS)).isoformat()


def serialize_group(group):
    out = dict(group)
    out["earliest_commit_at"] = iso(group["earliest_commit_at"])
    out["latest_commit_at"] = iso(group["latest_commit_at"])
    return out


def commits_payload(commits):
    """Top-level `commits` output: [{sha, date}] for every collected commit,
    dates as recorded by git log %aI (author-local offset preserved).
    Additive — feeds report.py's Peak-hours/Ship-day cards only."""
    return [{"sha": c["sha"], "date": c["date"]} for c in commits]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Commit grouping + episode linking (Paxel CommitGrouper/EpisodeLinker port).")
    parser.add_argument("--repo", required=True, help="path to the git repository")
    parser.add_argument("--sessions", required=True, help="sessions.jsonl from scripts/events.py")
    parser.add_argument("--author-name", action="append", default=[], dest="author_names")
    parser.add_argument("--author-email", action="append", default=[], dest="author_emails")
    parser.add_argument("--since", default=None,
                        help="ISO date passed to git log --since (default: 7 days "
                             "before the earliest session_created_at)")
    parser.add_argument("--out", default=None, help="output JSON path (default: stdout)")
    args = parser.parse_args(argv)

    sessions = load_sessions(args.sessions)
    since = args.since or default_since(sessions)

    commits, numstat = collect_git_data(args.repo, since,
                                        args.author_names, args.author_emails)
    groups = group_commits(commits, numstat, sessions)
    episodes = link_episodes(groups, sessions)

    result = {"commit_groups": [serialize_group(g) for g in groups],
              "episodes": episodes,
              "commits": commits_payload(commits)}
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
    else:
        sys.stdout.write(payload + "\n")
    sys.stderr.write("[gitdata] %d commits, %d groups, %d episodes (since=%s)\n"
                     % (len(commits), len(groups), len(episodes), since))
    return 0


if __name__ == "__main__":
    sys.exit(main())

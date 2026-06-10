#!/usr/bin/env python3
"""
events.py — typed event + signal extraction from Claude Code .jsonl session logs.

Faithful port of Paxel's client-side transcript event extraction:
  - EventExtractor              (event_extractor.rb)          — typed events
  - SessionSignalExtractor      (session_signal_extractor.rb) — signals
  - TranscriptPatterns          (concerns/transcript_patterns.rb) — shared regexes
  - PlanPatterns                (plan_patterns.rb)             — plan booleans
  - ActiveTimeWindowsCalculator (active_time_windows_calculator.rb)
  - TranscriptChunker           (transcript_chunker.rb)        — message iteration,
    raw-user-text filters, user_highlights, plan-file versioning, dispatch metadata

Reuses condense.py for parsing primitives (scrub/blocks/detect_format/
normalize_codex/iter_paths) so both scripts read sessions identically.

Usage:
  python3 events.py <session.jsonl ...>
Emits one JSON object per session to stdout (JSONL):
  {"session_id","path","first_prompt","session_created_at","session_modified_at",
   "events":[...],"session_signals":{...},"user_highlights","plan_files":[...],
   "active_time_windows":[["iso","iso"],...],"pr_number","git_branch",
   "event_git_shas":[...],"event_branches":[...],"dispatch_metadata":{...}}
"""
import sys, os, re, json, hashlib
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import condense  # noqa: E402 — scrub, blocks, detect_format, normalize_codex, iter_paths

# --- EventExtractor constants (event_extractor.rb:37-58, verbatim) ---
MAX_TEXT_LENGTH = 10_000        # user directives, task prompts, errors
MAX_BASH_LENGTH = 5_000         # piped commands + curl headers fit here
MAX_DESCRIPTION_LENGTH = 200    # short subagent description
MAX_THINKING_LENGTH = 20_000    # thinking blocks run 5-50K
MAX_EVENTS = 3_000
# Case-SENSITIVE on the basename on purpose: a lowercase `plan.md` is an
# ordinary doc, not a deliberate plan artifact (event_extractor.rb:42-48).
PLAN_PATH_PATTERN = re.compile(
    r"(?:\.claude/plans/[^/]+\.md|(?:^|/)(?:[A-Z][A-Z0-9_]*_)?PLAN\.md)\Z")
# Load-bearing signals bypass MAX_EVENTS (event_extractor.rb:53).
STRUCTURAL_EVENT_TYPES = {"subagent_dispatch", "subagent_return", "git_commit"}
SUBAGENT_TOOL_NAME_PATTERN = re.compile(r"(?:Task|Agent|subagent|[Aa]gent_?task)\Z")

# --- TranscriptPatterns (concerns/transcript_patterns.rb, verbatim) ---
# Ruby `^` is always line-anchored (re.M here); Ruby `/m` = dot-matches-newline (re.S).
RSPEC_RESULT = re.compile(r"(\d+)\s+examples?,\s+(\d+)\s+failures?(?:,\s+(\d+)\s+pending)?")
PYTEST_RESULT = re.compile(r"(\d+)\s+passed(?:,\s+(\d+)\s+failed)?")
JEST_RESULT = re.compile(r"Tests:\s+(?:(\d+)\s+failed,\s+)?(\d+)\s+passed")
CARGO_RESULT = re.compile(r"test result:.*?(\d+)\s+passed;\s+(\d+)\s+failed")

GIT_COMMIT_OUTPUT = re.compile(r"\[[\w/.-]+\s+(?:\(root-commit\)\s+)?([0-9a-f]{7,40})\]")
GIT_BRANCH_FROM_COMMIT = re.compile(r"\[([\w/.-]+)\s+(?:\(root-commit\)\s+)?[0-9a-f]{7,40}\]")
GIT_ON_BRANCH = re.compile(r"On branch ([\w/.-]+)")
GIT_SWITCH_BRANCH = re.compile(r"Switched to (?:a new )?branch '([\w/.-]+)'")
GIT_CHECKOUT_CMD = re.compile(r"git (?:checkout|switch)\s+(?:-[bc]\s+)?(\S+)")
GIT_COMMIT_MSG_DOUBLE = re.compile(r'git commit.*?-m\s+"((?:[^"\\]|\\.)*)"')
GIT_COMMIT_MSG_SINGLE = re.compile(r"git commit.*?-m\s+'((?:[^'\\]|\\.)*)'")
GIT_COMMIT_MSG_HEREDOC = re.compile(r"<<'?EOF'?\s*\n(.*?)\n\s*EOF", re.S)
GIT_PUSH = re.compile(r"git push")

ERROR_LINE = re.compile(
    r"(?:Error|Exception|FAILED|Errno|LoadError|NoMethodError|NameError|TypeError"
    r"|ArgumentError|SyntaxError)[:!\s]", re.I)

OPTION_PATTERNS = [
    re.compile(r"(?:option|approach|alternative|choice)\s*(?:\d|[A-C])", re.I),
    re.compile(r"\d+[.)]\s+\*\*[^*]+\*\*"),
    re.compile(r"(?:we could|you could|options are|alternatives):", re.I),
    re.compile(r"(?:here are|there are)\s+(?:\d+|several|a few)\s+(?:options|approaches|ways)", re.I),
    re.compile(r"(?:trade-?off|pros?\s+and\s+cons?|versus|vs\.?)\b", re.I),
]
QUESTION_PATTERNS = [
    re.compile(r"(?:would you (?:like|prefer|rather)|what (?:do you think|approach)"
               r"|should (?:I|we)|how (?:do you want|should))", re.I),
    re.compile(r"(?:which (?:option|approach)|do you want me to)\b", re.I),
]
TRADEOFF_PATTERN = re.compile(r"(?:trade-?off|pros?\s+and\s+cons?|versus|vs\.?)\b", re.I)
OPTION_MARKER = re.compile(r"^\s*(?:\d+[.)]\s|\*\s|-\s|[A-C][.)]\s)", re.M)
OPTION_WORD = re.compile(r"(?:option|approach|alternative)\s*(?:\d|[A-C])", re.I)

# --- PlanPatterns (plan_patterns.rb, verbatim) ---
VERIFICATION_PATTERN = re.compile(r"verif|test|check|confirm|\- \[ \]", re.I)
ALTERNATIVES_PATTERN = re.compile(r"alternativ|option|instead|tradeoff|approach [A-C]", re.I)
EDGE_CASES_PATTERN = re.compile(r"edge.case|corner.case|what.if|fallback|error.handling", re.I)

# --- SessionSignalExtractor constants (session_signal_extractor.rb, verbatim) ---
MAX_TEXT_FOR_REGEX = 20_000
ACTIVE_GAP_MINUTES = 90
SHORT_PROMPT_MAX_WORDS = 8
MAX_REPEATED_PROMPTS = 40
MAX_CHARGED_MESSAGES = 8

CRITIQUE_PATTERNS = re.compile(r"rate me|evaluate|how am i doing|critique|review my|what do you think of my", re.I)
SELF_CORRECTION_PATTERNS = re.compile(r"\bactually\b|wait,|no,\s|let me rethink|scratch that|on second thought", re.I)
KILL_PATTERNS = re.compile(r"\bdelete\b|\bremove\b|\bdrop\b|\bkill\b|get rid of|rip out|revert", re.I)
HYPOTHESIS_PATTERNS = re.compile(r"i think.*because|my theory is|i suspect|probably.*caused by|the issue is likely", re.I)
DOMAIN_CORRECTION_PATTERNS = re.compile(r"that's not how|actually it should|no,.*works like|you're wrong about", re.I)
# 2026-04-23 tightened PRODUCT/ARCHITECTURE patterns (session_signal_extractor.rb:24,32)
PRODUCT_PATTERNS = re.compile(r"\bcustomer\b|\bUX\b|user experience|onboarding|\bfriction\b|pain point|product decision|user need|user research", re.I)
NARRATIVE_PATTERNS = re.compile(r"story|narrative|context|framework|profile|comprehensive|big picture", re.I)
REVIEW_PATTERNS = re.compile(r"looks wrong|check if|verify|doesn't look right|are you sure|let me see", re.I)
CONFIRMATION_PATTERNS = re.compile(r"should i proceed|shall i|do you want me to|is that ok", re.I)
ARCHITECTURE_PATTERNS = re.compile(r"\barchitect(?:ure|ural)\b|\babstraction\b|\bdecoupl|\bcoupling\b|separation of concerns|design pattern|\bmodular|refactor into", re.I)
DEBUGGING_PATTERNS = re.compile(r"why|broken|error|fail|bug|wrong|issue|crash|exception|stack trace", re.I)
GRATITUDE_PATTERNS = re.compile(r"\bthank you\b|\bthanks\b(?! to\b)|\bthx\b|\bappreciate (?:it|that|this|you|the)\b", re.I)
FRUSTRATION_REGEX = re.compile(
    r"\b(?:wtf|wth|ffs|omfg|shit(?:ty|tiest)?|dumbass|horrible|awful|piss(?:ed|ing)? off"
    r"|piece of (?:shit|crap|junk)|what the (?:fuck|hell)"
    r"|fucking? (?:broken|useless|terrible|awful|horrible)|fuck you|screw (?:this|you)"
    r"|so frustrating|this sucks|damn it)\b", re.I)
CAPS_ALLOWLIST = {"I", "OK", "PR", "CI", "UI", "UX", "API", "URL", "URI", "SQL", "JSON",
                  "HTML", "CSS", "YAML", "TODO", "FIXME", "LGTM", "WIP", "CLI", "SDK",
                  "AWS", "DB"}
SHOUT_TOKEN = re.compile(r"\b[A-Z][A-Z']{2,}\b")
IMPERATIVE_VERBS = ["add", "build", "create", "implement", "write", "fix", "update",
                    "refactor", "deploy", "run", "test", "make", "delete", "remove"]

# --- TranscriptChunker constants (transcript_chunker.rb, verbatim) ---
MAX_USER_TEXT_LENGTH = 20_000
PARALLELISM_GAP_MINUTES = 15  # TranscriptSession::PARALLELISM_GAP_MINUTES
TASK_NOTIFICATION_PATTERN = re.compile(r"<task-notification\b[^>]*>.*?</task-notification>", re.S)
LOCAL_COMMAND_TAGS = ["<local-command-caveat", "<command-name", "<command-message",
                      "<command-args", "<local-command-stdout"]
SKILL_TEMPLATE_PATTERNS = [
    re.compile(r"\ABase directory for this skill:"),
    re.compile(r"<!-- AUTO-GENERATED from SKILL\.md"),
    re.compile(r"\A# /\w+:.*\n\n.*skill", re.I | re.S),
]
FIRST_PROMPT_LIMIT = 200  # transcript_discoverer.rb truncate(200)

# DEVIATION (documented): the Ruby pipeline gets pr_number from a client-side
# sidecar (`pr_links`, built with the gh CLI at upload time — transcript
# discoverer apply_pr_links: it maps sessions to PRs the user AUTHORED). That
# producer is not in the archive, so we recover the PR number from the output
# of a `gh pr create` run inside the session — the only transcript evidence
# that the session itself created the PR. Matching ANY PR URL in tool results
# is wrong: sessions that merely read a dependency's PR would gain a phantom
# pr_number, seed a phantom PR commit-group carrying the session's branch, and
# (via branch_match 0.7) swallow every same-branch session into one
# mega-episode (observed live: one group captured 83 of 101 sessions).
PR_URL_PATTERN = re.compile(r"github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")
PR_CREATE_CMD = re.compile(r"\bgh\s+pr\s+create\b")


# --- shared helpers ---

def _blank(s):
    """Ruby .blank? for strings/None."""
    return s is None or str(s).strip() == ""


def _truncate(s, n):
    """Ruby String#truncate: total length n INCLUDING the '...' omission."""
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: max(n - 3, 0)] + "..."


def _round(value, ndigits):
    """Ruby Float#round — half away from zero (Python round() is banker's)."""
    q = Decimal("1." + "0" * ndigits)
    return float(Decimal(repr(float(value))).quantize(q, rounding=ROUND_HALF_UP))


def _parse_ts(ts):
    """ISO-8601 → aware datetime, or None (Ruby Time.parse rescue nil)."""
    if ts is None or str(ts).strip() == "":
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _flatten_result_text(content):
    """tool_result content → text (event_extractor.rb:195-213): string as-is,
    array joins hash blocks' text.to_s with a space, else ''."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            ("" if b.get("text") is None else str(b.get("text")))
            for b in content if isinstance(b, dict))
    return ""


def _cast_bool(v):
    """ActiveModel::Type::Boolean cast == true. nil/'' → false; FALSE_VALUES →
    false; anything else → true."""
    if v is None or v == "":
        return False
    return v not in (False, 0, "0", "f", "F", "false", "FALSE", "off", "OFF")


# --- EventExtractor (event_extractor.rb) ---

class EventExtractor:
    def __init__(self):
        self.events = []
        self.git_commits = []
        self.detected_branch = None
        self.detected_pr_number = None  # local extension, see PR_URL_PATTERN note
        self._pending_git_commit = False
        # tool_use id of an in-flight `gh pr create` (True when the id is
        # unavailable — falls back to next-result adjacency). Matching by id
        # keeps parallel tool calls from misattributing an unrelated result's
        # PR URL to this session.
        self._pending_pr_create = None
        self.plan_files = {}            # basename -> [ {content|edit, full_path, timestamp} ]
        self.dispatch_count = 0
        self.return_count = 0
        self.run_in_background_count = 0
        self.unique_subagent_ids = set()
        self._dispatched_tool_use_ids = set()
        self.truncated = False

    def add_event(self, etype, timestamp, **attrs):
        if len(self.events) >= MAX_EVENTS and etype not in STRUCTURAL_EVENT_TYPES:
            self.truncated = True
            return
        self.events.append({"type": etype, "index": len(self.events),
                            "timestamp": timestamp, **attrs})

    # Extract from a tool_use block (assistant message content)
    def extract_from_tool_use(self, block, timestamp):
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            return
        tool_name = block.get("name")
        inp = block.get("input")
        if not tool_name or not isinstance(inp, dict):
            return

        if tool_name == "Edit":
            path = condense.scrub(str(inp.get("file_path") or ""))
            if not _blank(path):
                self.add_event("file_edit", timestamp, path=path)
                if PLAN_PATH_PATTERN.search(path):
                    basename = os.path.basename(path)
                    self.plan_files.setdefault(basename, []).append(
                        {"edit": True, "full_path": path, "timestamp": timestamp})
        elif tool_name == "Write":
            path = condense.scrub(str(inp.get("file_path") or ""))
            if not _blank(path):
                self.add_event("file_create", timestamp, path=path)
                if PLAN_PATH_PATTERN.search(path):
                    content = inp.get("content")
                    if not _blank(content):
                        basename = os.path.basename(path)
                        self.plan_files.setdefault(basename, []).append(
                            {"content": condense.scrub(str(content)),
                             "full_path": path, "timestamp": timestamp})
        elif tool_name == "Read":
            path = condense.scrub(str(inp.get("file_path") or ""))
            if not _blank(path):
                self.add_event("file_read", timestamp, path=path)
        elif tool_name in ("Bash", "bash", "exec_command", "shell_command"):
            command = str(inp.get("command") or "")
            self.extract_git_from_command(command, timestamp,
                                          tool_use_id=block.get("id"))
            if "git " not in command:
                # Ruby also applies DecisionTextRedactor.regex_redact here;
                # skipped per spec (decisions module owns redaction).
                self.add_event("bash_command", timestamp,
                               command=_truncate(condense.scrub(command), MAX_BASH_LENGTH))
        elif SUBAGENT_TOOL_NAME_PATTERN.fullmatch(tool_name):
            self.extract_subagent_dispatch(block, inp, timestamp)

    def extract_subagent_dispatch(self, block, inp, timestamp):
        tool_use_id = str(block.get("id") or "")
        if not tool_use_id:
            return
        description = str(inp.get("description") or "")
        scrubbed_desc = condense.scrub(description)
        safe_description = (_truncate(scrubbed_desc, MAX_DESCRIPTION_LENGTH)
                            if not _blank(scrubbed_desc) else None)
        prompt_hash = None
        if not _blank(inp.get("prompt")):
            prompt_hash = hashlib.sha1(
                str(inp.get("prompt")).encode("utf-8")).hexdigest()[:12]
        subagent_id = inp.get("subagent_type")
        if _blank(subagent_id):
            subagent_id = inp.get("agent_type")
        if _blank(subagent_id):
            subagent_id = "general-purpose"
        subagent_id = _truncate(str(subagent_id), 64)
        run_in_background = _cast_bool(inp.get("run_in_background"))

        self._dispatched_tool_use_ids.add(tool_use_id)
        self.dispatch_count += 1
        if run_in_background:
            self.run_in_background_count += 1
        self.unique_subagent_ids.add(subagent_id)

        self.add_event("subagent_dispatch", timestamp,
                       tool_use_id=tool_use_id, subagent_id=subagent_id,
                       description=safe_description, prompt_hash=prompt_hash,
                       run_in_background=run_in_background)

    def dispatch_metadata(self):
        return {"dispatch_count": self.dispatch_count,
                "return_count": self.return_count,
                "run_in_background_count": self.run_in_background_count,
                "unique_subagent_ids": sorted(self.unique_subagent_ids)}

    # Extract from a tool_result block (user message content)
    def extract_from_tool_result(self, block, timestamp):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            return

        # subagent_return emitted BEFORE the blank check so an error-only
        # result still produces the return event (event_extractor.rb:189-206).
        tool_use_id = block.get("tool_use_id")
        if not _blank(tool_use_id) and tool_use_id in self._dispatched_tool_use_ids:
            flattened = _flatten_result_text(block.get("content"))
            is_error = block.get("is_error") is True or bool(ERROR_LINE.search(flattened))
            self.return_count += 1
            self.add_event("subagent_return", timestamp,
                           tool_use_id=tool_use_id,
                           return_text_length=len(flattened),
                           indicates_error=is_error)

        result_text = _flatten_result_text(block.get("content"))
        if _blank(result_text):
            return  # note: pending_git_commit is NOT reset on a blank result (Ruby parity)

        m = GIT_COMMIT_OUTPUT.search(result_text)
        if m:
            sha = m.group(1)
            if self._pending_git_commit and self.git_commits:
                self.git_commits[-1]["sha"] = sha
                for e in reversed(self.events):
                    if e["type"] == "git_commit" and e.get("sha") is None:
                        e["sha"] = sha
                        break

        m = GIT_BRANCH_FROM_COMMIT.search(result_text)
        if m:
            self.detected_branch = m.group(1)
        m = GIT_ON_BRANCH.search(result_text)
        if m:
            self.detected_branch = m.group(1)
        m = GIT_SWITCH_BRANCH.search(result_text)
        if m:
            self.detected_branch = m.group(1)

        # local extension (see PR_URL_PATTERN note): only credit a PR number
        # when this result answers the `gh pr create` call (matched by
        # tool_use_id; adjacency fallback when ids are unavailable).
        pending = self._pending_pr_create
        if pending is not None and (pending is True
                                    or tool_use_id == pending):
            m = PR_URL_PATTERN.search(result_text)
            if m:
                self.detected_pr_number = int(m.group(1))
            self._pending_pr_create = None

        self.extract_test_results(result_text, timestamp)

        if ERROR_LINE.search(result_text):
            error_line = next((l for l in result_text.splitlines(keepends=True)
                               if ERROR_LINE.search(l)), None)
            if error_line is not None:
                error_msg = error_line.strip()
                self.add_event("error_encountered", timestamp,
                               message=_truncate(condense.scrub(error_msg), MAX_TEXT_LENGTH))

        self._pending_git_commit = False

    def extract_user_directive(self, text, timestamp):
        if _blank(text):
            return
        self.add_event("user_directive", timestamp,
                       text=_truncate(condense.scrub(text), MAX_TEXT_LENGTH))

    def extract_agent_proposal(self, text, timestamp):
        if _blank(text):
            return
        proposal_type = None
        option_count = None

        for pattern in OPTION_PATTERNS:
            if pattern.search(text):
                proposal_type = "options"
                option_count = len(OPTION_MARKER.findall(text))
                if option_count < 2:
                    option_count = len(OPTION_WORD.findall(text))
                if option_count < 2:
                    option_count = None
                break

        if proposal_type is None:
            for pattern in QUESTION_PATTERNS:
                if pattern.search(text):
                    proposal_type = "question"
                    break

        if proposal_type is None and TRADEOFF_PATTERN.search(text):
            proposal_type = "tradeoff"

        if proposal_type is None:
            return
        self.add_event("agent_proposal", timestamp,
                       text=_truncate(condense.scrub(text), MAX_TEXT_LENGTH),
                       proposal_type=proposal_type, option_count=option_count)

    def extract_agent_thinking(self, block, timestamp):
        if not isinstance(block, dict) or block.get("type") != "thinking":
            return
        text = str(block.get("thinking") or "")
        if _blank(text):
            return
        self.add_event("agent_thinking", timestamp,
                       text=_truncate(condense.scrub(text), MAX_THINKING_LENGTH))

    def extract_git_from_command(self, command, timestamp, tool_use_id=None):
        m = GIT_CHECKOUT_CMD.search(command)
        if m:
            branch = m.group(1)
            if not branch.startswith("-"):
                self.detected_branch = branch
                self.add_event("git_branch_switch", timestamp, branch=branch)

        if GIT_PUSH.search(command):
            self.add_event("git_push", timestamp)

        if PR_CREATE_CMD.search(command):
            self._pending_pr_create = (tool_use_id
                                       if not _blank(tool_use_id) else True)

        if "git commit" not in command:
            return
        m = GIT_COMMIT_MSG_DOUBLE.search(command)
        if m:
            message = m.group(1)
        else:
            m = GIT_COMMIT_MSG_SINGLE.search(command)
            if m:
                message = m.group(1)
            else:
                m = GIT_COMMIT_MSG_HEREDOC.search(command)
                if m:
                    message = m.group(1).strip()
                else:
                    message = "[interactive commit]"
        message = condense.scrub(message)
        self.git_commits.append({"message": message, "timestamp": timestamp})
        self.add_event("git_commit", timestamp, message=message, sha=None)
        self._pending_git_commit = True

    def extract_test_results(self, text, timestamp):
        # Order matters: rspec/jest/cargo before the too-broad pytest pattern.
        m = RSPEC_RESULT.search(text)
        if m:
            self.add_event("test_run", timestamp, framework="rspec",
                           passed=int(m.group(1)) - int(m.group(2)),
                           failed=int(m.group(2)),
                           pending=int(m.group(3)) if m.group(3) else 0)
            return
        m = JEST_RESULT.search(text)
        if m:
            self.add_event("test_run", timestamp, framework="jest",
                           passed=int(m.group(2)),
                           failed=int(m.group(1)) if m.group(1) else 0)
            return
        m = CARGO_RESULT.search(text)
        if m:
            self.add_event("test_run", timestamp, framework="cargo",
                           passed=int(m.group(1)), failed=int(m.group(2)))
            return
        m = PYTEST_RESULT.search(text)
        if m:
            self.add_event("test_run", timestamp, framework="pytest",
                           passed=int(m.group(1)),
                           failed=int(m.group(2)) if m.group(2) else 0)


# --- raw user text filters (transcript_chunker.rb extract_raw_user_text) ---

def _local_command_content(text):
    return any(text.startswith(tag) for tag in LOCAL_COMMAND_TAGS)


def _skill_template_content(text):
    return any(p.search(text) for p in SKILL_TEMPLATE_PATTERNS)


def extract_raw_user_text(content):
    """Port of TranscriptChunker#extract_raw_user_text (without SecretScrubber —
    raw text feeds word counts; scrubbing happens where Ruby persists)."""
    if isinstance(content, str):
        stripped = content.replace("\x00", "").strip()
        if stripped.startswith(("<system_instruction", "<system-reminder")):
            return None
        stripped = TASK_NOTIFICATION_PATTERN.sub("", stripped).strip()
        if not stripped or _local_command_content(stripped) or _skill_template_content(stripped):
            return None
        return condense.scrub(stripped)
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = str(block.get("text") or "").replace("\x00", "")
            if text.startswith(("<system_instruction", "<system-reminder")):
                continue
            text = TASK_NOTIFICATION_PATTERN.sub("", text).strip()
            if not text or _local_command_content(text) or _skill_template_content(text):
                continue
            parts.append(condense.scrub(text))
        joined = " ".join(parts)
        return joined if joined else None
    return None


# --- SessionSignalExtractor (session_signal_extractor.rb) ---

def _count_matches(texts, pattern):
    """Counts MESSAGES matching, not occurrences."""
    return sum(1 for t in texts if pattern.search(t))


def _count_imperative(texts):
    count = 0
    for t in texts:
        words = t.strip().split()
        first_word = words[0].lower() if words else None
        if first_word in IMPERATIVE_VERBS:
            count += 1
    return count


def _parse_timestamps(ts_list):
    if not isinstance(ts_list, list):
        return []
    return [dt for dt in (_parse_ts(ts) for ts in ts_list) if dt is not None]


def compute_duration(first_ts, last_ts, message_timestamps=None):
    parsed = _parse_timestamps(message_timestamps)
    if len(parsed) >= 2:
        parsed.sort()
        active_seconds = 0.0
        for a, b in zip(parsed, parsed[1:]):
            delta = (b - a).total_seconds()
            if delta <= ACTIVE_GAP_MINUTES * 60:
                active_seconds += delta
        return _round(active_seconds / 60.0, 1)
    first = _parse_ts(first_ts)
    last = _parse_ts(last_ts)
    if first is None or last is None:
        return 0
    return _round((last - first).total_seconds() / 60.0, 1)


def _extract_quantitative(user_messages, first_ts, last_ts, tool_count,
                          assistant_count, git_commits, tools_used, message_timestamps):
    duration_minutes = compute_duration(first_ts, last_ts, message_timestamps)
    user_msg_count = len(user_messages)
    total_user_words = sum(m.get("word_count") or 0 for m in user_messages)
    avg_prompt_length = (_round(total_user_words / user_msg_count, 1)
                         if user_msg_count > 0 else 0)
    return {
        "user_message_count": user_msg_count,
        "total_user_words": total_user_words,
        "avg_prompt_length_words": avg_prompt_length,
        "tool_count": tool_count,
        "assistant_count": assistant_count,
        "duration_minutes": duration_minutes,
        "messages_per_minute": (_round(user_msg_count / duration_minutes, 2)
                                if duration_minutes > 0 else 0),
        "tools_per_message": (_round(tool_count / user_msg_count, 2)
                              if user_msg_count > 0 else 0),
        "git_commit_count": len(git_commits or []),
        "unique_tools": len(set(tools_used or [])),
        "plan_mode_used": "EnterPlanMode" in (tools_used or []),
        "task_tool_used": "Task" in (tools_used or []),
        "worktree_used": "EnterWorktree" in (tools_used or []),
    }


def _extract_tdd_discipline(events):
    first_test_idx = None
    first_edit_idx = None
    for i, e in enumerate(events):
        etype = e.get("type")
        if etype == "test_run" and first_test_idx is None:
            first_test_idx = i
        elif etype in ("file_edit", "file_create") and first_edit_idx is None:
            # Plan-file writes are not implementation work (extractor.rb:218-226).
            if not PLAN_PATH_PATTERN.search(str(e.get("path") or "")):
                first_edit_idx = i
    if first_test_idx is None and first_edit_idx is None:
        return {}
    test_first = first_test_idx is not None and (
        first_edit_idx is None or first_test_idx < first_edit_idx)
    return {"tdd_discipline_ratio": 1.0 if test_first else 0.0}


def _extract_recovery_speed(events):
    distances = []
    for i, e in enumerate(events):
        if e.get("type") != "error_encountered":
            continue
        for j in range(i + 1, len(events)):
            jtype = events[j].get("type")
            if jtype == "test_run" and int(events[j].get("passed") or 0) > 0:
                distances.append(j - i)
                break
            if jtype == "git_commit":
                distances.append(j - i)
                break
    if not distances:
        return {}
    s = sorted(distances)
    n = len(s)
    median = s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    return {"recovery_speed": _round(median, 1)}


def _extract_error_retry_ratio(events):
    error_count = 0
    retried = 0
    for i, e in enumerate(events):
        if e.get("type") != "error_encountered":
            continue
        error_count += 1
        prev_cmd = None
        for j in range(i - 1, -1, -1):
            if events[j].get("type") == "bash_command":
                prev_cmd = events[j].get("command")
                break
        if prev_cmd is None:
            continue
        for j in range(i + 1, min(i + 5, len(events) - 1) + 1):
            if events[j].get("type") == "bash_command" and events[j].get("command") == prev_cmd:
                retried += 1
                break
    if error_count == 0:
        return {}
    return {"error_retry_ratio": _round(retried / error_count, 3)}


def _extract_event_signals(events):
    if not isinstance(events, list) or not events:
        return {}
    file_count = sum(1 for e in events if e.get("type") in ("file_edit", "file_create"))
    test_events = [e for e in events if e.get("type") == "test_run"]
    error_count = sum(1 for e in events if e.get("type") == "error_encountered")
    total_passed = sum(int(e.get("passed") or 0) for e in test_events)
    total_failed = sum(int(e.get("failed") or 0) for e in test_events)
    total_tests = total_passed + total_failed
    plan_creates = sum(1 for e in events if e.get("type") == "file_create"
                       and PLAN_PATH_PATTERN.search(str(e.get("path") or "")))

    signals = {"files_modified_count": file_count,
               "test_run_count": len(test_events),
               "error_count": error_count}
    if plan_creates > 0:
        signals["plan_file_count"] = plan_creates
    if total_tests > 0:
        signals["test_pass_rate"] = _round(total_passed / total_tests, 3)
    signals.update(_extract_tdd_discipline(events))
    signals.update(_extract_recovery_speed(events))
    signals.update(_extract_error_retry_ratio(events))
    return signals


def _classify_prompt(text):
    stripped = text.strip().lower()
    words = stripped.split()
    first_word = words[0] if words else None
    if first_word in IMPERATIVE_VERBS:
        return "directive"
    if stripped.endswith("?") or stripped.startswith(
            ("how", "what", "why", "where", "when", "can you", "could you")):
        return "question"
    if SELF_CORRECTION_PATTERNS.search(stripped) or DOMAIN_CORRECTION_PATTERNS.search(stripped):
        return "correction"
    if DEBUGGING_PATTERNS.search(stripped):
        return "debugging"
    if REVIEW_PATTERNS.search(stripped):
        return "review"
    return "other"


def _normalize_prompt(text):
    norm = re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower())
    norm = re.sub(r" +", " ", norm)  # Ruby squeeze(" ")
    return norm.strip()


def _extract_repeated_prompts(user_messages):
    counts = {}
    examples = {}
    for m in user_messages:
        wc = int(m.get("word_count") or 0)
        if wc == 0 or wc > SHORT_PROMPT_MAX_WORDS:
            continue
        norm = _normalize_prompt(m.get("text"))
        if len(norm) < 2:
            continue
        counts[norm] = counts.get(norm, 0) + 1
        if norm not in examples:
            examples[norm] = _truncate(condense.scrub(str(m.get("text") or "")).strip(), 80)
    if not counts:
        return {}
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_REPEATED_PROMPTS]
    return {"repeated_prompts": [
        {"norm": norm, "count": c, "example": examples[norm]} for norm, c in top]}


def _charged(text):
    if FRUSTRATION_REGEX.search(text):
        return True
    if text.count("!") >= 2:
        return True
    shouts = [w for w in SHOUT_TOKEN.findall(text) if w not in CAPS_ALLOWLIST]
    return len(shouts) >= 2


def _extract_charged_messages(user_messages):
    out = []
    for m in user_messages:
        text = str(m.get("text") or "").strip()
        if not text or len(text) > 280:
            continue
        if not _charged(text):
            continue
        out.append(_truncate(condense.scrub(text).strip(), 200))
        if len(out) >= MAX_CHARGED_MESSAGES:
            break
    out = list(dict.fromkeys(out))  # Ruby uniq! preserves first occurrence
    return {"charged_messages": out} if out else {}


def extract_signals(user_messages, first_timestamp, last_timestamp, tool_count,
                    assistant_count, git_commits, tools_used, events=None,
                    message_timestamps=None):
    """Port of SessionSignalExtractor.extract (pr_diff omitted — no PR diff
    locally, so the optional pr_stats key never appears)."""
    texts = [_truncate(str(m.get("text") or ""), MAX_TEXT_FOR_REGEX) for m in user_messages]

    signals = {}
    signals.update(_extract_quantitative(user_messages, first_timestamp, last_timestamp,
                                         tool_count, assistant_count, git_commits,
                                         tools_used, message_timestamps))
    signals.update(_extract_event_signals(events))
    signals.update({  # delegation
        "imperative_prompts": _count_imperative(texts),
        "confirmation_requests": _count_matches(texts, CONFIRMATION_PATTERNS),
        "kill_decisions": _count_matches(texts, KILL_PATTERNS),
        "review_checks": _count_matches(texts, REVIEW_PATTERNS),
    })
    signals.update({  # feedback loops
        "self_corrections": _count_matches(texts, SELF_CORRECTION_PATTERNS),
        "critiques": _count_matches(texts, CRITIQUE_PATTERNS),
        "domain_corrections": _count_matches(texts, DOMAIN_CORRECTION_PATTERNS),
    })
    signals.update({  # decision making
        "hypothesis_driven": _count_matches(texts, HYPOTHESIS_PATTERNS),
        "debugging_messages": _count_matches(texts, DEBUGGING_PATTERNS),
        "architecture_discussions": _count_matches(texts, ARCHITECTURE_PATTERNS),
    })
    signals.update({"narrative_framing": _count_matches(texts, NARRATIVE_PATTERNS)})
    signals.update({"product_references": _count_matches(texts, PRODUCT_PATTERNS)})
    substantive = sum(1 for m in user_messages if (m.get("word_count") or 0) > 15)
    terse = sum(1 for m in user_messages if (m.get("word_count") or 0) <= 5)
    signals.update({
        "substantive_messages": substantive,
        "terse_messages": terse,
        "substantive_ratio": (_round(substantive / len(user_messages), 2)
                              if user_messages else 0),
        "courtesy_messages": _count_matches(texts, GRATITUDE_PATTERNS),
    })
    prompt_types = {}
    for text in texts:
        ptype = _classify_prompt(text)
        prompt_types[ptype] = prompt_types.get(ptype, 0) + 1
    signals["prompt_types"] = prompt_types
    signals.update(_extract_repeated_prompts(user_messages))
    signals.update(_extract_charged_messages(user_messages))
    return signals


# --- ActiveTimeWindowsCalculator (active_time_windows_calculator.rb) ---

def active_time_windows(timestamps):
    parsed = sorted(_parse_timestamps(timestamps))
    if not parsed:
        return []
    gap_seconds = PARALLELISM_GAP_MINUTES * 60
    windows = []
    window_start = parsed[0]
    window_end = parsed[0]
    for t in parsed[1:]:
        if (t - window_end).total_seconds() > gap_seconds:
            windows.append([_iso(window_start), _iso(window_end)])
            window_start = t
        window_end = t
    windows.append([_iso(window_start), _iso(window_end)])
    return windows


# --- plan files (transcript_chunker.rb persist_plan_files + plan_patterns.rb) ---

def build_plan_files(plan_files):
    """Version = per-filename counter over Write entries; Edits are recorded in
    the version stream but skipped for numbering (chunker.rb:622-641). The
    PlanPatterns booleans are applied to each version's content."""
    out = []
    for filename, versions in plan_files.items():
        version_num = 0
        for entry in versions:
            if entry.get("edit"):
                continue
            version_num += 1
            content = entry.get("content") or ""
            out.append({
                "filename": filename,
                "full_path": entry.get("full_path"),
                "version": version_num,
                "content": content,
                "has_verification": bool(VERIFICATION_PATTERN.search(content)),
                "has_alternatives": bool(ALTERNATIVES_PATTERN.search(content)),
                "has_edge_cases": bool(EDGE_CASES_PATTERN.search(content)),
            })
    return out


# --- session walk (transcript_chunker.rb parse_entries) ---

def _read_entries(path):
    agent_type = condense.detect_format(path)
    if agent_type == "codex_cli":
        entries, meta = condense.normalize_codex(path)
        return entries, meta
    entries = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.replace("\x00", "").strip()
            if not raw:
                continue
            try:
                entries.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return entries, {}


def extract_session(path):
    try:
        entries, normalizer_meta = _read_entries(path)
    except OSError:
        return None

    # Subagent detection: Paxel knows from the discovery layout (subagents/
    # directory). Locally: path-based, with an all-sidechain fallback so a
    # subagent transcript processed standalone isn't filtered to nothing
    # (chunker.rb:199-204).
    message_entries = [e for e in entries
                       if isinstance(e, dict) and isinstance(e.get("message"), dict)]
    is_subagent = "/subagents/" in path or (
        bool(message_entries) and all(e.get("isSidechain") for e in message_entries))

    extractor = EventExtractor()
    raw_user_messages = []
    first_timestamp = None
    last_timestamp = None
    all_message_timestamps = []
    tool_count = 0
    assistant_count = 0
    tools_used = []
    entry_git_branch = None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        if entry.get("isSidechain") and not is_subagent:
            continue

        if entry_git_branch is None and not _blank(entry.get("gitBranch")):
            entry_git_branch = entry.get("gitBranch")

        content = message.get("content")
        timestamp = entry.get("timestamp")
        if timestamp:
            if first_timestamp is None:
                first_timestamp = timestamp
            last_timestamp = timestamp
            all_message_timestamps.append(timestamp)

        etype = entry.get("type")
        if etype == "user":
            if isinstance(content, list):
                for block in content:
                    extractor.extract_from_tool_result(block, timestamp)
            raw_text = extract_raw_user_text(content)
            if raw_text:
                raw_user_messages.append({
                    "text": _truncate(raw_text, MAX_USER_TEXT_LENGTH),
                    "timestamp": timestamp,
                    "word_count": len(raw_text.split()),
                })
                extractor.extract_user_directive(raw_text, timestamp)
        elif etype == "assistant":
            counted = False
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        extractor.extract_from_tool_use(block, timestamp)
                        if block.get("name"):
                            tools_used.append(block["name"])
                        tool_count += 1
                        counted = True
                    elif btype == "thinking":
                        extractor.extract_agent_thinking(block, timestamp)
                    elif btype == "text" and not _blank(block.get("text")):
                        extractor.extract_agent_proposal(block["text"], timestamp)
                        counted = True
            elif isinstance(content, str):
                extractor.extract_agent_proposal(content, timestamp)
                counted = True  # Ruby's string path always yields condensed text
            if counted:
                assistant_count += 1

    # SessionSignalExtractor runs only when raw user messages exist
    # (chunker.rb extract_and_save_signals early return).
    signals = {}
    if raw_user_messages:
        signals = extract_signals(
            raw_user_messages, first_timestamp, last_timestamp, tool_count,
            assistant_count, extractor.git_commits, tools_used,
            events=extractor.events, message_timestamps=all_message_timestamps)

    # user_highlights (chunker.rb:610-614): >15-word user messages, 2000 chars
    # each, first 50, joined "\n---\n"; null when empty (.presence).
    highlight_texts = [_truncate(m["text"], 2_000)
                       for m in raw_user_messages if m["word_count"] > 15][:50]
    user_highlights = "\n---\n".join(highlight_texts) or None

    first_prompt = None
    if raw_user_messages:
        first_prompt = _truncate(raw_user_messages[0]["text"], FIRST_PROMPT_LIMIT)

    # git_branch priority mirrors the Ruby pipeline: client metadata (the
    # per-entry gitBranch field Claude Code stamps / Codex session_meta git
    # branch) wins; the extractor's transcript-detected branch backfills
    # (discoverer.rb:196 + chunker.rb:317-323).
    git_branch = entry_git_branch or normalizer_meta.get("git_branch") \
        or extractor.detected_branch

    return {
        "session_id": str(normalizer_meta.get("session_id")
                          or os.path.splitext(os.path.basename(path))[0]),
        "path": path,
        "first_prompt": first_prompt,
        "session_created_at": first_timestamp,
        "session_modified_at": last_timestamp,
        "events": extractor.events,
        "session_signals": signals,
        "user_highlights": user_highlights,
        "plan_files": build_plan_files(extractor.plan_files),
        "active_time_windows": active_time_windows(all_message_timestamps),
        "pr_number": extractor.detected_pr_number,
        "git_branch": git_branch,
        "event_git_shas": [e["sha"] for e in extractor.events
                           if e["type"] == "git_commit" and e.get("sha")],
        "event_branches": [e["branch"] for e in extractor.events
                           if e["type"] == "git_branch_switch" and e.get("branch")],
        "dispatch_metadata": extractor.dispatch_metadata(),
    }


def main():
    if len(sys.argv) < 2:
        sys.stderr.write(__doc__)
        sys.exit(2)
    for p in condense.iter_paths(sys.argv[1:]):
        # One malformed session must never abort the batch; skip and report it.
        try:
            out = extract_session(p)
        except Exception as ex:
            sys.stderr.write(f"[skip] {p}: {type(ex).__name__}: {ex}\n")
            continue
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()

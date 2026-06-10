#!/usr/bin/env python3
"""
analytics.py — faithful Python port of YC Paxel's client-side UPLOAD-ONLY
analytics. These metrics NEVER feed the local score — they are a transparency
report of what Paxel's server receives alongside the condensed transcripts.

Ruby sources of truth (ported verbatim — constants, regexes, rounding):
  local_code_quality_analyzer.rb  — 14 deterministic code-quality dimensions
  codebase_profiler.rb            — frameworks/deps/CLAUDE.md/agent configs/
                                    architecture tables/documentation/infra
  velocity_metrics_service.rb     — numstat-derived velocity (+ author-scoped)
  steering_trace_extractor.rb     — 10 STEERING_ACTIONS over user_directive
                                    events (redirect via REDIRECT_INDICATORS
                                    from concerns/transcript_patterns.rb)
  parallelism_analyzer.rb         — dispatch/return + committed-return signals
  pr_diff_stats_service.rb        — TEST_PATH_PATTERN

DOCUMENTED SUBSTITUTION — /git_metrics.txt:
  Paxel's container reads a pre-packaged /git_metrics.txt
  (`git log --pretty='%H|%aI|%s' --numstat` produced client-side) and,
  server-side, a numstat hash with author/email per commit. Locally we run
  `git log --pretty=format:%H|%aI|%an|%ae|%s --numstat` ONCE against --repo
  and derive BOTH views from it: the {hash,date,subject,files[]} commit list
  the code-quality git dimensions parse, and the per-commit
  {author,email,date,added,deleted,files} records velocity sums. Same data,
  one source, no intermediate file.

DOCUMENTED DEGRADATIONS — parallelism (no child subagent session records
locally; events.py emits main sessions only, and there are no
commit_group_sessions links):
  - dispatch_with_committed_return_count: the Ruby predicate's first branch
    (a child subagent session that shipped) can never fire, so the
    parent-commit-after-return fallback (ParallelismAnalyzer
    .parent_committed_after_return?) is the operative path here.
  - concurrent_pairs_with_ships_count / review_separation_count: require
    child subagent sessions with active-window overlap / shared commit
    groups. Emitted as null with a note — NOT as fake zeros.

sessions.jsonl input (scripts/events.py output) — one JSON object per line:
  {"session_id": ..., "events": [{"type": "user_directive", "text": ...},
   {"type": "subagent_dispatch"|"subagent_return", "tool_use_id": ...},
   {"type": "git_commit", "message": ..., "sha": ...}, ...],
   "active_time_windows": [[iso, iso], ...], "session_signals": {...},
   "dispatch_metadata": {"dispatch_count": N, "return_count": N, ...}}

Usage:
  python3 analytics.py --repo <path> --sessions <sessions.jsonl>
      [--since ISO] [--out report.json] [--md report.md]
      [--author NAME ...] [--author-email EMAIL ...]
"""
import argparse
import glob as globmod
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

# --- constants (verbatim from the Ruby services) ---
MAX_FILES = 10_000          # content-read cap (local_code_quality_analyzer.rb:18)
MAX_FILE_SIZE = 100_000     # 100KB per file (analyzer + profiler MAX_FILE_READ)

SOURCE_EXTENSIONS = {".rb", ".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs",
                     ".java", ".kt", ".swift", ".c", ".cpp", ".h"}
RESCUE_LANGUAGES = {".rb", ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt",
                    ".go", ".rs", ".swift", ".c", ".cpp"}

# pr_diff_stats_service.rb / velocity_metrics_service.rb (verbatim, /i)
TEST_PATH_PATTERN = re.compile(
    r"(?:test|spec|__tests__|_test\.go|_test\.rb|\.test\.|\.spec\.)", re.I)

CONVENTIONAL_PATTERN = re.compile(
    r"\A(feat|fix|docs|style|refactor|test|chore|perf|ci|build|revert)[(:]")

# Ruby $ matches end-of-LINE by default → re.M is load-bearing for ruby_bare.
RESCUE_PATTERNS = {
    "ruby_bare": re.compile(r"rescue\s*($|\s*=>)", re.M),
    "ruby_standard": re.compile(r"rescue\s+(?:StandardError|Exception)\b"),
    "ruby_specific": re.compile(
        r"rescue\s+[A-Z]\w+(?:::\w+)*(?:\s*,\s*[A-Z]\w+(?:::\w+)*)*"),
    "js_catch": re.compile(r"catch\s*\(\s*\w+\s*\)"),
    "python_bare": re.compile(r"except\s*:"),
    "python_exception": re.compile(r"except\s+Exception"),
    "python_specific": re.compile(r"except\s+[A-Z]\w+"),
}

# test_file? (local_code_quality_analyzer.rb:527-529) — case-SENSITIVE
TEST_FILE_DIR = re.compile(r"(?:test|spec|__tests__)/")
TEST_FILE_SUFFIX = re.compile(r"[._](?:test|spec)\.\w+$")

# codebase_profiler.rb — agent config map + constraint pattern (verbatim)
AGENT_CONFIG_FILES = {
    "claude_code": {"paths": ["CLAUDE.md", ".claude/CLAUDE.md"], "type": "markdown"},
    "codex": {"paths": ["AGENTS.md", "agents.md", "codex.md", "CODEX.md"], "type": "markdown"},
    "cursor": {"paths": [".cursorrules"], "globs": [".cursor/rules/*.mdc"], "type": "markdown"},
    "windsurf": {"paths": [".windsurfrules"], "type": "markdown"},
    "github_copilot": {"paths": [".github/copilot-instructions.md"], "type": "markdown"},
    "aider": {"paths": [".aider.conf.yml"], "type": "yaml"},
    "continue_dev": {"paths": [".continuerc.json", ".continue/config.json"], "type": "json"},
}
CONSTRAINT_PATTERN = re.compile(
    r"\bNEVER\b|\bALWAYS\b|\bMUST\b|\bDO NOT\b|\bREQUIRED\b|\bSHALL\b"
    r"|\bIMPORTANT:|\bENSURE\b", re.I)

# steering_trace_extractor.rb STEERING_ACTIONS (10, verbatim, ordered) with
# redirect = REDIRECT_INDICATORS from concerns/transcript_patterns.rb.
REDIRECT_INDICATORS = re.compile(
    r"\b(actually|instead|wait|change|switch|pivot|different approach"
    r"|on second thought)\b", re.I)
STEERING_ACTIONS = [
    ("explore", re.compile(
        r"let's (try|explore|look at|investigate|check)|what if|how about|i wonder", re.I)),
    ("constrain", re.compile(
        r"\b(must|should|always|never|require|constraint|rule)\b"
        r".*\b(use|follow|apply|enforce)\b", re.I)),
    ("delegate", re.compile(
        r"\b(implement|build|create|write|add|fix|update|deploy|run|test|make)\b", re.I)),
    ("inspect", re.compile(
        r"\b(show me|let me see|what does|how does|look at|check|verify|review)\b", re.I)),
    ("reject", re.compile(
        r"\b(no|wrong|bad|don't|stop|that's not|revert|undo|scratch that|kill)\b", re.I)),
    ("redirect", REDIRECT_INDICATORS),
    ("verify", re.compile(
        r"\b(test|verify|check|confirm|make sure|does it|is it|run the)\b", re.I)),
    ("ship", re.compile(
        r"\b(ship|deploy|push|release|merge|commit|done|good to go|looks good)\b", re.I)),
    ("debug", re.compile(
        r"\b(why|error|fail|bug|broken|wrong|issue|crash|exception|trace)\b", re.I)),
    ("recover", re.compile(
        r"\b(fix|resolve|recover|restore|rollback|patch|workaround|fallback)\b", re.I)),
]
STEERING_ACTIONS_CAP = 100


# --- Ruby-semantics helpers ---

def ruby_round(x, ndigits=0):
    """Float#round: half away from zero on the exact binary double.
    Decimal(float) is binary-exact, so e.g. 2.675 (stored 2.67499...) rounds
    to 2.67 exactly as Ruby does. ndigits=0 returns int (Ruby returns Integer)."""
    q = Decimal(1).scaleb(-ndigits)
    d = Decimal(x).quantize(q, rounding=ROUND_HALF_UP)
    return int(d) if ndigits == 0 else float(d)


def ratio(numerator, denominator):
    """analyzer#ratio / velocity#safe_ratio: 0.0 on zero denominator, round(3)."""
    if float(denominator) == 0.0:
        return 0.0
    return ruby_round(float(numerator) / float(denominator), 3)


def ruby_lines_count(content):
    """String#lines.count: split after \\n, trailing partial line counts."""
    if not content:
        return 0
    return content.count("\n") + (0 if content.endswith("\n") else 1)


def ruby_split_ws(content):
    """String#split(/\\s+/): keeps a leading "" on leading whitespace, drops
    trailing empties — re.split keeps both, so trim only the tail."""
    parts = re.split(r"\s+", content)
    while parts and parts[-1] == "":
        parts.pop()
    return parts


def rails_truncate(text, length, omission="..."):
    """Rails String#truncate: result length <= `length`, omission included."""
    if len(text) <= length:
        return text
    return text[: max(length - len(omission), 0)] + omission


def scan_count(pattern, content):
    """Ruby content.scan(re).size — number of matches (groups irrelevant)."""
    return sum(1 for _ in pattern.finditer(content))


def median(arr):
    if not arr:
        return 0
    s = sorted(arr)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 == 1 else ruby_round((s[mid - 1] + s[mid]) / 2.0, 0)


def to_i(value):
    """Ruby #to_i: nil → 0, non-numeric junk → 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_date(value):
    """Lenient ISO date parse (Date.parse analog for git %aI dates)."""
    if not value:
        return None
    s = str(value)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --- git log collection (the /git_metrics.txt substitution) ---

GIT_LOG_FORMAT = "%H|%aI|%an|%ae|%s"
_GIT_HEADER_RE = re.compile(r"\A[0-9a-f]{40}\|")
_GIT_NUMSTAT_RE = re.compile(r"\A\d+\t\d+\t")
_GIT_BINARY_RE = re.compile(r"\A-\t-\t")


def collect_commits(repo_path, since=None):
    """One `git log --numstat` run → list of commit dicts (newest first):
    {hash, date, author, email, subject, files: [{added, deleted, path}]}.
    Parsing mirrors analyzer#parse_git_metrics: numstat lines \\d+\\t\\d+\\t,
    binary files kept as added=0/deleted=0 entries."""
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return []
    cmd = ["git", "-C", repo_path, "log", "--no-color",
           f"--pretty=format:{GIT_LOG_FORMAT}", "--numstat"]
    if since:
        cmd.append(f"--since={since}")
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:  # e.g. repo with no commits yet
        return []
    commits, cur = [], None
    for line in out.stdout.decode("utf-8", errors="replace").split("\n"):
        if _GIT_HEADER_RE.match(line):
            sha, dt, author, email, subject = line.split("|", 4)
            cur = {"hash": sha, "date": dt, "author": author, "email": email,
                   "subject": subject, "files": []}
            commits.append(cur)
        elif cur is not None and _GIT_NUMSTAT_RE.match(line):
            added, deleted, path = line.split("\t", 2)
            cur["files"].append({"added": int(added), "deleted": int(deleted),
                                 "path": path})
        elif cur is not None and _GIT_BINARY_RE.match(line):
            _, _, path = line.split("\t", 2)
            cur["files"].append({"added": 0, "deleted": 0, "path": path,
                                 "binary": True})
    return commits


# --- CodebaseProfiler port (the pieces the dimensions consume) ---

class Profiler:
    """codebase_profiler.rb port. `files` is the analyzer's filtered relative
    path list; the Ruby class receives it pre-joined as a newline file_tree
    string — both views are kept here. Rails.logger lines are dropped."""

    def __init__(self, repo_path, files):
        self.repo_path = repo_path
        self.files = list(files)
        self.file_tree = "\n".join(self.files)

    def safe_read(self, filename):
        """Profiler's own safe_read: 100KB cap, NO symlink-escape check —
        this asymmetry with the analyzer's safe_read is verbatim Ruby."""
        path = os.path.join(self.repo_path, filename)
        try:
            if not os.path.exists(path):
                return None
            if os.path.getsize(path) > MAX_FILE_SIZE:
                return None
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    def detect_frameworks(self):
        frameworks = []
        gemfile = self.safe_read("Gemfile")
        if gemfile is not None:
            for name in ("rails", "sinatra", "rspec", "minitest", "sidekiq"):
                if name in gemfile:
                    frameworks.append(name)
        pkg = self.safe_read("package.json")
        if pkg is not None:
            for name in ("react", "next", "vue", "express", "jest", "typescript"):
                if name in pkg:
                    frameworks.append(name)
        return list(dict.fromkeys(frameworks))  # uniq, order-preserving

    def count_dependencies(self):
        count = 0
        gemfile = self.safe_read("Gemfile")
        if gemfile is not None:
            count += scan_count(re.compile(r"^\s*gem\s+", re.M), gemfile)
        pkg = self.safe_read("package.json")
        if pkg is not None:
            try:
                parsed = json.loads(pkg)
                count += len(parsed.get("dependencies") or {})
                count += len(parsed.get("devDependencies") or {})
            except (json.JSONDecodeError, AttributeError):
                pass
        return count

    def _claude_md_stats(self, content):
        lines = content.splitlines()
        return {
            "word_count": len(ruby_split_ws(content)),
            "line_count": ruby_lines_count(content),
            "rules_count": sum(
                1 for l in lines if l.strip().startswith(
                    ("-", "*", "1", "2", "3", "4", "5", "6", "7", "8", "9"))),
            "never_count": scan_count(re.compile(r"\bNEVER\b", re.I), content),
            "always_count": scan_count(re.compile(r"\bALWAYS\b", re.I), content),
            "must_count": scan_count(re.compile(r"\bMUST\b", re.I), content),
            "explicit_constraints": scan_count(CONSTRAINT_PATTERN, content),
        }

    def analyze_claude_md(self):
        content = self.safe_read("CLAUDE.md")
        return None if content is None else self._claude_md_stats(content)

    def _config_content_stats(self, content, ftype):
        return {
            "word_count": len(ruby_split_ws(content)),
            "constraint_count": (scan_count(CONSTRAINT_PATTERN, content)
                                 if ftype == "markdown" else 0),
        }

    def analyze_agent_configs(self):
        tools_found = []
        total_word_count = 0
        total_constraint_count = 0
        claude_stats = None

        for tool_name, config in AGENT_CONFIG_FILES.items():
            found_any = False
            tool_words = 0
            tool_constraints = 0

            for rel_path in config.get("paths", []):
                content = self.safe_read(rel_path)
                if content is None:
                    continue
                found_any = True
                stats = self._config_content_stats(content, config["type"])
                tool_words += stats["word_count"]
                tool_constraints += stats["constraint_count"]
                # Legacy CLAUDE.md fields: prefer root CLAUDE.md, fall back
                # to .claude/CLAUDE.md (codebase_profiler.rb:137-141)
                if tool_name == "claude_code" and (
                        claude_stats is None or rel_path == "CLAUDE.md"):
                    claude_stats = self._claude_md_stats(content)

            for pattern in config.get("globs", []):
                for full_path in globmod.glob(os.path.join(self.repo_path, pattern)):
                    try:
                        if not os.path.isfile(full_path):
                            continue
                        if os.path.getsize(full_path) > MAX_FILE_SIZE:
                            continue
                        with open(full_path, "r", encoding="utf-8",
                                  errors="replace") as fh:
                            content = fh.read()
                    except OSError:
                        continue
                    found_any = True
                    stats = self._config_content_stats(content, config["type"])
                    tool_words += stats["word_count"]
                    tool_constraints += stats["constraint_count"]

            if found_any:
                tools_found.append(tool_name)
                total_word_count += tool_words
                total_constraint_count += tool_constraints

        result = {
            "exists": bool(tools_found),
            "tools_configured": tools_found,
            "tool_count": len(tools_found),
            "total_word_count": total_word_count,
            "total_constraint_count": total_constraint_count,
        }
        if claude_stats:
            result.update(claude_stats)
        return result

    # Per-framework architecture path-regex tables (verbatim; Ruby \A/\z → \A/\Z)
    ARCHITECTURE_PATTERNS = {
        "rails": {
            "services": r"\Aapp/services/.+\.rb\Z",
            "jobs": r"\Aapp/jobs/.+\.rb\Z",
            "models": r"\Aapp/models/(?!concerns/).+\.rb\Z",
            "controllers": r"\Aapp/controllers/.+_controller\.rb\Z",
            "views": r"\Aapp/views/.+\.(?:erb|haml|slim|jbuilder)\Z",
            "concerns": r"\Aapp/(?:models|controllers)/concerns/.+\.rb\Z",
            "helpers": r"\Aapp/helpers/.+\.rb\Z",
            "channels": r"\Aapp/channels/.+\.rb\Z",
            "mailers": r"\Aapp/mailers/.+\.rb\Z",
            "middleware": r"\Aapp/middleware/.+\.rb\Z",
            "serializers": r"\Aapp/serializers/.+\.rb\Z",
            "validators": r"\Aapp/validators/.+\.rb\Z",
            "decorators": r"\Aapp/decorators/.+\.rb\Z",
            "policies": r"\Aapp/policies/.+\.rb\Z",
            "components": r"\Aapp/components/.+\.rb\Z",
        },
        "go": {
            "commands": r"\Acmd/[^/]+/main\.go\Z",
            "packages": r"\Ainternal/[^/]+/[^/]+\.go\Z",
            "handlers": r"handler[s]?/[^/]+\.go\Z",
            "middleware": r"middleware/[^/]+\.go\Z",
            "models": r"model[s]?/[^/]+\.go\Z",
        },
        "python": {
            "models": r"models?/[^/]+\.py\Z",
            "views": r"views?/[^/]+\.py\Z",
            "services": r"services?/[^/]+\.py\Z",
            "tasks": r"tasks?/[^/]+\.py\Z",
            "serializers": r"serializers?/[^/]+\.py\Z",
            "middleware": r"middleware/[^/]+\.py\Z",
        },
        "node": {
            "routes": r"routes?/[^/]+\.[jt]sx?\Z",
            "controllers": r"controllers?/[^/]+\.[jt]sx?\Z",
            "services": r"services?/[^/]+\.[jt]sx?\Z",
            "models": r"models?/[^/]+\.[jt]sx?\Z",
            "middleware": r"middleware/[^/]+\.[jt]sx?\Z",
            "components": r"components?/[^/]+\.[jt]sx?\Z",
        },
        "generic": {
            "services": r"\A(?!(?:test|spec|__tests__)/)[^/]*/services?/[^/]+\.\w+\Z",
            "models": r"\A(?!(?:test|spec|__tests__)/)[^/]*/models?/[^/]+\.\w+\Z",
            "controllers": r"\A(?!(?:test|spec|__tests__)/)[^/]*/controllers?/[^/]+\.\w+\Z",
        },
    }

    def detect_project_framework(self):
        tree = self.file_tree
        if "Gemfile" in tree and "app/" in tree:
            return "rails"
        if "go.mod" in tree or "go.sum" in tree:
            return "go"
        if "requirements.txt" in tree or "pyproject.toml" in tree or "setup.py" in tree:
            return "python"
        if "package.json" in tree:
            return "node"
        return "generic"

    def analyze_architecture(self):
        framework = self.detect_project_framework()
        patterns = self.ARCHITECTURE_PATTERNS[framework]
        counts = {}
        for name, pattern in patterns.items():
            rx = re.compile(pattern)
            count = sum(1 for path in self.files if rx.search(path.strip()))
            if count > 0:
                counts[name] = count
        return counts

    def analyze_documentation(self):
        found_docs = []
        for path in self.files:
            path = path.strip()
            if not path:
                continue
            if not (re.search(r"\.md\Z", path, re.I)
                    or re.match(r"README", path, re.I)):
                continue
            if re.search(r"(?:test|spec|__tests__|evals?)/", path, re.I):
                continue
            if re.search(r"(?:node_modules|vendor|\.github)/", path, re.I):
                continue
            found_docs.append(path)
        return {
            "doc_count": len(found_docs),
            "docs": found_docs[:30],
            "has_design_docs": any(re.search(r"design", d, re.I) for d in found_docs),
            "has_architecture_doc": any(re.search(r"architect", d, re.I) for d in found_docs),
            "has_testing_doc": any(re.search(r"testing", d, re.I) for d in found_docs),
        }

    def detect_infrastructure_from_tree(self):
        tree = self.file_tree
        signals = {}
        if "Dockerfile" in tree or "docker-compose" in tree:
            signals["docker"] = True
        if ".github/workflows" in tree or ".circleci" in tree or "Jenkinsfile" in tree:
            signals["ci"] = True
        if ".tf" in tree:
            signals["terraform"] = True
        if "k8s" in tree or "kubernetes" in tree:
            signals["kubernetes"] = True
        if "datadog" in tree or "sentry" in tree or "newrelic" in tree:
            signals["monitoring"] = True
        return signals


# --- LocalCodeQualityAnalyzer port (14 dimensions) ---

class CodeQualityAnalyzer:
    def __init__(self, repo_path, commits):
        self.repo_path = repo_path
        self.commits = commits          # collect_commits output (git_metrics sub)
        self.files = []
        self.profiler = None
        self._contents = {}
        self._source_files = None
        self._readable_source_files = None

    def analyze(self):
        try:
            if not os.path.isdir(self.repo_path):
                return {"status": "no_repo", "dimensions": {}}
            self.files = self.discover_files()
            if not self.files:
                return {"status": "empty_repo", "dimensions": {}}
            self.profiler = Profiler(self.repo_path, self.files)

            l1_analyzers = [
                ("commit_discipline", "analyze_commit_discipline"),
                ("test_quality", "analyze_test_quality"),
                ("code_quality", "analyze_code_quality"),
                ("error_handling", "analyze_error_handling"),
                ("security_signals", "analyze_security_signals"),
                ("architecture", "analyze_architecture"),
                ("documentation", "analyze_documentation"),
                ("agent_config_quality", "analyze_agent_configs"),
                ("infrastructure", "analyze_infrastructure"),
                ("dependency_management", "analyze_dependencies"),
                ("git_workflow", "analyze_git_workflow"),
                ("code_evolution", "analyze_code_evolution"),
                ("performance_awareness", "analyze_performance"),
                ("production_readiness", "analyze_production_readiness"),
            ]
            dims = {}
            for dim, method_name in l1_analyzers:
                # Fault-isolate one dimension: a crash records an error for
                # THIS dimension only (local_code_quality_analyzer.rb:69-76).
                try:
                    dims[dim] = getattr(self, method_name)()
                except Exception as e:
                    dims[dim] = {"status": "error", "error": type(e).__name__}
            return {"status": "complete", "file_count": len(self.files),
                    "dimensions": dims}
        except Exception as e:
            return {"status": "error", "error": f"{type(e).__name__}: {e}",
                    "dimensions": {}}

    # -- file discovery (uncapped classification list; reads capped below) --

    def discover_files(self):
        if os.path.isdir(os.path.join(self.repo_path, ".git")):
            try:
                out = subprocess.run(
                    ["git", "-C", self.repo_path, "ls-files", "-z"],
                    capture_output=True, timeout=120)
                raw = out.stdout if out.returncode == 0 else b""
            except (OSError, subprocess.TimeoutExpired):
                raw = b""
            # decode(errors="replace") ≈ Ruby .scrub on each path (audit C15)
            files = [f for f in raw.decode("utf-8", errors="replace").split("\0") if f]
        else:
            files = self._walk_files()
        kept = []
        for f in files:
            if f.startswith((".git/", "node_modules/", "vendor/", ".bundle/")):
                continue
            if "/node_modules/" in f or "/vendor/bundle/" in f:
                continue
            if not self._safe_path(f):
                continue
            kept.append(f)
        return kept

    def _walk_files(self):
        # Dir.glob(FNM_DOTMATCH) analog; sorted for determinism (Ruby glob
        # order is OS-dependent — documented deviation).
        out = []
        for root, _dirs, names in os.walk(self.repo_path):
            for name in names:
                full = os.path.join(root, name)
                if os.path.isfile(full):
                    out.append(os.path.relpath(full, self.repo_path))
        return sorted(out)

    def _safe_path(self, relative_path):
        """Symlink guard: prevent path traversal outside the repo."""
        full = os.path.join(self.repo_path, relative_path)
        try:
            if not os.path.exists(full):
                return False
            if not os.path.islink(full):
                return True
            return os.path.realpath(full).startswith(
                os.path.realpath(self.repo_path))
        except OSError:
            return False

    def source_files(self):
        if self._source_files is None:
            self._source_files = [
                f for f in self.files
                if os.path.splitext(f)[1] in SOURCE_EXTENSIONS]
        return self._source_files

    def readable_source_files(self):
        # MAX_FILES caps CONTENT reads only, at the source level (finding #4)
        if self._readable_source_files is None:
            self._readable_source_files = self.source_files()[:MAX_FILES]
        return self._readable_source_files

    def safe_read(self, relative_path):
        if relative_path in self._contents:
            return self._contents[relative_path]
        full = os.path.join(self.repo_path, relative_path)
        try:
            if not (os.path.exists(full) and os.path.isfile(full)):
                return None
            if os.path.getsize(full) > MAX_FILE_SIZE:
                return None
            if not self._safe_path(relative_path):
                return None
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            return None
        self._contents[relative_path] = content
        return content

    def test_file(self, path):
        return bool(TEST_FILE_DIR.search(path) or TEST_FILE_SUFFIX.search(path))

    def line_count(self, relative_path):
        content = self.safe_read(relative_path)
        return ruby_lines_count(content) if content is not None else 0

    # -- dimensions 1..14 --

    def analyze_commit_discipline(self):
        commits = self.commits
        if not commits:
            return {"status": "no_git_data"}
        conventional = sum(1 for c in commits
                           if CONVENTIONAL_PATTERN.match(c["subject"]))
        reverts = sum(1 for c in commits
                      if re.match(r"revert", c["subject"], re.I))
        atomic = sum(1 for c in commits if len(c["files"]) <= 20)
        sizes = [sum(f["added"] + f["deleted"] for f in c["files"]) for c in commits]
        avg_size = ruby_round(sum(sizes) / len(sizes), 0) if sizes else 0
        return {
            "total_commits": len(commits),
            "conventional_commit_ratio": ratio(conventional, len(commits)),
            "revert_ratio": ratio(reverts, len(commits)),
            "atomic_commit_ratio": ratio(atomic, len(commits)),
            "avg_commit_size_lines": avg_size,
        }

    def analyze_test_quality(self):
        all_test_files = [f for f in self.files if self.test_file(f)]
        test_source = [f for f in self.readable_source_files() if self.test_file(f)]
        non_test_source = [f for f in self.readable_source_files()
                           if not self.test_file(f)]
        test_loc = sum(self.line_count(f) for f in test_source)
        prod_loc = sum(self.line_count(f) for f in non_test_source)
        test_dirs = list(dict.fromkeys(f.split("/")[0] for f in all_test_files))
        frameworks = []
        for name, rx in [
            ("rspec", re.compile(r"spec.*_spec\.rb$")),
            ("jest", re.compile(r"\.test\.[jt]sx?$")),
            ("pytest", re.compile(r"test_.*\.py$")),
            ("minitest", re.compile(r"test.*_test\.rb$")),
            ("go_test", re.compile(r"_test\.go$")),
            ("bats", re.compile(r"\.bats$")),
        ]:
            if any(rx.search(f) for f in self.files):
                frameworks.append(name)
        return {
            "test_file_count": len(all_test_files),
            "test_ratio": ratio(test_loc, prod_loc),
            "test_dirs": len(test_dirs),
            "has_factories": any(re.search(r"factories/|factory\.", f)
                                 for f in self.files),
            "has_fixtures": any(re.search(r"fixtures/|fixture", f)
                                for f in self.files),
            "test_frameworks": frameworks,
        }

    def analyze_code_quality(self):
        if not self.source_files():
            return {"status": "no_source_files"}
        lengths = [self.line_count(f) for f in self.readable_source_files()]
        return {
            "total_source_files": len(self.source_files()),
            "avg_file_length": ruby_round(sum(lengths) / len(lengths), 0),
            "median_file_length": median(lengths),
            "god_object_count": sum(1 for l in lengths if l > 500),
            "long_file_count": sum(1 for l in lengths if l > 300),
            "max_file_length": max(lengths) if lengths else 0,
        }

    def analyze_error_handling(self):
        rescue_total = rescue_specific = rescue_generic = 0
        has_retry = False
        retry_rx = re.compile(r"retry|retries|backoff|exponential", re.I)
        for f in self.readable_source_files():
            ext = os.path.splitext(f)[1]
            if ext not in RESCUE_LANGUAGES:
                continue
            content = self.safe_read(f)
            if content is None:
                continue
            if ext == ".rb":
                bare = scan_count(RESCUE_PATTERNS["ruby_bare"], content)
                standard = scan_count(RESCUE_PATTERNS["ruby_standard"], content)
                specific = scan_count(RESCUE_PATTERNS["ruby_specific"], content) - standard
                rescue_generic += bare + standard
                rescue_specific += max(specific, 0)
                rescue_total += bare + standard + max(specific, 0)
            elif ext in (".js", ".jsx", ".ts", ".tsx"):
                catches = scan_count(RESCUE_PATTERNS["js_catch"], content)
                rescue_total += catches
                rescue_specific += catches  # JS catch is always specific
            elif ext == ".py":
                bare = scan_count(RESCUE_PATTERNS["python_bare"], content)
                exception = scan_count(RESCUE_PATTERNS["python_exception"], content)
                specific = scan_count(RESCUE_PATTERNS["python_specific"], content) - exception
                rescue_generic += bare + exception
                rescue_specific += max(specific, 0)
                rescue_total += bare + exception + max(specific, 0)
            if retry_rx.search(content):
                has_retry = True
        return {
            "rescue_total": rescue_total,
            "rescue_specific": rescue_specific,
            "rescue_generic": rescue_generic,
            "bare_rescue_ratio": ratio(rescue_generic, rescue_total),
            "has_retry_logic": has_retry,
        }

    def analyze_security_signals(self):
        eval_count = exec_count = sql_interpolation = hardcoded = 0
        eval_rx = re.compile(r"\beval\s*\(")
        exec_rx = re.compile(r"\b(?:exec|system|popen|spawn)\s*\(")
        sql_rx = re.compile(r"\bwhere\s*\(\s*\"[^\"]*#\{")
        secret_rx = re.compile(
            r"(?:password|secret|api_key|token)\s*=\s*[\"'][^\"']{8,}[\"']", re.I)
        for f in self.readable_source_files():
            if self.test_file(f):
                continue
            content = self.safe_read(f)
            if content is None:
                continue
            eval_count += scan_count(eval_rx, content)
            exec_count += scan_count(exec_rx, content)
            sql_interpolation += scan_count(sql_rx, content)
            hardcoded += scan_count(secret_rx, content)
        sec_cfg_rx = re.compile(r"security|cors|csp|rack.attack|rate.limit", re.I)
        has_security_config = any(
            sec_cfg_rx.search(f) and not self.test_file(f) for f in self.files)
        return {
            "eval_usage": eval_count,
            "exec_usage": exec_count,
            "sql_interpolation_count": sql_interpolation,
            "hardcoded_secret_patterns": hardcoded,
            "has_security_config": has_security_config,
        }

    def analyze_architecture(self):
        arch = self.profiler.analyze_architecture()
        return {
            "components": arch,
            "total_components": sum(arch.values()),
            "component_types": len(arch),
            "has_separation_of_concerns": len(arch) >= 3,
        }

    def analyze_documentation(self):
        docs = self.profiler.analyze_documentation()
        return {
            "doc_count": docs["doc_count"],
            "has_readme": any(re.match(r"README", f, re.I) for f in self.files),
            "has_architecture_doc": docs["has_architecture_doc"],
            "has_design_docs": docs["has_design_docs"],
            "has_testing_doc": docs["has_testing_doc"],
            "has_changelog": any(re.match(r"CHANGELOG", f, re.I) for f in self.files),
        }

    def analyze_agent_configs(self):
        return self.profiler.analyze_agent_configs()

    def analyze_infrastructure(self):
        signals = self.profiler.detect_infrastructure_from_tree()
        # NOTE: "linter" is read but never SET by detect_infrastructure_from_tree
        # — has_linter is always false. Verbatim Ruby quirk, kept faithfully.
        return {
            "has_docker": signals.get("docker", False),
            "has_ci": signals.get("ci", False),
            "has_monitoring": signals.get("monitoring", False),
            "has_linter": signals.get("linter", False),
        }

    def analyze_dependencies(self):
        lock_rx = re.compile(
            r"Gemfile\.lock|package-lock\.json|yarn\.lock|pnpm-lock"
            r"|poetry\.lock|go\.sum", re.I)
        return {
            "dependency_count": self.profiler.count_dependencies(),
            "frameworks": self.profiler.detect_frameworks(),
            "has_lockfile": any(lock_rx.search(f) for f in self.files),
        }

    def analyze_git_workflow(self):
        commits = self.commits
        if not commits:
            return {"status": "no_git_data"}
        version_rx = re.compile(r"bump|version|release|v\d+\.\d+", re.I)
        version_commits = sum(1 for c in commits if version_rx.search(c["subject"]))
        dates = list(dict.fromkeys(
            c["date"].split("T")[0] for c in commits if c.get("date")))
        return {
            "version_commits": version_commits,
            "active_days": len(dates),
            "commits_per_active_day": ratio(len(commits), len(dates)) if dates else 0,
        }

    def analyze_code_evolution(self):
        commits = self.commits
        if not commits:
            return {"status": "no_git_data"}
        total_added = total_deleted = refactor_commits = 0
        for c in commits:
            total_added += sum(f["added"] for f in c["files"])
            total_deleted += sum(f["deleted"] for f in c["files"])
            if re.search(r"refactor", c["subject"], re.I):
                refactor_commits += 1
        return {
            "total_lines_added": total_added,
            "total_lines_deleted": total_deleted,
            "deletion_ratio": ratio(total_deleted, total_added + total_deleted),
            "refactor_commit_ratio": ratio(refactor_commits, len(commits)),
            "net_loc_change": total_added - total_deleted,
        }

    def analyze_performance(self):
        flags = {"has_caching": False, "has_indexing": False,
                 "has_pagination": False, "has_connection_pooling": False,
                 "has_eager_loading": False}
        rxs = {
            "has_caching": re.compile(r"cache|memoize|Rails\.cache|redis", re.I),
            "has_indexing": re.compile(
                r"\badd_index\b|\bcreate_index\b|\bADD INDEX\b", re.I),
            "has_pagination": re.compile(
                r"paginate|pagy|kaminari|will_paginate|limit.*offset", re.I),
            "has_connection_pooling": re.compile(
                r"connection_pool|pool_size|ConnectionPool", re.I),
            "has_eager_loading": re.compile(
                r"includes\(|preload\(|eager_load\(", re.I),
        }
        for f in self.readable_source_files():
            content = self.safe_read(f)
            if content is None:
                continue
            for key, rx in rxs.items():
                if not flags[key] and rx.search(content):
                    flags[key] = True
        return flags

    def analyze_production_readiness(self):
        def any_content(rx):
            for f in self.readable_source_files():
                content = self.safe_read(f)
                if content is not None and rx.search(content):
                    return True
            return False

        has_error_tracking = (
            any(re.search(r"sentry|bugsnag|rollbar|honeybadger|airbrake", f, re.I)
                for f in self.files)
            or any_content(re.compile(r"Sentry|Bugsnag|Rollbar|Honeybadger", re.I)))
        has_logging = any_content(
            re.compile(r"logger|Rails\.logger|console\.log|logging", re.I))
        has_health_check = (
            any(re.search(r"health|ping|heartbeat", f, re.I) for f in self.files)
            or any_content(re.compile(r"health_check|healthz|readiness", re.I)))
        has_rate_limiting = (
            any(re.search(r"rack.attack|rate.limit|throttle", f, re.I)
                for f in self.files)
            or any_content(re.compile(r"Rack::Attack|rate_limit|throttle", re.I)))
        has_env_config = any(
            re.search(r"\.env\.example|\.env\.sample|env\.yml", f)
            for f in self.files)
        return {
            "has_error_tracking": has_error_tracking,
            "has_logging": has_logging,
            "has_health_check": has_health_check,
            "has_rate_limiting": has_rate_limiting,
            "has_env_config": has_env_config,
        }

    # -- profile section (CodebaseProfiler pieces, for the report) --

    def profile(self):
        if self.profiler is None:
            return {"status": "no_repo" if not os.path.isdir(self.repo_path)
                    else "empty_repo"}
        return {
            "project_framework": self.profiler.detect_project_framework(),
            "frameworks": self.profiler.detect_frameworks(),
            "dependency_count": self.profiler.count_dependencies(),
            "claude_md_stats": self.profiler.analyze_claude_md(),
            "agent_config_stats": self.profiler.analyze_agent_configs(),
            "architecture_signals": self.profiler.analyze_architecture(),
            "documentation": self.profiler.analyze_documentation(),
            "infra_signals": self.profiler.detect_infrastructure_from_tree(),
        }


# --- VelocityMetricsService port (numstat path; diff fallback not needed) ---

def compute_velocity(commits):
    """velocity_metrics_service.rb#compute over collect_commits output.
    Returns {} when there are no commits (Ruby: no_data_sources?)."""
    if not commits:
        return {}
    ins = sum(f["added"] for c in commits for f in c["files"])
    dels = sum(f["deleted"] for c in commits for f in c["files"])
    test_ins = sum(f["added"] for c in commits for f in c["files"]
                   if TEST_PATH_PATTERN.search(str(f.get("path") or "")))
    test_dels = sum(f["deleted"] for c in commits for f in c["files"]
                    if TEST_PATH_PATTERN.search(str(f.get("path") or "")))

    dates = [d for d in (parse_date(c.get("date")) for c in commits) if d]
    date_range_days = ((max(dates) - min(dates)).days + 1) if dates else 0
    loc_per_day = (ins - dels) // max(date_range_days, 1)  # Ruby Integer#/

    by_author = {}
    for c in commits:
        author = c.get("author") or "unknown"
        slot = by_author.setdefault(
            author, {"insertions": 0, "deletions": 0, "commits": 0})
        slot["insertions"] += sum(f["added"] for f in c["files"])
        slot["deletions"] += sum(f["deleted"] for f in c["files"])
        slot["commits"] += 1

    by_date = {}
    for c in commits:
        d = parse_date(c.get("date"))
        if not d:
            continue
        key = str(d)
        slot = by_date.setdefault(
            key, {"date": key, "insertions": 0, "deletions": 0, "commits": 0})
        slot["insertions"] += sum(f["added"] for f in c["files"])
        slot["deletions"] += sum(f["deleted"] for f in c["files"])
        slot["commits"] += 1
    daily = sorted(by_date.values(), key=lambda d: d["date"])
    peak = max(daily, key=lambda d: d["insertions"]) if daily else None

    # ship_to_revert: Ruby reads a precomputed git_metrics["reverts"] counter.
    # Locally we derive reverts with the SAME rule commit_discipline uses
    # (subject matches /\Arevert/i) — documented substitution.
    reverts = sum(1 for c in commits if re.match(r"revert", c["subject"], re.I))
    total = len(commits)
    ship_to_revert = ratio(total - reverts, total) if total else None

    return {
        "insertions": ins,
        "deletions": dels,
        "net_loc": ins - dels,
        "loc_per_day": loc_per_day,
        "test_insertions": test_ins,
        "test_deletions": test_dels,
        "test_ratio": ratio(test_ins, ins),
        "authors": by_author,
        "peak_day": peak,
        "daily_loc": daily,
        "ship_to_revert_ratio": ship_to_revert,
        "data_source": "numstat",
        "date_range_days": date_range_days,
    }


def compute_velocity_for_author(commits, author_names, author_emails):
    """velocity_metrics_service.rb#compute_for_author — case-insensitive
    match on author name OR email. Returns {} when nothing matches."""
    if not commits:
        return {}
    name_set = {n.lower() for n in author_names}
    email_set = {e.lower() for e in author_emails if e}
    filtered = [c for c in commits
                if (c.get("author") or "").lower() in name_set
                or (c.get("email") or "").lower() in email_set]
    if not filtered:
        return {}
    ins = sum(f["added"] for c in filtered for f in c["files"])
    dels = sum(f["deleted"] for c in filtered for f in c["files"])
    dates = [d for d in (parse_date(c.get("date")) for c in filtered) if d]
    days = ((max(dates) - min(dates)).days + 1) if dates else 1
    test_ins = sum(f["added"] for c in filtered for f in c["files"]
                   if TEST_PATH_PATTERN.search(str(f.get("path") or "")))
    return {
        "insertions": ins,
        "deletions": dels,
        "net_loc": ins - dels,
        "loc_per_day": (ins - dels) // max(days, 1),
        "test_insertions": test_ins,
        "test_ratio": ratio(test_ins, ins),
        "commits": len(filtered),
        "date_range_days": days,
    }


# --- SteeringTraceExtractor port ---

def extract_steering_trace(user_events):
    """Per-session action classification of user_directive events.
    Mirrors extract_trace_from_events: strip → truncate(500) → match all 10
    patterns; actions capped at 100, total_actions uncapped."""
    actions = []
    action_counts = {}
    for idx, event in enumerate(user_events):
        text = str(event.get("text") or "").strip()
        text = rails_truncate(text, 500)
        if not text:
            continue
        for action_type, pattern in STEERING_ACTIONS:
            if pattern.search(text):
                actions.append({"type": action_type, "line": idx,
                                "text": rails_truncate(text, 200)})
                action_counts[action_type] = action_counts.get(action_type, 0) + 1
    return {
        "actions": actions[:STEERING_ACTIONS_CAP],
        "action_counts": action_counts,
        "total_actions": len(actions),
    }


def analyze_steering(sessions):
    per_session = {}
    totals = {}
    total_actions = 0
    for i, session in enumerate(sessions):
        sid = str(session.get("session_id") or session.get("id") or f"session_{i}")
        user_events = [e for e in (session.get("events") or [])
                       if isinstance(e, dict) and e.get("type") == "user_directive"]
        trace = (extract_steering_trace(user_events) if user_events
                 else {"actions": [], "action_counts": {}, "total_actions": 0})
        per_session[sid] = {"action_counts": trace["action_counts"],
                            "total_actions": trace["total_actions"]}
        for k, v in trace["action_counts"].items():
            totals[k] = totals.get(k, 0) + v
        total_actions += trace["total_actions"]
    return {
        "per_session": per_session,
        "totals": {
            "action_counts": totals,
            "total_actions": total_actions,
            "sessions_analyzed": len(per_session),
            "sessions_with_actions": sum(
                1 for t in per_session.values() if t["total_actions"] > 0),
        },
    }


# --- ParallelismAnalyzer port (local degradations documented up top) ---

def parent_committed_after_return(events):
    """ParallelismAnalyzer.parent_committed_after_return?: a git_commit event
    positioned after the FIRST subagent_return."""
    if not events:
        return False
    first_return_idx = next(
        (i for i, e in enumerate(events) if e.get("type") == "subagent_return"),
        None)
    if first_return_idx is None:
        return False
    return any(e.get("type") == "git_commit"
               for e in events[first_return_idx + 1:])


def analyze_parallelism(sessions):
    mains = [s for s in sessions
             if not s.get("is_subagent") and not s.get("parent_session_id")]
    subagents = [s for s in sessions if s.get("is_subagent")]

    dispatch_count = sum(
        to_i((s.get("dispatch_metadata") or {}).get("dispatch_count"))
        for s in mains)
    return_count = sum(
        to_i((s.get("dispatch_metadata") or {}).get("return_count"))
        for s in mains)

    committed = 0
    for main in mains:
        events = [e for e in (main.get("events") or []) if isinstance(e, dict)]
        dispatches = [e for e in events if e.get("type") == "subagent_dispatch"]
        if not dispatches:
            continue
        return_ids = {e.get("tool_use_id") for e in events
                      if e.get("type") == "subagent_return" and e.get("tool_use_id")}
        if not return_ids:
            continue
        if not any(d.get("tool_use_id") in return_ids for d in dispatches):
            continue
        # No child subagent session records exist locally, so the Ruby
        # predicate's children-shipped branch can never fire; the
        # parent-commit-after-return fallback is the operative path.
        if parent_committed_after_return(events):
            committed += 1

    return {
        "dispatch_count": dispatch_count,
        "return_count": return_count,
        "dispatch_with_committed_return_count": committed,
        "subagent_count": len(subagents),
        "orphan_subagent_count": sum(
            1 for s in subagents if not s.get("parent_session_id")),
        # Require child subagent sessions / commit_group_sessions links that
        # don't exist locally — null with a note, not fake zeros.
        "concurrent_pairs_with_ships_count": None,
        "review_separation_count": None,
        "notes": [
            "dispatch_with_committed_return_count uses the parent-commit-"
            "after-return fallback only: child subagent session records do "
            "not exist locally, so the children-shipped branch never fires.",
            "concurrent_pairs_with_ships_count and review_separation_count "
            "need child subagent sessions with active-window overlap / "
            "shared commit groups; not derivable locally — emitted as null.",
            "cross_tool_delegation_count / cross_tool_child_completed_count "
            "omitted: cross-tool child narratives are server-side only.",
        ],
    }


# --- report assembly ---

UPLOAD_ONLY_NOTE = (
    "Upload-only analytics: a transparency report of the deterministic "
    "metrics Paxel's client computes and uploads. They never feed the local "
    "Paxel-skill score.")


def load_sessions(path, since=None):
    sessions = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                sessions.append(obj)
    if since:
        cutoff = parse_datetime(since)
        if cutoff:
            kept = []
            for s in sessions:
                stamps = [parse_datetime(e.get("timestamp"))
                          for e in (s.get("events") or []) if isinstance(e, dict)]
                stamps = [t for t in stamps if t]
                # Keep timestamp-less sessions; drop only those provably older.
                if not stamps or max(stamps) >= cutoff:
                    kept.append(s)
            sessions = kept
    return sessions


def _guarded(fn):
    try:
        return fn()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def build_report(repo_path, sessions, since=None,
                 author_names=None, author_emails=None):
    commits = collect_commits(repo_path, since=since)
    analyzer = CodeQualityAnalyzer(repo_path, commits)

    code_quality = _guarded(analyzer.analyze)
    profile = _guarded(analyzer.profile)
    velocity = _guarded(lambda: compute_velocity(commits))
    if author_names or author_emails:
        velocity["author_velocity"] = _guarded(lambda: compute_velocity_for_author(
            commits, author_names or [], author_emails or []))
    steering = _guarded(lambda: analyze_steering(sessions))
    parallelism = _guarded(lambda: analyze_parallelism(sessions))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": os.path.abspath(repo_path),
        "since": since,
        "session_count": len(sessions),
        "_note": UPLOAD_ONLY_NOTE,
        "code_quality": code_quality,
        "velocity": velocity,
        "steering": steering,
        "parallelism": parallelism,
        "profile": profile,
    }


# --- markdown report ---

def _pct(value):
    return f"{value * 100:.1f}%" if isinstance(value, (int, float)) else "n/a"


def _dim(dims, key):
    d = dims.get(key)
    return d if isinstance(d, dict) else {}


def _flags(d):
    on = [k for k, v in d.items() if v is True]
    return ", ".join(on) if on else "none"


def render_markdown(report):
    lines = ["# Paxel Upload Analytics (local transparency report)", ""]
    lines.append(f"- Generated: {report['generated_at']}")
    lines.append(f"- Repo: `{report['repo']}`")
    if report.get("since"):
        lines.append(f"- Since: {report['since']}")
    lines.append(f"- Sessions analyzed: {report['session_count']}")
    lines.append("")
    lines.append(f"> {report['_note']}")
    lines.append("")

    cq = report.get("code_quality") or {}
    dims = cq.get("dimensions") or {}
    lines.append(f"## Code Quality — status: {cq.get('status', 'error')}")
    if cq.get("status") == "complete":
        lines.append(f"- Files analyzed: {cq.get('file_count')}")
        d = _dim(dims, "commit_discipline")
        if "total_commits" in d:
            lines.append(
                f"- Commit discipline: {d['total_commits']} commits, "
                f"conventional {_pct(d.get('conventional_commit_ratio'))}, "
                f"reverts {_pct(d.get('revert_ratio'))}, "
                f"atomic {_pct(d.get('atomic_commit_ratio'))}, "
                f"avg size {d.get('avg_commit_size_lines')} lines")
        d = _dim(dims, "test_quality")
        if d:
            lines.append(
                f"- Tests: {d.get('test_file_count', 0)} test files, "
                f"test/prod LOC ratio {d.get('test_ratio', 0)}, "
                f"frameworks: {', '.join(d.get('test_frameworks') or []) or 'none'}")
        d = _dim(dims, "code_quality")
        if "total_source_files" in d:
            lines.append(
                f"- Code: {d['total_source_files']} source files, "
                f"avg {d.get('avg_file_length')} / median "
                f"{d.get('median_file_length')} lines, "
                f"{d.get('god_object_count')} god objects (>500), "
                f"max {d.get('max_file_length')}")
        d = _dim(dims, "error_handling")
        if d:
            lines.append(
                f"- Error handling: {d.get('rescue_total', 0)} handlers, "
                f"bare/generic ratio {d.get('bare_rescue_ratio', 0)}, "
                f"retry logic: {d.get('has_retry_logic', False)}")
        d = _dim(dims, "security_signals")
        if d:
            lines.append(
                f"- Security: eval {d.get('eval_usage', 0)}, "
                f"exec {d.get('exec_usage', 0)}, "
                f"SQL interpolation {d.get('sql_interpolation_count', 0)}, "
                f"hardcoded-secret patterns {d.get('hardcoded_secret_patterns', 0)}")
        d = _dim(dims, "architecture")
        if d:
            lines.append(
                f"- Architecture: {d.get('total_components', 0)} components "
                f"across {d.get('component_types', 0)} types "
                f"(separation of concerns: {d.get('has_separation_of_concerns', False)})")
        d = _dim(dims, "documentation")
        if d:
            lines.append(
                f"- Docs: {d.get('doc_count', 0)} files, "
                f"README: {d.get('has_readme', False)}, "
                f"CHANGELOG: {d.get('has_changelog', False)}")
        d = _dim(dims, "agent_config_quality")
        if d:
            lines.append(
                f"- Agent configs: {', '.join(d.get('tools_configured') or []) or 'none'} "
                f"({d.get('total_word_count', 0)} words, "
                f"{d.get('total_constraint_count', 0)} constraints)")
        d = _dim(dims, "infrastructure")
        if d:
            lines.append(f"- Infrastructure: {_flags(d)}")
        d = _dim(dims, "dependency_management")
        if d:
            lines.append(
                f"- Dependencies: {d.get('dependency_count', 0)} declared, "
                f"lockfile: {d.get('has_lockfile', False)}, "
                f"frameworks: {', '.join(d.get('frameworks') or []) or 'none'}")
        d = _dim(dims, "git_workflow")
        if "active_days" in d:
            lines.append(
                f"- Git workflow: {d['active_days']} active days, "
                f"{d.get('commits_per_active_day')} commits/day, "
                f"{d.get('version_commits')} version commits")
        d = _dim(dims, "code_evolution")
        if "total_lines_added" in d:
            lines.append(
                f"- Evolution: +{d['total_lines_added']} / "
                f"-{d.get('total_lines_deleted')} "
                f"(net {d.get('net_loc_change')}), "
                f"deletion ratio {d.get('deletion_ratio')}, "
                f"refactor commits {_pct(d.get('refactor_commit_ratio'))}")
        d = _dim(dims, "performance_awareness")
        if d:
            lines.append(f"- Performance awareness: {_flags(d)}")
        d = _dim(dims, "production_readiness")
        if d:
            lines.append(f"- Production readiness: {_flags(d)}")
        errored = [k for k, v in dims.items()
                   if isinstance(v, dict) and v.get("status") == "error"]
        if errored:
            lines.append(f"- Dimensions errored: {', '.join(errored)}")
    elif cq.get("error"):
        lines.append(f"- Error: {cq['error']}")
    lines.append("")

    v = report.get("velocity") or {}
    lines.append("## Velocity")
    if v.get("insertions") is not None:
        lines.append(
            f"- LOC: +{v['insertions']} / -{v['deletions']} "
            f"(net {v['net_loc']}), {v['loc_per_day']} net LOC/day over "
            f"{v['date_range_days']} days")
        lines.append(
            f"- Test insertions: {v['test_insertions']} "
            f"(ratio {v['test_ratio']})")
        peak = v.get("peak_day") or {}
        if peak:
            lines.append(
                f"- Peak day: {peak.get('date')} "
                f"(+{peak.get('insertions')} / {peak.get('commits')} commits)")
        lines.append(f"- Authors: {len(v.get('authors') or {})}, "
                     f"ship-to-revert ratio: {v.get('ship_to_revert_ratio')}")
        av = v.get("author_velocity")
        if isinstance(av, dict) and av.get("insertions") is not None:
            lines.append(
                f"- Author-scoped: +{av['insertions']} / -{av['deletions']} "
                f"over {av['commits']} commits ({av['loc_per_day']} net LOC/day)")
    else:
        lines.append("- No git data (no commits found)")
    lines.append("")

    st = report.get("steering") or {}
    totals = (st.get("totals") or {})
    lines.append("## Steering")
    lines.append(
        f"- {totals.get('total_actions', 0)} steering actions across "
        f"{totals.get('sessions_with_actions', 0)}/"
        f"{totals.get('sessions_analyzed', 0)} sessions")
    counts = totals.get("action_counts") or {}
    if counts:
        top = sorted(counts.items(), key=lambda kv: -kv[1])
        lines.append("- Action mix: " + ", ".join(f"{k} {n}" for k, n in top))
    lines.append("")

    p = report.get("parallelism") or {}
    lines.append("## Parallelism")
    lines.append(
        f"- Dispatches: {p.get('dispatch_count', 0)}, "
        f"returns: {p.get('return_count', 0)}")
    lines.append(
        f"- Sessions with dispatch + matched return + commit after return: "
        f"{p.get('dispatch_with_committed_return_count', 0)} "
        f"(parent-commit fallback; see notes)")
    lines.append(
        "- concurrent_pairs_with_ships_count / review_separation_count: "
        "null (need child subagent session records that don't exist locally)")
    lines.append("")

    pf = report.get("profile") or {}
    lines.append("## Profile")
    if pf.get("status") in ("no_repo", "empty_repo", "error"):
        lines.append(f"- status: {pf.get('status')}")
    else:
        lines.append(f"- Project framework: {pf.get('project_framework')}")
        lines.append(
            f"- Frameworks detected: "
            f"{', '.join(pf.get('frameworks') or []) or 'none'}; "
            f"{pf.get('dependency_count', 0)} dependencies")
        ac = pf.get("agent_config_stats") or {}
        lines.append(
            f"- Agent configs: {', '.join(ac.get('tools_configured') or []) or 'none'}")
        doc = pf.get("documentation") or {}
        lines.append(f"- Documentation files: {doc.get('doc_count', 0)}")
        infra = pf.get("infra_signals") or {}
        lines.append(
            f"- Infra signals: "
            f"{', '.join(sorted(k for k, on in infra.items() if on)) or 'none'}")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Paxel upload-only analytics (local transparency report)")
    ap.add_argument("--repo", required=True, help="repo path to analyze")
    ap.add_argument("--sessions", required=True,
                    help="sessions.jsonl (scripts/events.py output)")
    ap.add_argument("--since", help="ISO date/datetime lower bound "
                    "(git log --since + drops provably-older sessions)")
    ap.add_argument("--out", help="write report JSON here (default: stdout)")
    ap.add_argument("--md", help="write a readable markdown report here")
    ap.add_argument("--author", action="append", default=[],
                    help="author name for the author-scoped velocity variant "
                    "(repeatable)")
    ap.add_argument("--author-email", action="append", default=[],
                    help="author email for the author-scoped velocity variant "
                    "(repeatable)")
    args = ap.parse_args()

    if not os.path.isfile(args.sessions):
        sys.stderr.write(f"sessions file not found: {args.sessions}\n")
        sys.exit(2)

    sessions = load_sessions(args.sessions, since=args.since)
    report = build_report(args.repo, sessions, since=args.since,
                          author_names=args.author,
                          author_emails=args.author_email)

    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    else:
        sys.stdout.write(payload + "\n")
    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(report))


if __name__ == "__main__":
    main()

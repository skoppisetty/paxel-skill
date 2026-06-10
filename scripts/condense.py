#!/usr/bin/env python3
"""
condense.py — deterministic condensing of Claude Code and Codex CLI .jsonl
session logs.

Faithful port of Paxel's client-side condensing path (TranscriptChunker +
ToolInputSummarizer + SecretScrubber + CodexNormalizer/TranscriptFormatDetector
for Codex rollouts). This is the load-bearing determinism:
the scorer must see the SAME condensed text Paxel produces, not the raw
transcript. Feeding raw .jsonl would score longer, code-bearing text and drift.

What it reproduces (verbatim from the client image):
  - tool_result OUTPUT bodies dropped to "[ToolResult: N bytes]"
  - Write/Edit/MultiEdit content/diff bodies dropped to "[N bytes]" markers
  - Task/Agent prompts dropped to "[N bytes, sha=<sha256[:12]>]"
  - Bash/Read/Grep/Glob inputs kept as path/cmd, secret-scrubbed
  - secret scrubbing of surviving prose
  - MAX_TEXT_LENGTH=20000 per text block, MIN_CHUNK_TOKENS=200 drop, token≈chars/4

Codex rollout files (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl) are
auto-detected and normalized to Claude Code canonical entries first: injected
boilerplate dropped, apply_patch → per-file Write/Edit, update_plan → a
CODEX_PLAN.md plan signal (not a code edit), shell/exec_command → Bash.

Usage:
  python3 condense.py <file-or-dir> [<file-or-dir> ...]
Emits one JSON object per session to stdout (JSONL):
  {"session_id","path","agent_type","cwd","condensed_text","token_estimate","facts":{...}}
"""
import sys, os, re, json, glob, hashlib

# --- constants (verbatim from transcript_chunker.rb / episode_summarizer.rb) ---
MAX_TEXT_LENGTH = 20_000
MIN_CHUNK_TOKENS = 200          # sessions under ~800 chars are not scored
FIRST_PROMPT_LIMIT = 1_000
BASH_CMD_LIMIT = 160
BASH_DESC_LIMIT = 80
TASK_DESC_LIMIT = 120
TASK_SUBAGENT_LIMIT = 40

def est_tokens(s: str) -> int:
    return -(-len(s) // 4)        # ceil(len/4)

# --- secret scrubber (verbatim port of the 19 SecretScrubber PATTERNS) ---
# Same patterns, replacements, and ORDER as secret_scrubber.rb — ordering is
# load-bearing (anthropic before openai; vendor keys before bearer_token;
# env_var_secret last).
SECRET_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"), "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"\bAKIA[A-Z0-9]{16,}"), "[REDACTED_AWS_ACCESS_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{35}"), "[REDACTED_GOOGLE_API_KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[REDACTED_JWT]"),
    (re.compile(r"\b[Bb]earer[\s:=]+[A-Za-z0-9._\-~+/=]{20,}"), "Bearer [REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z0-9 ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\b(postgres(?:ql)?|redis|mongodb(?:\+srv)?|mysql|mssql|amqps?)://[^\s:@/]*:[^\s@]+@\S+"), r"\1://[REDACTED]@host"),
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}"), "[REDACTED_STRIPE_KEY]"),
    (re.compile(r"\b(?:xox[baprs]|xapp)-[A-Za-z0-9-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), "[REDACTED_HF_TOKEN]"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), "[REDACTED_NPM_TOKEN]"),
    (re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}"), "[REDACTED_PYPI_TOKEN]"),
    (re.compile(r"\byk_[0-9a-f]{16,}"), "[REDACTED_YC_TOKEN]"),
    (re.compile(r"\b(?:AC|SK)[0-9a-f]{32}\b"), "[REDACTED_TWILIO_KEY]"),
    (re.compile(r"\b1//0[A-Za-z0-9_-]{20,}"), "[REDACTED_GOOGLE_OAUTH]"),
    (re.compile(r"\bAccountKey=[A-Za-z0-9+/=]{40,}"), "AccountKey=[REDACTED]"),
    # env_var_secret stays last; lazy middle + plural suffix + value charset
    # stopping at '.' and the (?!\[REDACTED) lookahead are all load-bearing
    # (secret_scrubber.rb:148-181). The var NAME is preserved.
    (re.compile(r"\b([A-Z][A-Z0-9_]*?(?:SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|KEY)S?)\s*=\s*(?!\[REDACTED)[^\s\"'\n,;.]+"), r"\1=[REDACTED]"),
]

def scrub(text: str) -> str:
    # Fail-closed contract: never raise; non-string → "" (secret_scrubber.rb:13-16)
    if not isinstance(text, str) or not text:
        return ""
    try:
        for pat, repl in SECRET_PATTERNS:
            text = pat.sub(repl, text)
        return text
    except Exception:
        return ""

def clip(text, limit):
    text = scrub(str(text or ""))
    return text if len(text) <= limit else text[:limit] + "…"

# --- ToolInputSummarizer (verbatim rules from tool_input_summarizer.rb) ---
def summarize_tool_use(name, inp):
    inp = inp or {}
    def b(v):  # byte-count marker for dropped bodies
        return f"[{len(str(v if v is not None else '').encode('utf-8'))} bytes]"
    if name in ("Write",):
        return f"Write(file_path={clip(inp.get('file_path'),200)}, content={b(inp.get('content'))})"
    if name in ("Edit",):
        return (f"Edit(file_path={clip(inp.get('file_path'),200)}, "
                f"old_string={b(inp.get('old_string'))}, new_string={b(inp.get('new_string'))})")
    if name in ("MultiEdit", "NotebookEdit"):
        edits = inp.get("edits") or []
        return f"{name}(file_path={clip(inp.get('file_path') or inp.get('notebook_path'),200)}, edits=[{len(edits)} edits])"
    if name in ("Task", "Agent"):
        prompt = inp.get("prompt") or ""
        sha = hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()[:12]
        return (f"Task(description={clip(inp.get('description'),TASK_DESC_LIMIT)}, "
                f"subagent_type={clip(inp.get('subagent_type'),TASK_SUBAGENT_LIMIT)}, "
                f"prompt=[{len(str(prompt).encode('utf-8'))} bytes, sha={sha}])")
    if name == "Bash":
        return f"Bash(command={clip(inp.get('command'),BASH_CMD_LIMIT)}, description={clip(inp.get('description'),BASH_DESC_LIMIT)})"
    if name == "Read":
        return f"Read(file_path={clip(inp.get('file_path'),200)})"
    if name == "Grep":
        return f"Grep(pattern={clip(inp.get('pattern'),160)}, path={clip(inp.get('path'),200)})"
    if name == "Glob":
        return f"Glob(pattern={clip(inp.get('pattern'),200)})"
    # unknown tool: keys only, never values (drops bodies)
    keys = ", ".join(sorted((inp or {}).keys()))
    return f"{name}({keys})"

def blocks(content):
    """Normalize a message 'content' field to a list of block dicts.

    Claude Code usually stores content as a string or a list of block dicts,
    but a list can also carry bare strings; normalize those to text blocks and
    drop anything that is neither, so callers can assume every block is a dict.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, str):
                out.append({"type": "text", "text": b})
            elif isinstance(b, dict):
                out.append(b)
        return out
    return []

# --- Codex CLI support (port of TranscriptFormatDetector + CodexNormalizer) ---
# Codex rollout JSONL (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl) is converted
# to Claude Code canonical entries BEFORE condensing, exactly as Paxel does, so
# the same chunker rules and facts counters apply to both formats.

CODEX_PREAMBLE_ANCHOR = "You are Codex, a coding agent"
# Injected agent/safety boilerplate that Codex emits as user-role turns; counting
# it as user prompts inflates prompt metrics (codex_normalizer.rb:30-45).
CODEX_SYSTEM_PATTERNS = [
    "<permissions instructions>",
    "<environment_context>",
    "AGENTS.md instructions",
    "<skills_instructions>",
    "<turn_aborted>",
    "Filesystem sandboxing",
    "sandbox_mode",
    "The following is the Codex agent history whose request action you are assessing",
]
# Synthetic path for update_plan → Write, so plan activity is visible in the
# condensed text without registering as a code edit (codex_normalizer.rb:56-63).
CODEX_PLAN_PATH = "CODEX_PLAN.md"
CODEX_SKIP_TYPES = {"session_meta", "token_count", "task_started", "task_complete",
                    "turn_aborted", "web_search_call", "unknown"}
PATCH_BEGIN_MARKER = "*** Begin Patch"
PATCH_FILE_RE = re.compile(r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+)$",
                           re.MULTILINE)


def detect_format(path):
    """Return "codex_cli" or "claude_code" from the first parseable JSONL line.

    Codex (v0.115+) opens with type=session_meta — Claude Code never emits it.
    Detection must NOT key on originator alone: Codex launched from Claude Code
    writes originator="Claude Code" (transcript_format_detector.rb:71-83). The
    originator substring check only covers the pre-wrapper v0.92 format.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                # No NUL stripping here — mirrors read_first_lines'
                # line.scrub.strip (transcript_format_detector.rb:120-121)
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    first = json.loads(raw)
                except json.JSONDecodeError:
                    return "claude_code"
                if not isinstance(first, dict):
                    return "claude_code"
                if first.get("type") == "session_meta":
                    return "codex_cli"
                if "codex" in str(first.get("originator") or ""):
                    return "codex_cli"
                return "claude_code"
    except OSError:
        pass
    return "claude_code"


def _codex_payload(raw):
    """Unwrap an entry to its payload (wrapped v0.115+ vs raw v0.92)."""
    if "payload" in raw:
        return raw["payload"]
    if "type" in raw and "timestamp" in raw:
        return raw
    if "originator" in raw or ("id" in raw and "cwd" in raw):
        return {"type": "session_meta", **raw}
    return raw


def _codex_system_content(text):
    if text.lstrip().startswith(CODEX_PREAMBLE_ANCHOR):
        return True
    return any(p in text for p in CODEX_SYSTEM_PATTERNS)


def _codex_message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") in ("input_text", "text"))
    return ""


def _codex_extract_patch_text(args):
    """Patch envelope text iff this is a genuine apply_patch call (command[0]
    is the literal "apply_patch"), else None — a shell command that merely
    quotes the marker must not misfire (codex_normalizer.rb:271-276)."""
    cmd = args.get("cmd") or args.get("command")
    if not (isinstance(cmd, list) and cmd and str(cmd[0]) == "apply_patch"):
        return None
    patch = next((str(s) for s in cmd[1:] if PATCH_BEGIN_MARKER in str(s)), None)
    if patch is None:
        patch = "\n".join(str(s) for s in cmd[1:])
    return patch if PATCH_BEGIN_MARKER in patch else None


def _codex_render_plan(plan, explanation):
    lines = []
    for step in plan:
        if not isinstance(step, dict):
            continue
        text = str(step.get("step") or "").strip()
        if text:
            # Ruby: status.to_s.presence || "pending" — whitespace-only is blank
            status = str(step.get("status") or "").strip() or "pending"
            lines.append(f"- [{status}] {text}")
    if str(explanation or "").strip():
        lines.insert(0, str(explanation).strip())
    return "\n".join(lines)


def _codex_assistant_entry(blocks_):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks_}}


def _codex_patch_blocks(patch_text):
    """apply_patch envelope → one Write/Edit tool_use per file marker.

    "Add File" → Write (file create); "Update/Delete File"/"Move to" → Edit; a
    rename yields an Edit for both source and destination. Only the path is
    kept — the diff body never reaches the condensed text. [] if no markers.
    """
    return [
        {"type": "tool_use",
         "name": "Write" if op.startswith("Add") else "Edit",
         "input": {"file_path": path.strip()}}
        for op, path in PATCH_FILE_RE.findall(patch_text)
    ]


def _codex_command_str(command):
    """Ruby's command.to_s, byte-for-byte: Array#to_s is inspect format with
    double-quoted strings, which JSON matches for str/number/bool elements."""
    if isinstance(command, list):
        try:
            return json.dumps(command)
        except (TypeError, ValueError):
            return str(command)
    return str(command)


def _codex_convert_function_call(payload):
    name = str(payload.get("name") or "")
    try:
        args = json.loads(str(payload.get("arguments") or ""))
    except json.JSONDecodeError:
        # Ruby: JSON::ParserError → args = {} → falls through to Bash("[name]")
        args = {}
    if not isinstance(args, dict):
        # Ruby: args["cmd"] on a parsed Array raises TypeError, rescued by
        # convert_entry → the ENTRY is skipped (unlike a parse error above).
        return None

    # apply_patch → one Write/Edit per "*** <op> File:" marker so file paths
    # survive (the Bash form would ship the whole diff); a rename emits an Edit
    # for both source and destination. Falls through to Bash if no markers parse.
    patch_text = _codex_extract_patch_text(args)
    if patch_text:
        file_blocks = _codex_patch_blocks(patch_text)
        if file_blocks:
            return _codex_assistant_entry(file_blocks)

    # update_plan → synthetic Write at CODEX_PLAN_PATH (a plan signal; excluded
    # from the code_edits counter in condense_session).
    if name == "update_plan":
        plan = args.get("plan")
        if not (isinstance(plan, list) and plan):
            return None
        return _codex_assistant_entry([
            {"type": "tool_use", "name": "Write",
             "input": {"file_path": CODEX_PLAN_PATH,
                       "content": _codex_render_plan(plan, args.get("explanation"))}}])

    # Ruby || falls through on nil ONLY — ""/[] are truthy and must be kept.
    command = args.get("cmd")
    if command is None:
        command = args.get("command")
    if command is None:
        command = f"[{name}]"
    return _codex_assistant_entry([
        {"type": "tool_use", "name": "Bash",
         "input": {"command": _codex_command_str(command)}}])


def _codex_convert(payload):
    """Convert one Codex payload to a Claude Code canonical entry, or None."""
    ptype = payload.get("type")
    if ptype == "user_message":
        text = str(payload.get("message") or "")
        if not text.strip() or _codex_system_content(text):
            return None
        return {"type": "user", "message": {"role": "user", "content": text}}
    if ptype == "agent_message":
        text = str(payload.get("message") or "")
        if not text.strip():
            return None
        return {"type": "assistant", "message": {"role": "assistant", "content": text}}
    if ptype == "function_call":
        return _codex_convert_function_call(payload)
    if ptype == "custom_tool_call":
        # Newer Codex ships apply_patch as a custom tool with the envelope in a
        # top-level "input" field. NOT in the archived Paxel normalizer (its
        # case has no custom_tool_call branch) — deliberate divergence, else
        # every modern Codex session reads as 0 code edits.
        text = str(payload.get("input") or "")
        if payload.get("name") == "apply_patch" and PATCH_BEGIN_MARKER in text:
            file_blocks = _codex_patch_blocks(text)
            if file_blocks:
                return _codex_assistant_entry(file_blocks)
        return None
    if ptype == "function_call_output":
        output = str(payload.get("output") or "")
        if not output.strip():
            return None
        return {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": output}]}}
    if ptype == "reasoning":
        # Summary text only; encrypted-only reasoning is dropped. The thinking
        # entry contributes nothing to condensed TEXT (the chunker skips
        # thinking blocks, transcript_chunker.rb:414-415) but must exist in
        # the canonical stream like Paxel's.
        summary = payload.get("summary")
        text = ""
        if isinstance(summary, list) and summary:
            text = "\n".join(s.get("text") for s in summary
                             if isinstance(s, dict) and s.get("text") is not None)
        if not text.strip():
            return None
        return _codex_assistant_entry([{"type": "thinking", "thinking": text}])
    if ptype == "agent_reasoning":
        text = str(payload.get("text") or "")
        if not text.strip():
            return None
        return _codex_assistant_entry([{"type": "thinking", "thinking": text}])
    if ptype == "message":
        role = str(payload.get("role") or "")
        if role == "developer":
            return None
        text = _codex_message_text(payload.get("content"))
        if not text.strip() or _codex_system_content(text):
            return None
        canonical = "user" if role == "user" else "assistant"
        return {"type": canonical, "message": {"role": canonical, "content": text}}
    return None


def _codex_extract_metadata(payload, metadata):
    metadata.setdefault("session_id", payload.get("id"))
    metadata.setdefault("cwd", payload.get("cwd"))
    if payload.get("model_provider") is not None:
        metadata.setdefault("model", payload.get("model_provider"))
    git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
    if git.get("branch") is not None:
        metadata.setdefault("git_branch", git.get("branch"))
    if git.get("repository_url") is not None:
        metadata.setdefault("git_remote", git.get("repository_url"))


def normalize_codex(path):
    """Read a Codex rollout file → (canonical entries, metadata).

    metadata: session_id, cwd, and (when present) model, git_branch,
    git_remote — the same fields CodexNormalizer#extract_metadata captures.
    An IO error mid-file keeps the entries parsed so far rather than sinking
    the session (codex_normalizer.rb:138-145).
    """
    entries, metadata = [], {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                # No NUL stripping — CodexNormalizer reads line.scrub.strip
                # (codex_normalizer.rb:78-79); a raw NUL inside the line makes
                # JSON.parse fail and the line is skipped, same as here. Only
                # the Claude chunker path deletes NULs (transcript_chunker.rb:148).
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    raw = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict):
                    continue
                timestamp = raw.get("timestamp")
                payload = _codex_payload(raw)
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "session_meta" or raw.get("type") == "session_meta":
                    _codex_extract_metadata(payload, metadata)
                    continue
                # turn_context (model + approval_policy) carries no transcript content
                if "model" in payload and "approval_policy" in payload:
                    metadata.setdefault("model", payload.get("model"))
                    continue
                if str(payload.get("type")) in CODEX_SKIP_TYPES:
                    continue
                # One malformed entry must not sink the session — Ruby rescues
                # per entry in convert_entry (codex_normalizer.rb:195-198).
                try:
                    entry = _codex_convert(payload)
                except Exception:
                    continue
                if entry:
                    entry["timestamp"] = timestamp
                    entries.append(entry)
    except OSError:
        pass
    return entries, metadata


def condense_session(path):
    lines_out = []
    first_prompt = None
    n_user = n_assistant = n_tool_use = n_tool_result = n_dispatch = 0
    n_code_edits = n_git_commits = 0
    session_id = os.path.splitext(os.path.basename(path))[0]
    cwd = None

    agent_type = detect_format(path)
    try:
        if agent_type == "codex_cli":
            entries, codex_meta = normalize_codex(path)
            session_id = codex_meta.get("session_id") or session_id
            cwd = codex_meta.get("cwd")
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                entries = []
                for raw in f:
                    raw = raw.replace("\x00", "").strip()
                    if not raw:
                        continue
                    try:
                        entries.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
            # No session_meta for Claude Code (Codex branch above has one);
            # the first entry carrying a cwd wins.
            for e in entries:
                if isinstance(e, dict) and e.get("cwd"):
                    cwd = str(e["cwd"])
                    break
    except OSError:
        return None

    for e in entries:
        etype = e.get("type")
        msg = e.get("message") or {}
        role = msg.get("role") or etype
        if role == "user":
            n_user += 1
            for blk in blocks(msg.get("content")):
                t = blk.get("type")
                if t == "text":
                    txt = scrub(blk.get("text", ""))[:MAX_TEXT_LENGTH]
                    if txt.strip():
                        if first_prompt is None:
                            first_prompt = txt[:FIRST_PROMPT_LIMIT]
                        lines_out.append(f"USER: {txt}")
                elif t == "tool_result":
                    body = blk.get("content")
                    # NOTE: byte count is over json.dumps(body); for a string body this
                    # includes the surrounding quotes/escapes, so the marker may differ
                    # by a few bytes from Paxel's own count. Representative, not exact.
                    nbytes = len(json.dumps(body).encode("utf-8")) if body is not None else 0
                    n_tool_result += 1
                    lines_out.append(f"[ToolResult: {nbytes} bytes]")
        elif role == "assistant":
            n_assistant += 1
            for blk in blocks(msg.get("content")):
                t = blk.get("type")
                if t == "text":
                    txt = scrub(blk.get("text", ""))[:MAX_TEXT_LENGTH]
                    if txt.strip():
                        lines_out.append(f"ASSISTANT: {txt}")
                elif t == "tool_use":
                    name = blk.get("name", "Tool")
                    inp = blk.get("input") or {}
                    n_tool_use += 1
                    if name in ("Task", "Agent"):
                        n_dispatch += 1
                    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                        # The synthetic Codex plan write is a plan signal, not a
                        # code edit — counting it would flip plan-only sessions
                        # to "shipping" (codex_normalizer.rb:300-303).
                        if not (agent_type == "codex_cli"
                                and inp.get("file_path") == CODEX_PLAN_PATH):
                            n_code_edits += 1
                    if name == "Bash" and re.search(r"\bgit\s+commit\b", str(inp.get("command") or "")):
                        n_git_commits += 1
                    lines_out.append(f"TOOL_USE: {summarize_tool_use(name, inp)}")

    condensed = "\n".join(lines_out)
    tokens = est_tokens(condensed)
    return {"session_id": session_id, "path": path, "agent_type": agent_type,
            "cwd": cwd, "condensed_text": condensed,
            "token_estimate": tokens, "too_short": tokens < MIN_CHUNK_TOKENS,
            "facts": {"user_messages": n_user, "assistant_messages": n_assistant,
                      "tool_uses": n_tool_use, "tool_results": n_tool_result,
                      "subagent_dispatches": n_dispatch, "code_edits": n_code_edits,
                      "git_commits": n_git_commits, "first_prompt": first_prompt}}

def iter_paths(args):
    for a in args:
        if os.path.isdir(a):
            yield from sorted(glob.glob(os.path.join(a, "**", "*.jsonl"), recursive=True))
        elif a.endswith(".jsonl") and os.path.isfile(a):
            yield a

def main():
    if len(sys.argv) < 2:
        sys.stderr.write(__doc__)
        sys.exit(2)
    for p in iter_paths(sys.argv[1:]):
        # One malformed session must never abort the batch; skip and report it.
        try:
            out = condense_session(p)
        except Exception as ex:
            sys.stderr.write(f"[skip] {p}: {type(ex).__name__}: {ex}\n")
            continue
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    main()

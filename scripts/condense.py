#!/usr/bin/env python3
"""
condense.py — deterministic condensing of Claude Code .jsonl session logs.

Faithful port of Paxel's client-side condensing path (TranscriptChunker +
ToolInputSummarizer + SecretScrubber). This is the load-bearing determinism:
the scorer must see the SAME condensed text Paxel produces, not the raw
transcript. Feeding raw .jsonl would score longer, code-bearing text and drift.

What it reproduces (verbatim from the client image):
  - tool_result OUTPUT bodies dropped to "[ToolResult: N bytes]"
  - Write/Edit/MultiEdit content/diff bodies dropped to "[N bytes]" markers
  - Task/Agent prompts dropped to "[N bytes, sha=<sha256[:12]>]"
  - Bash/Read/Grep/Glob inputs kept as path/cmd, secret-scrubbed
  - secret scrubbing of surviving prose
  - MAX_TEXT_LENGTH=20000 per text block, MIN_CHUNK_TOKENS=200 drop, token≈chars/4

Usage:
  python3 condense.py <file-or-dir> [<file-or-dir> ...]
Emits one JSON object per session to stdout (JSONL):
  {"session_id","path","condensed_text","token_estimate","facts":{...}}
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

# --- secret scrubber (representative port of the 22 SecretScrubber patterns) ---
# Low score-impact for a dev who never pastes a real key, but kept for fidelity.
SECRET_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"sk-(?:proj|svcacct|admin)?-?[A-Za-z0-9_\-]{20,}"), "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"AKIA[0-9A-Z]{16,}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_PAT]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}"), "[REDACTED_STRIPE_KEY]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"hf_[A-Za-z0-9]{20,}"), "[REDACTED_HF_TOKEN]"),
    (re.compile(r"npm_[A-Za-z0-9]{30,}"), "[REDACTED_NPM_TOKEN]"),
    (re.compile(r"yk_[0-9a-f]{16,}"), "[REDACTED_YC_TOKEN]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    # env_var_secret runs last (same ordering note as the original)
    (re.compile(r"\b[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|KEY)S?\s*=\s*\S+"), "[REDACTED_ENV_SECRET]"),
]

def scrub(text: str) -> str:
    if not isinstance(text, str) or not text:
        return ""
    for pat, repl in SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text

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

def condense_session(path):
    lines_out = []
    first_prompt = None
    n_user = n_assistant = n_tool_use = n_tool_result = n_dispatch = 0
    n_code_edits = n_git_commits = 0
    session_id = os.path.splitext(os.path.basename(path))[0]

    try:
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
                        n_code_edits += 1
                    if name == "Bash" and re.search(r"\bgit\s+commit\b", str(inp.get("command") or "")):
                        n_git_commits += 1
                    lines_out.append(f"TOOL_USE: {summarize_tool_use(name, inp)}")

    condensed = "\n".join(lines_out)
    tokens = est_tokens(condensed)
    if tokens < MIN_CHUNK_TOKENS:
        return {"session_id": session_id, "path": path, "condensed_text": condensed,
                "token_estimate": tokens, "too_short": True,
                "facts": {"user_messages": n_user, "assistant_messages": n_assistant,
                          "tool_uses": n_tool_use, "tool_results": n_tool_result,
                          "subagent_dispatches": n_dispatch, "code_edits": n_code_edits, "git_commits": n_git_commits, "first_prompt": first_prompt}}
    return {"session_id": session_id, "path": path, "condensed_text": condensed,
            "token_estimate": tokens, "too_short": False,
            "facts": {"user_messages": n_user, "assistant_messages": n_assistant,
                      "tool_uses": n_tool_use, "tool_results": n_tool_result,
                      "subagent_dispatches": n_dispatch, "code_edits": n_code_edits, "git_commits": n_git_commits, "first_prompt": first_prompt}}

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

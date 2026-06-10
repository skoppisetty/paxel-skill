#!/usr/bin/env python3
"""
Golden + regression tests for the condense/aggregate scripts.

Stdlib only (unittest) — no third-party deps, matching the README's
"Python 3 stdlib" promise. Run from anywhere:

    python3 scripts/test_scripts.py            # or: python3 -m unittest -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import condense  # noqa: E402
import aggregate  # noqa: E402


def write_session(lines):
    """Write JSONL entries to a temp .jsonl and return its path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
    return path


class CondenseUnitTests(unittest.TestCase):
    def test_est_tokens_is_ceil_div_4(self):
        self.assertEqual(condense.est_tokens(""), 0)
        self.assertEqual(condense.est_tokens("a"), 1)       # ceil(1/4)
        self.assertEqual(condense.est_tokens("abcd"), 1)
        self.assertEqual(condense.est_tokens("abcde"), 2)   # ceil(5/4)

    def test_scrub_redacts_known_secret_shapes(self):
        self.assertEqual(condense.scrub("key sk-ant-" + "A" * 30),
                         "key [REDACTED_ANTHROPIC_KEY]")
        self.assertIn("[REDACTED_AWS_ACCESS_KEY]", condense.scrub("AKIA" + "B" * 16))
        # env_var_secret preserves the variable NAME (secret_scrubber.rb:178-181)
        self.assertEqual(condense.scrub("API_TOKEN=hunter2"), "API_TOKEN=[REDACTED]")
        self.assertEqual(condense.scrub("OPENAI_API_KEYS=abc-def"),
                         "OPENAI_API_KEYS=[REDACTED]")
        self.assertEqual(condense.scrub(None), "")

    def test_scrub_full_pattern_set(self):
        cases = [
            ("eyJhbGci123.eyJzdWIi456.sig-789xx", "[REDACTED_JWT]"),
            ("Bearer " + "a1" * 15, "Bearer [REDACTED]"),
            ("postgres://user:pass@host:5432/db", "postgres://[REDACTED]@host"),
            ("redis://:secret@cache:6379", "redis://[REDACTED]@host"),
            ("pypi-" + "x" * 24, "[REDACTED_PYPI_TOKEN]"),
            ("AC" + "0" * 32, "[REDACTED_TWILIO_KEY]"),
            ("1//0" + "r" * 24, "[REDACTED_GOOGLE_OAUTH]"),
            ("AccountKey=" + "B" * 44, "AccountKey=[REDACTED]"),
            ("AIza" + "c" * 35, "[REDACTED_GOOGLE_API_KEY]"),
            ("xapp-" + "d" * 12, "[REDACTED_SLACK_TOKEN]"),
        ]
        for raw, expected in cases:
            self.assertEqual(condense.scrub(raw), expected, msg=raw)

    def test_scrub_ordering_vendor_key_wins_inside_bearer(self):
        # anthropic_key runs before bearer_token, so the vendor replacement
        # wins and the bearer pattern no longer matches (secret_scrubber.rb:18-20).
        out = condense.scrub("Bearer sk-ant-" + "A" * 30)
        self.assertEqual(out, "Bearer [REDACTED_ANTHROPIC_KEY]")

    def test_blocks_normalizes_bare_strings_and_drops_junk(self):
        self.assertEqual(condense.blocks("hi"), [{"type": "text", "text": "hi"}])
        self.assertEqual(
            condense.blocks(["bare", {"type": "text", "text": "dict"}, 42, None]),
            [{"type": "text", "text": "bare"}, {"type": "text", "text": "dict"}],
        )
        self.assertEqual(condense.blocks(7), [])

    def test_summarize_drops_write_and_edit_bodies(self):
        out = condense.summarize_tool_use(
            "Write", {"file_path": "a.py", "content": "x" * 1000})
        self.assertIn("file_path=a.py", out)
        self.assertIn("content=[1000 bytes]", out)
        self.assertNotIn("xxxx", out)  # body never leaks

        out = condense.summarize_tool_use(
            "Edit", {"file_path": "a.py", "old_string": "ab", "new_string": "abc"})
        self.assertIn("old_string=[2 bytes]", out)
        self.assertIn("new_string=[3 bytes]", out)

    def test_summarize_task_prompt_becomes_byte_sha_marker(self):
        out = condense.summarize_tool_use(
            "Task", {"description": "review", "subagent_type": "Explore",
                     "prompt": "secret plan"})
        self.assertIn("prompt=[11 bytes, sha=", out)
        self.assertNotIn("secret plan", out)

    def test_unknown_tool_emits_keys_not_values(self):
        out = condense.summarize_tool_use("MysteryTool", {"token": "s3cr3t", "url": "x"})
        self.assertEqual(out, "MysteryTool(token, url)")
        self.assertNotIn("s3cr3t", out)


class CondenseSessionTests(unittest.TestCase):
    def test_facts_and_markers_on_a_normal_session(self):
        path = write_session([
            {"type": "user", "message": {"role": "user", "content": "Add a retry wrapper."}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Doing it."},
                {"type": "tool_use", "name": "Edit",
                 "input": {"file_path": "f.py", "old_string": "a", "new_string": "bb"}},
            ]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "ok"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "git commit -m wip", "description": "commit"}},
                {"type": "tool_use", "name": "Task",
                 "input": {"description": "d", "subagent_type": "Explore", "prompt": "go"}},
            ]}},
        ])
        try:
            out = condense.condense_session(path)
        finally:
            os.remove(path)
        facts = out["facts"]
        self.assertEqual(facts["user_messages"], 2)
        self.assertEqual(facts["assistant_messages"], 2)
        self.assertEqual(facts["code_edits"], 1)
        self.assertEqual(facts["git_commits"], 1)
        self.assertEqual(facts["subagent_dispatches"], 1)
        self.assertEqual(facts["tool_results"], 1)
        self.assertIn("[ToolResult:", out["condensed_text"])
        self.assertIn("USER: Add a retry wrapper.", out["condensed_text"])

    def test_too_short_flag_below_min_tokens(self):
        path = write_session([
            {"type": "user", "message": {"role": "user", "content": "hi"}}])
        try:
            out = condense.condense_session(path)
        finally:
            os.remove(path)
        self.assertTrue(out["too_short"])
        self.assertLess(out["token_estimate"], condense.MIN_CHUNK_TOKENS)

    def test_bare_string_block_does_not_crash(self):
        # Regression: a list carrying a bare string used to raise AttributeError
        # and abort the whole batch.
        path = write_session([
            {"type": "user", "message": {"role": "user",
                                         "content": ["a bare string in a list"]}},
            {"type": "assistant", "message": {"role": "assistant",
                                              "content": [{"type": "text", "text": "ok"}]}},
        ])
        try:
            out = condense.condense_session(path)
        finally:
            os.remove(path)
        self.assertIn("USER: a bare string in a list", out["condensed_text"])

    def test_malformed_lines_are_skipped_not_fatal(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("{not json\n")
            f.write(json.dumps({"type": "user", "message": {
                "role": "user", "content": "real line"}}) + "\n")
        try:
            out = condense.condense_session(path)
        finally:
            os.remove(path)
        self.assertIn("USER: real line", out["condensed_text"])


class CodexFormatDetectionTests(unittest.TestCase):
    def test_new_format_session_meta_detects_codex(self):
        path = write_session([
            {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
             "payload": {"id": "s1", "cwd": "/p", "originator": "codex_cli_rs"}}])
        try:
            self.assertEqual(condense.detect_format(path), "codex_cli")
        finally:
            os.remove(path)

    def test_old_format_originator_detects_codex(self):
        path = write_session([
            {"id": "s1", "cwd": "/p", "originator": "codex_cli_rs"}])
        try:
            self.assertEqual(condense.detect_format(path), "codex_cli")
        finally:
            os.remove(path)

    def test_claude_session_detects_claude_code(self):
        path = write_session([
            {"type": "user", "message": {"role": "user", "content": "hi"}}])
        try:
            self.assertEqual(condense.detect_format(path), "claude_code")
        finally:
            os.remove(path)

    def test_codex_launched_from_claude_still_detects_codex(self):
        # originator is set by the LAUNCHING tool; detection must key on
        # type=session_meta, not originator (transcript_format_detector.rb:71-83).
        path = write_session([
            {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
             "payload": {"id": "s1", "cwd": "/p", "originator": "Claude Code"}}])
        try:
            self.assertEqual(condense.detect_format(path), "codex_cli")
        finally:
            os.remove(path)


class CodexCondenseTests(unittest.TestCase):
    @staticmethod
    def _session():
        ts = "2026-01-01T00:00:00Z"
        patch = ("*** Begin Patch\n"
                 "*** Update File: src/worker.py\n"
                 "@@\n-old\n+new\n"
                 "*** Add File: src/new_file.py\n"
                 "+content\n"
                 "*** End Patch")
        return [
            {"timestamp": ts, "type": "session_meta",
             "payload": {"id": "sess-1", "cwd": "/Volumes/Code/demo",
                         "originator": "codex_cli_rs"}},
            # injected boilerplate must never count as a user prompt
            {"timestamp": ts, "type": "event_msg",
             "payload": {"type": "user_message",
                         "message": "You are Codex, a coding agent running in the Codex CLI"}},
            {"timestamp": ts, "type": "event_msg",
             "payload": {"type": "user_message",
                         "message": "<environment_context>cwd=/x</environment_context>"}},
            {"timestamp": ts, "type": "event_msg",
             "payload": {"type": "user_message",
                         "message": "Fix the retry bug in worker.py"}},
            {"timestamp": ts, "type": "event_msg",
             "payload": {"type": "agent_message",
                         "message": "Looking at worker.py now."}},
            {"timestamp": ts, "type": "response_item",
             "payload": {"type": "reasoning",
                         "summary": [{"type": "summary_text", "text": "hidden chain"}]}},
            {"timestamp": ts, "type": "response_item",
             "payload": {"type": "function_call", "name": "shell",
                         "arguments": json.dumps(
                             {"command": ["bash", "-lc", "git commit -m fix"]})}},
            {"timestamp": ts, "type": "response_item",
             "payload": {"type": "function_call_output", "output": "committed ok"}},
            {"timestamp": ts, "type": "response_item",
             "payload": {"type": "function_call", "name": "shell",
                         "arguments": json.dumps({"command": ["apply_patch", patch]})}},
            {"timestamp": ts, "type": "response_item",
             "payload": {"type": "function_call", "name": "update_plan",
                         "arguments": json.dumps(
                             {"explanation": "plan v1",
                              "plan": [{"step": "fix retry", "status": "pending"}]})}},
            {"timestamp": ts, "type": "response_item",
             "payload": {"type": "message", "role": "developer",
                         "content": [{"type": "input_text", "text": "dev noise"}]}},
            {"timestamp": ts, "type": "event_msg",
             "payload": {"type": "token_count", "input_tokens": 5}},
        ]

    def _condense(self):
        path = write_session(self._session())
        try:
            return condense.condense_session(path)
        finally:
            os.remove(path)

    def test_metadata_and_agent_type(self):
        out = self._condense()
        self.assertEqual(out["agent_type"], "codex_cli")
        self.assertEqual(out["session_id"], "sess-1")
        self.assertEqual(out["cwd"], "/Volumes/Code/demo")

    def test_system_boilerplate_is_dropped(self):
        out = self._condense()
        self.assertNotIn("You are Codex", out["condensed_text"])
        self.assertNotIn("environment_context", out["condensed_text"])
        self.assertEqual(out["facts"]["first_prompt"],
                         "Fix the retry bug in worker.py")

    def test_user_and_agent_messages_map_to_canonical_lines(self):
        out = self._condense()
        self.assertIn("USER: Fix the retry bug in worker.py", out["condensed_text"])
        self.assertIn("ASSISTANT: Looking at worker.py now.", out["condensed_text"])

    def test_shell_call_maps_to_bash_and_counts_git_commit(self):
        out = self._condense()
        self.assertIn("Bash(command=", out["condensed_text"])
        self.assertEqual(out["facts"]["git_commits"], 1)

    def test_function_call_output_becomes_tool_result_marker(self):
        out = self._condense()
        self.assertIn("[ToolResult:", out["condensed_text"])
        self.assertNotIn("committed ok", out["condensed_text"])
        self.assertEqual(out["facts"]["tool_results"], 1)

    def test_apply_patch_emits_one_edit_or_write_per_file(self):
        out = self._condense()
        self.assertIn("Edit(file_path=src/worker.py", out["condensed_text"])
        self.assertIn("Write(file_path=src/new_file.py", out["condensed_text"])
        # the patch body itself must never reach the condensed text
        self.assertNotIn("Begin Patch", out["condensed_text"])
        self.assertEqual(out["facts"]["code_edits"], 2)

    def test_update_plan_is_a_plan_signal_not_a_code_edit(self):
        out = self._condense()
        self.assertIn("Write(file_path=CODEX_PLAN.md", out["condensed_text"])
        # code_edits stays 2 (the apply_patch files); the synthetic plan
        # write must not flip episode classification to "shipping".
        self.assertEqual(out["facts"]["code_edits"], 2)

    def test_reasoning_is_excluded_from_condensed_text(self):
        # transcript_chunker.rb:414-415 skips thinking blocks from condensed text.
        out = self._condense()
        self.assertNotIn("hidden chain", out["condensed_text"])

    def test_developer_messages_are_dropped(self):
        out = self._condense()
        self.assertNotIn("dev noise", out["condensed_text"])

    def test_array_arguments_skip_the_entry(self):
        # Ruby: args["cmd"] on a parsed Array raises TypeError, rescued by
        # convert_entry → entry skipped (codex_normalizer.rb:195-198). A parse
        # ERROR, by contrast, yields {} and falls through to Bash("[name]").
        path = write_session([
            {"timestamp": "t", "type": "session_meta",
             "payload": {"id": "s3", "cwd": "/p", "originator": "codex_cli_rs"}},
            {"timestamp": "t", "type": "response_item",
             "payload": {"type": "function_call", "name": "shell",
                         "arguments": "[1, 2]"}},
        ])
        try:
            out = condense.condense_session(path)
        finally:
            os.remove(path)
        self.assertNotIn("TOOL_USE", out["condensed_text"])

    def test_ruby_nil_only_fallback_keeps_empty_cmd(self):
        # Ruby || only falls through on nil; cmd="" must be kept, not
        # replaced by "[exec_command]".
        path = write_session([
            {"timestamp": "t", "type": "session_meta",
             "payload": {"id": "s4", "cwd": "/p", "originator": "codex_cli_rs"}},
            {"timestamp": "t", "type": "response_item",
             "payload": {"type": "function_call", "name": "exec_command",
                         "arguments": json.dumps({"cmd": ""})}},
        ])
        try:
            out = condense.condense_session(path)
        finally:
            os.remove(path)
        self.assertNotIn("[exec_command]", out["condensed_text"])
        self.assertIn("Bash(command=,", out["condensed_text"])

    def test_array_command_renders_ruby_inspect_style(self):
        # Ruby Array#to_s uses double quotes: ["bash", "-lc", ...] — the
        # condensed text must match byte-for-byte.
        path = write_session([
            {"timestamp": "t", "type": "session_meta",
             "payload": {"id": "s5", "cwd": "/p", "originator": "codex_cli_rs"}},
            {"timestamp": "t", "type": "response_item",
             "payload": {"type": "function_call", "name": "shell",
                         "arguments": json.dumps(
                             {"command": ["bash", "-lc", "echo hi"]})}},
        ])
        try:
            out = condense.condense_session(path)
        finally:
            os.remove(path)
        self.assertIn('Bash(command=["bash", "-lc", "echo hi"]', out["condensed_text"])

    def test_reasoning_counts_as_assistant_entry(self):
        # CodexNormalizer converts reasoning to canonical thinking entries;
        # only the chunker drops them from condensed TEXT. The entry stream
        # (and assistant_messages) must include them.
        out = self._condense()
        # agent_message + reasoning + shell + apply_patch + update_plan
        self.assertEqual(out["facts"]["assistant_messages"], 5)
        self.assertNotIn("hidden chain", out["condensed_text"])

    def test_normalize_codex_attaches_timestamps(self):
        path = write_session(self._session())
        try:
            entries, _ = condense.normalize_codex(path)
        finally:
            os.remove(path)
        self.assertTrue(entries)
        self.assertTrue(all(e.get("timestamp") == "2026-01-01T00:00:00Z"
                            for e in entries))

    def test_metadata_captures_model_and_git(self):
        path = write_session([
            {"timestamp": "t", "type": "session_meta",
             "payload": {"id": "s6", "cwd": "/p", "originator": "codex_cli_rs",
                         "model_provider": "openai",
                         "git": {"branch": "main",
                                 "repository_url": "git@github.com:a/b.git"}}},
            # turn_context must not override session_meta's model_provider
            {"timestamp": "t", "type": "turn_context",
             "payload": {"model": "gpt-5", "approval_policy": "never"}},
        ])
        try:
            _, meta = condense.normalize_codex(path)
        finally:
            os.remove(path)
        self.assertEqual(meta["model"], "openai")
        self.assertEqual(meta["git_branch"], "main")
        self.assertEqual(meta["git_remote"], "git@github.com:a/b.git")

    def test_render_plan_blank_status_defaults_to_pending(self):
        # Ruby: step["status"].to_s.presence || "pending" — whitespace-only
        # is blank, so it must render as pending.
        out = condense._codex_render_plan([{"step": "x", "status": "  "}], None)
        self.assertEqual(out, "- [pending] x")

    def test_custom_tool_call_apply_patch_counts_edits(self):
        # Newer Codex emits apply_patch as a custom_tool_call with the patch in
        # a top-level "input" field (observed in real ~/.codex rollouts). The
        # archived Paxel normalizer misses this shape entirely — deliberate
        # divergence: without it every modern Codex session scores 0 edits.
        path = write_session([
            {"timestamp": "t", "type": "session_meta",
             "payload": {"id": "s2", "cwd": "/p", "originator": "codex_cli_rs"}},
            {"timestamp": "t", "type": "response_item",
             "payload": {"type": "custom_tool_call", "name": "apply_patch",
                         "status": "completed", "call_id": "c1",
                         "input": ("*** Begin Patch\n"
                                   "*** Add File: docs/logo.svg\n+<svg/>\n"
                                   "*** Update File: src/app.py\n@@\n-a\n+b\n"
                                   "*** End Patch")}},
        ])
        try:
            out = condense.condense_session(path)
        finally:
            os.remove(path)
        self.assertIn("Write(file_path=docs/logo.svg", out["condensed_text"])
        self.assertIn("Edit(file_path=src/app.py", out["condensed_text"])
        self.assertEqual(out["facts"]["code_edits"], 2)

    def test_old_format_raw_entries_condense(self):
        path = write_session([
            {"id": "old-1", "cwd": "/old", "originator": "codex_cli_rs"},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hello from old codex"}]},
            {"type": "message", "role": "assistant",
             "content": [{"type": "text", "text": "hi back"}]},
        ])
        try:
            out = condense.condense_session(path)
        finally:
            os.remove(path)
        self.assertEqual(out["agent_type"], "codex_cli")
        self.assertIn("USER: hello from old codex", out["condensed_text"])
        self.assertIn("ASSISTANT: hi back", out["condensed_text"])

    def test_claude_sessions_report_claude_agent_type(self):
        path = write_session([
            {"type": "user", "message": {"role": "user", "content": "hi"}}])
        try:
            out = condense.condense_session(path)
        finally:
            os.remove(path)
        self.assertEqual(out["agent_type"], "claude_code")


class AggregateTests(unittest.TestCase):
    def test_band_cut_thresholds_are_verbatim(self):
        cases = [(0, "WEAK"), (3.9, "WEAK"), (4, "LIMITED"), (5.9, "LIMITED"),
                 (6, "STRONG"), (7.9, "STRONG"), (8, "ELITE"), (8.9, "ELITE"),
                 (9, "EXEMPLAR"), (10.0, "EXEMPLAR")]
        for score, band in cases:
            self.assertEqual(aggregate.band_for_score(score), band, msg=f"score={score}")

    def test_confidence_weighted_mean(self):
        eps = [{"scores": {"steering": 8.0}, "confidence": 1.0},
               {"scores": {"steering": 4.0}, "confidence": 1.0}]
        per_axis, overall = aggregate.rollup(eps)
        self.assertEqual(per_axis["steering"], 6.0)
        self.assertEqual(overall, 6.0)

    def test_confidence_zero_carries_no_weight(self):
        # Regression: confidence 0 used to be coerced to 0.8 by `or`.
        eps = [{"scores": {"steering": 8.0}, "confidence": 1.0},
               {"scores": {"steering": 2.0}, "confidence": 0}]
        per_axis, _ = aggregate.rollup(eps)
        self.assertEqual(per_axis["steering"], 8.0)  # the conf-0 episode is ignored

    def test_missing_axis_is_omitted_not_defaulted(self):
        eps = [{"scores": {"steering": 7.0}, "confidence": 0.8}]
        per_axis, overall = aggregate.rollup(eps)
        self.assertEqual(set(per_axis), {"steering"})
        self.assertEqual(overall, 7.0)

    def test_empty_input_yields_none(self):
        per_axis, overall = aggregate.rollup([])
        self.assertEqual(per_axis, {})
        self.assertIsNone(overall)


class CwdCaptureTest(unittest.TestCase):
    def test_claude_code_cwd_captured_from_entry_level_key(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "s1.jsonl")
            entries = [
                {"type": "user", "cwd": "/Volumes/Code/myproj",
                 "timestamp": "2026-06-01T10:00:00Z",
                 "message": {"role": "user", "content": "hello there world"}},
                {"type": "assistant", "cwd": "/Volumes/Code/myproj",
                 "timestamp": "2026-06-01T10:00:05Z",
                 "message": {"role": "assistant",
                             "content": [{"type": "text", "text": "hi"}]}},
            ]
            with open(p, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e) + "\n")
            rec = condense.condense_session(p)
            self.assertEqual(rec["cwd"], "/Volumes/Code/myproj")

    def test_claude_code_cwd_none_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "s2.jsonl")
            with open(p, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "user", "message": {
                    "role": "user", "content": "hello"}}) + "\n")
            rec = condense.condense_session(p)
            self.assertIsNotNone(rec, "session should parse even when too_short")
            self.assertIsNone(rec["cwd"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

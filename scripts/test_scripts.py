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
        self.assertIn("[REDACTED_AWS_KEY]", condense.scrub("AKIA" + "B" * 16))
        self.assertIn("[REDACTED_ENV_SECRET]", condense.scrub("API_TOKEN=hunter2"))
        self.assertEqual(condense.scrub(None), "")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""
Tests for decisions.py (decision-exchange extraction, finalization, render).

Stdlib only (unittest). Run from anywhere:

    python3 scripts/test_decisions.py
"""
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decisions  # noqa: E402


def ev(etype, text="", **extra):
    return {"type": etype, "text": text, **extra}


def twenty_words(prefix=""):
    words = ["w%d" % i for i in range(20)]
    return (prefix + " " + " ".join(words)).strip()


def write_json(obj, suffix=".json"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return path


def write_sessions(sessions):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for session_id, events in sessions:
            f.write(json.dumps({"session_id": session_id, "events": events}) + "\n")
    return path


class RubyHelperTests(unittest.TestCase):
    def test_ruby_truncate_appends_omission_within_limit(self):
        self.assertEqual(decisions.ruby_truncate("a" * 150, 100), "a" * 97 + "...")
        self.assertEqual(decisions.ruby_truncate("short", 100), "short")
        self.assertEqual(decisions.ruby_truncate("a" * 100, 100), "a" * 100)

    def test_ruby_split_ws_matches_ruby_semantics(self):
        self.assertEqual(decisions.ruby_split_ws("a b  c"), ["a", "b", "c"])
        self.assertEqual(decisions.ruby_split_ws("  a b"), ["", "a", "b"])  # leading kept
        self.assertEqual(decisions.ruby_split_ws("a b "), ["a", "b"])       # trailing dropped
        self.assertEqual(decisions.ruby_split_ws(""), [])


class Pass1PairingTests(unittest.TestCase):
    def test_pairs_within_response_window(self):
        events = [
            ev("agent_proposal", "Option 1 or option 2?", proposal_type="options"),
            ev("test_run"), ev("test_run"),
            ev("user_directive", "go with option 2"),  # offset 3 = window edge
        ]
        cands = decisions.extract_candidates("s1", events)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c["source"], "paired")
        self.assertEqual(c["event_index"], 0)
        self.assertEqual(c["response_index"], 3)
        self.assertEqual(c["agent_text"], "Option 1 or option 2?")
        self.assertEqual(c["user_text"], "go with option 2")
        self.assertEqual(c["proposal_type"], "options")

    def test_directive_beyond_window_is_not_paired(self):
        events = [
            ev("agent_proposal", "A or B?"),
            ev("test_run"), ev("test_run"), ev("test_run"),
            ev("user_directive", "short reply"),  # offset 4 > RESPONSE_WINDOW
        ]
        cands = decisions.extract_candidates("s1", events)
        self.assertEqual(cands, [])  # directive too short for pass 2 as well

    def test_pairing_blocked_by_intervening_proposal(self):
        events = [
            ev("agent_proposal", "First proposal"),
            ev("agent_proposal", "Second proposal"),
            ev("user_directive", "do the second"),
        ]
        cands = decisions.extract_candidates("s1", events)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["event_index"], 1)  # only the second pairs
        self.assertEqual(cands[0]["agent_text"], "Second proposal")


class Pass2ProactiveTests(unittest.TestCase):
    def test_unpaired_directive_with_preceding_thinking_context(self):
        events = [
            ev("agent_thinking", "earlier context"),
            ev("agent_thinking", "nearest context"),
            ev("user_directive", twenty_words()),
            ev("agent_thinking", "following context"),
        ]
        cands = decisions.extract_candidates("s1", events)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c["source"], "unpaired")
        self.assertEqual(c["agent_text"], "nearest context")  # preceding preferred
        self.assertEqual(c["event_index"], 2)
        self.assertEqual(c["response_index"], 2)

    def test_following_thinking_used_when_no_preceding(self):
        events = [
            ev("user_directive", twenty_words()),
            ev("agent_thinking", "after context"),
        ]
        cands = decisions.extract_candidates("s1", events)
        self.assertEqual(cands[0]["agent_text"], "after context")

    def test_noise_filters(self):
        events = [
            ev("user_directive", "too few words"),
            ev("user_directive", twenty_words("This session is being continued")),
            ev("user_directive", twenty_words("Implement the following plan")),
        ]
        self.assertEqual(decisions.extract_candidates("s1", events), [])

    def test_paired_directive_not_recollected_in_pass2(self):
        events = [
            ev("agent_proposal", "A or B?"),
            ev("user_directive", twenty_words()),
        ]
        cands = decisions.extract_candidates("s1", events)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["source"], "paired")


class RedactionTests(unittest.TestCase):
    def test_fenced_code_with_language(self):
        text = "before\n```python\nx = 1\ny = 2\n```\nafter"
        self.assertEqual(decisions.regex_redact(text),
                         "before\n[python code, ~2 lines]\nafter")

    def test_fenced_code_without_language(self):
        self.assertEqual(decisions.regex_redact("```\nx = 1\n```"),
                         "[code block, ~1 lines]")

    def test_identifiers(self):
        self.assertEqual(
            decisions.regex_redact("use `UserAccount` and `foo_bar` here"),
            "use [identifier] and [identifier] here")
        # long contiguous inline code without uppercase/underscore
        self.assertEqual(decisions.regex_redact("see `some.long.dot.chain` now"),
                         "see [identifier] now")
        # plain prose words in backticks survive
        self.assertEqual(decisions.regex_redact("set it to `nil` please"),
                         "set it to `nil` please")

    def test_file_paths(self):
        self.assertEqual(decisions.regex_redact("edit app/models/user.rb today"),
                         "edit [path] today")
        self.assertEqual(decisions.regex_redact("look at /usr/local/bin/thing now"),
                         "look at [path] now")

    def test_truncates_to_2000_chars(self):
        out = decisions.regex_redact("z" * 3000)
        self.assertEqual(len(out), 2000)
        self.assertTrue(out.endswith("..."))

    def test_blank_returns_empty(self):
        self.assertEqual(decisions.regex_redact(None), "")
        self.assertEqual(decisions.regex_redact("   "), "")


class PromptGoldenTests(unittest.TestCase):
    def test_build_prompt_format_verbatim(self):
        batch = [
            {"agent_text": "Pick A or B", "user_text": "B please"},
            {"agent_text": "Next question", "user_text": "skip it"},
        ]
        expected = (
            "Classify these 2 exchanges:\n\n"
            "Exchange 0:\nAgent: Pick A or B\nDeveloper: B please\n"
            "\n"
            "Exchange 1:\nAgent: Next question\nDeveloper: skip it\n"
        )
        self.assertEqual(decisions.build_prompt(batch), expected)

    def test_prompt_truncates_each_text_to_2000(self):
        batch = [{"agent_text": "a" * 3000, "user_text": "u"}]
        prompt = decisions.build_prompt(batch)
        self.assertIn("Agent: " + "a" * 1997 + "...", prompt)


class FinalizeTests(unittest.TestCase):
    def _candidate(self, **over):
        cand = {"candidate_id": 0, "session_id": "s1", "source": "paired",
                "agent_text": "Pick a path", "user_text": "users want onboarding",
                "proposal_type": "options", "option_count": 2,
                "event_index": 0, "response_index": 1}
        cand.update(over)
        return cand

    def _classification(self, **over):
        item = {"index": 0, "is_decision": True, "decision_type": "product_insight",
                "confidence": "high", "narrative": "n", "law_key": "iron-rule"}
        item.update(over)
        return decisions.normalize_classification(item, ["iron-rule"])

    def test_significance_and_domain_mapping(self):
        for dtype, expected in (("strategic_redirect", "strategic"),
                                ("product_insight", "strategic"),
                                ("technical_catch", "moderate"),
                                ("option_selection", "tactical"),
                                ("something_else", "tactical")):
            out = decisions.create_classified_decisions(
                [self._candidate()], [self._classification(decision_type=dtype)], [])
            self.assertEqual(out[0]["significance"], expected, dtype)
        out = decisions.create_classified_decisions(
            [self._candidate()], [self._classification()], [])
        d = out[0]
        self.assertEqual(d["domain"], "product")  # "users"/"onboarding"
        self.assertEqual(d["proposal_type"], "options")
        self.assertEqual(d["law_key"], "iron-rule")
        self.assertEqual(d["response_word_count"], 3)
        self.assertEqual(d["reversibility"], "unknown")

    def test_invalid_law_key_nulled(self):
        item = decisions.normalize_classification(
            {"index": 0, "is_decision": True, "law_key": "made-up-law"}, ["iron-rule"])
        self.assertIsNone(item["law_key"])

    def test_not_a_decision_skipped(self):
        out = decisions.create_classified_decisions(
            [self._candidate()], [self._classification(is_decision=False)], [])
        self.assertEqual(out, [])

    def test_unpaired_defaults_proactive_insight_and_recognized(self):
        cand = self._candidate(source="unpaired", proposal_type=None, option_count=None)
        out = decisions.create_classified_decisions(
            [cand], [self._classification()], [])
        self.assertEqual(out[0]["proposal_type"], "proactive_insight")
        self.assertTrue(out[0]["agent_recognized"])

    def test_regex_fallback_counter_proposal(self):
        cand = self._candidate(
            agent_text="We could use option A caching or option B indexing",
            user_text="Neither, drop the cache entirely and precompute results upfront")
        out = decisions.create_regex_decisions([cand], [])
        self.assertEqual(out[0]["proposal_type"], "counter_proposal")
        self.assertEqual(out[0]["significance"], "strategic")

    def test_regex_fallback_option_reference_is_not_counter(self):
        cand = self._candidate(
            agent_text="We could use option A caching or option B indexing",
            user_text="go with option B")
        out = decisions.create_regex_decisions([cand], [])
        self.assertEqual(out[0]["proposal_type"], "options")

    def test_finalize_end_to_end(self):
        events = [
            ev("agent_proposal", "Here are two: option 1 queue, option 2 cron",
               proposal_type="options", option_count=2),
            ev("user_directive", "go with option 2, cron keeps it simpler"),
            ev("test_run", passed=3, failed=0),
            ev("git_commit"),
        ]
        sessions_path = write_sessions([("s1", events)])
        cand_path = write_json(None)
        batch_path = write_json(None)
        decisions.cmd_extract(Namespace(sessions=sessions_path, out=cand_path,
                                        batches=batch_path))
        with open(batch_path) as f:
            batches = json.load(f)
        self.assertEqual(len(batches), 1)
        self.assertTrue(batches[0]["input_text"].startswith("Classify these 1 exchanges:"))

        cls_path = write_json([[{"index": 0, "is_decision": True,
                                 "decision_type": "option_selection",
                                 "confidence": "high", "narrative": "Chose cron",
                                 "law_key": "iron-rule"}]])
        out_path = write_json(None)
        decisions.cmd_finalize(Namespace(candidates=cand_path, classifications=cls_path,
                                         sessions=sessions_path, out=out_path))
        with open(out_path) as f:
            final = json.load(f)
        self.assertEqual(len(final), 1)
        d = final[0]
        self.assertEqual(d["significance"], "tactical")
        self.assertEqual(d["decision_type"], "option_selection")
        self.assertEqual(d["law_key"], "iron-rule")
        self.assertEqual(d["outcome"]["signal"], "positive")
        self.assertEqual(d["outcome"]["confidence"], 0.7)  # test_run + commit
        self.assertEqual(d["outcome"]["evidence"],
                         "1 test run(s): 3 passed, 0 failed. 1 commit(s) made")
        self.assertIn("redacted_proposal_text", d)
        for p in (sessions_path, cand_path, batch_path, cls_path, out_path):
            os.unlink(p)


class OutcomeTests(unittest.TestCase):
    def _decision(self, idx=0):
        return {"event_index": idx, "session_id": "s1"}

    def test_positive_last_test_green(self):
        events = [ev("agent_proposal"), ev("test_run", passed=0, failed=2),
                  ev("test_run", passed=5, failed=0)]
        out = decisions.analyze_outcome(self._decision(), events)
        self.assertEqual(out["signal"], "positive")
        self.assertEqual(out["confidence"], 0.7)  # 2 test runs

    def test_negative_on_reversal(self):
        events = [ev("agent_proposal"), ev("user_directive", "actually revert that")]
        out = decisions.analyze_outcome(self._decision(), events)
        self.assertEqual(out["signal"], "negative")
        self.assertEqual(out["confidence"], 0.5)
        self.assertEqual(out["evidence"], "User reversed direction")

    def test_negative_last_test_red(self):
        events = [ev("agent_proposal"), ev("test_run", passed=1, failed=2)]
        out = decisions.analyze_outcome(self._decision(), events)
        self.assertEqual(out["signal"], "negative")

    def test_commit_without_failures_positive(self):
        events = [ev("agent_proposal"), ev("git_commit")]
        out = decisions.analyze_outcome(self._decision(), events)
        self.assertEqual(out["signal"], "positive")

    def test_errors_only_mixed(self):
        events = [ev("agent_proposal"), ev("error_encountered")]
        out = decisions.analyze_outcome(self._decision(), events)
        self.assertEqual(out["signal"], "mixed")

    def test_neutral_no_signal(self):
        events = [ev("agent_proposal"), ev("agent_thinking", "hmm")]
        out = decisions.analyze_outcome(self._decision(), events)
        self.assertEqual(out["signal"], "neutral")
        self.assertEqual(out["confidence"], 0.3)
        self.assertEqual(out["evidence"], "No signal events after decision")

    def test_scan_stops_at_next_proposal(self):
        events = [ev("agent_proposal"), ev("git_commit"),
                  ev("agent_proposal"), ev("test_run", passed=0, failed=9)]
        out = decisions.analyze_outcome(self._decision(), events)
        self.assertEqual(out["signal"], "positive")  # red test is past the boundary

    def test_no_events_after_decision(self):
        self.assertIsNone(decisions.analyze_outcome(self._decision(), [ev("agent_proposal")]))


class RenderTests(unittest.TestCase):
    def _decisions(self):
        return [
            {"session_id": "s1", "domain": "architecture", "significance": "strategic",
             "proposal_type": "counter_proposal", "agent_recognized": True,
             "proposal_text": "Choose A or B", "response_text": "Neither, do C",
             "event_index": 4, "outcome": {"signal": "positive", "confidence": 0.7,
                                           "evidence": "1 commit(s) made"}},
            {"session_id": "s1", "domain": "general", "significance": "tactical",
             "proposal_type": "options", "agent_recognized": False,
             "proposal_text": "x", "response_text": "y", "event_index": 1,
             "outcome": None},
            {"session_id": "s2", "domain": "product", "significance": "moderate",
             "proposal_type": "proactive_insight", "agent_recognized": True,
             "proposal_text": "p" * 150, "response_text": "resp",
             "event_index": 2, "outcome": None},
        ]

    def test_render_golden_string(self):
        dec_path = write_json(self._decisions())
        fd, out_path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        decisions.cmd_render(Namespace(decisions=dec_path, session_ids="s1,s2",
                                       out=out_path))
        with open(out_path) as f:
            got = f.read()
        expected = (
            "2 decision exchange(s) detected. 1 counter-proposal(s)."
            " 1 proactive insight(s). 2 agent-recognized.\n"
            "[product/moderate [PROACTIVE-INSIGHT, AGENT-RECOGNIZED]] Agent: "
            + "p" * 97 + "... | Dev: resp\n"
            "[architecture/strategic [COUNTER-PROPOSAL, AGENT-RECOGNIZED]] "
            "Agent: Choose A or B | Dev: Neither, do C -> positive\n"
        )
        self.assertEqual(got, expected)
        os.unlink(dec_path)
        os.unlink(out_path)

    def test_render_filters_sessions_and_tactical(self):
        dec_path = write_json(self._decisions())
        fd, out_path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        decisions.cmd_render(Namespace(decisions=dec_path, session_ids="s1",
                                       out=out_path))
        with open(out_path) as f:
            got = f.read()
        self.assertTrue(got.startswith("1 decision exchange(s) detected."))
        self.assertNotIn("PROACTIVE-INSIGHT", got)  # s2 excluded
        os.unlink(dec_path)
        os.unlink(out_path)

    def test_render_empty_prints_nothing(self):
        dec_path = write_json([])
        fd, out_path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        decisions.cmd_render(Namespace(decisions=dec_path, session_ids="s1",
                                       out=out_path))
        with open(out_path) as f:
            self.assertEqual(f.read(), "")
        os.unlink(dec_path)
        os.unlink(out_path)

    def test_summarize_first_ten_lines_but_counts_all(self):
        ds = [{"session_id": "s1", "domain": "general", "significance": "moderate",
               "proposal_type": "question", "agent_recognized": False,
               "proposal_text": "p%d" % i, "response_text": "r",
               "event_index": i, "outcome": None} for i in range(12)]
        out = decisions.summarize_decisions(ds)
        lines = out.split("\n")
        self.assertEqual(lines[0], "12 decision exchange(s) detected.")
        self.assertEqual(len(lines), 11)  # header + 10


class BatchCollisionTests(unittest.TestCase):
    def test_batch_index_collision_last_batch_wins(self):
        """Faithful Ruby quirk: per-batch indexes collide across batches of one
        session; index_by keeps the LAST item, so >20 candidates per session
        get classified by the final batch's (0-based) indexes."""
        events = [ev("agent_proposal", "P%d?" % i) if i % 2 == 0
                  else ev("user_directive", "do %d" % i) for i in range(42)]
        cands = decisions.extract_candidates("s1", events)
        self.assertEqual(len(cands), 21)
        for i, c in enumerate(cands):
            c["candidate_id"] = i
        batches = decisions.build_batches(cands)
        self.assertEqual([len(b["candidate_ids"]) for b in batches], [20, 1])
        # batch 0 says candidate 0 is NOT a decision; batch 1 (index 0 again)
        # says it IS — the later batch overwrites the earlier in the map.
        classifications = [
            decisions.normalize_classification(
                {"index": 0, "is_decision": False}, []),
            decisions.normalize_classification(
                {"index": 0, "is_decision": True, "decision_type": "technical_catch",
                 "confidence": "high", "narrative": "n", "law_key": None}, []),
        ]
        out = decisions.create_classified_decisions(cands, classifications, events)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["event_index"], 0)  # candidate 0 became a decision


if __name__ == "__main__":
    unittest.main(verbosity=2)

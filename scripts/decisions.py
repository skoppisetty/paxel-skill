#!/usr/bin/env python3
"""
decisions.py — deterministic halves of Paxel's decision-exchange pipeline.

Faithful port of the client-side decision path (DecisionExchangeExtractor +
DecisionClassifier's deterministic parts + DecisionTextRedactor +
InSessionAnalyzer + EpisodeSummarizer#summarize_decisions). The LLM
classification itself is dispatched by the orchestrating Claude session using
prompts/decision_classifier.md — this script only extracts candidates, builds
the batch prompts, finalizes classified results, traces in-session outcomes,
and renders the episode decision summary.

Subcommands (run in order):

  extract   --sessions sessions.jsonl --out candidates.json --batches batches.json
      Pass 1: pair agent_proposal → user_directive within RESPONSE_WINDOW=3
      events (an intervening agent_proposal kills the pairing). Pass 2: unpaired
      user_directives ≥ MIN_PROACTIVE_WORDS, minus session-continuation and
      plan-paste noise, with nearby agent_thinking (±5, preceding preferred) as
      context. batches.json packs candidates in per-session batches of 20 with
      the verbatim DecisionClassifier#build_prompt input_text.

  finalize  --candidates candidates.json --classifications cls.json
            --sessions sessions.jsonl --out decisions.json
      cls.json = list of per-batch JSON arrays (aligned with batches.json by
      position), each item {index, is_decision, decision_type, confidence,
      narrative, law_key}. Ports create_classified_decisions (law_key validated
      against reference/decision_catalog.json, infer_significance, regex
      domain/reversibility on the combined text) plus the per-SESSION regex
      fallback (create_decision / create_proactive_insight) when a session got
      zero classifications back. Then ports InSessionAnalyzer: scans events
      after each decision until the next agent_proposal, counts test
      runs/errors/commits/reversals and attaches {signal, confidence, evidence}.
      Also stores redacted_proposal_text/redacted_response_text via the
      DecisionTextRedactor port — mirroring the client pipeline's "Redacting
      code before upload" step (redaction is NOT applied to candidate texts or
      classifier prompts; the Ruby classifier and the local scoring path both
      see raw text).

  render    --decisions decisions.json --session-ids id1,id2 [--out -]
      EpisodeSummarizer#summarize_decisions, verbatim: drop tactical decisions,
      order by event_index, first 10 lines + header counts. NOTE: exchange
      CHAINS ARE NOT PORTED (ExchangeChainDetector needs embeddings) — every
      decision is treated as not-in-chain, so chain_count is always 0 and the
      "[chain N]" suffix never appears. Prints nothing when there are no
      non-tactical decisions (Ruby returns nil).

Faithfully ported quirks (deliberate, do not "fix"):
  - DecisionClassifier#build_prompt numbers exchanges PER BATCH (0..19) and
    create_classified_decisions keys classifications by SESSION-local candidate
    index. With >20 candidates in one session, batch indexes collide and the
    later batch's items overwrite the earlier ones in the map (Ruby
    `index_by`). Reproduced as-is.
  - Counter-proposal detection exists ONLY on the regex fallback path
    (create_decision); the LLM path never assigns proposal_type
    "counter_proposal".
  - Domain/significance/reversibility regexes run on the RAW combined
    candidate text, before any truncation or redaction.
"""
import argparse
import json
import os
import re
import sys

# --- constants (verbatim from decision_exchange_extractor.rb /
#     decision_classifier.rb / transcript_patterns.rb) ---
RESPONSE_WINDOW = 3
THINKING_SEARCH_WINDOW = 5
MIN_NOVEL_WORDS = 3
MAX_EXCHANGE_TEXT_LENGTH = 10_000   # decision proposal/response cap
MAX_TEXT_LENGTH = 2_000             # per-text cap inside the batch prompt
BATCH_SIZE = 20
MIN_PROACTIVE_WORDS = 20
SESSION_CONTINUATION_PREFIX = "This session is being continued"
PLAN_PASTE_PREFIX = "Implement the following plan"

CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "reference", "decision_catalog.json")

# --- Ruby compatibility helpers ---

def ruby_truncate(text, length, omission="..."):
    """ActiveSupport String#truncate: omission counts WITHIN the limit."""
    text = "" if text is None else str(text)
    if len(text) <= length:
        return text
    return text[: max(length - len(omission), 0)] + omission


def ruby_split_ws(text):
    """Ruby String#split(/\\s+/): keeps a leading "" element, drops trailing."""
    parts = re.split(r"\s+", text or "")
    while parts and parts[-1] == "":
        parts.pop()
    return parts


def ruby_to_i(value):
    """Ruby #to_i: nil → 0, leading-integer parse, garbage → 0."""
    if value is None or value is False:
        return 0
    if value is True:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    m = re.match(r"\s*([-+]?\d+)", str(value))
    return int(m.group(1)) if m else 0


def blank(text):
    return text is None or str(text).strip() == ""


# --- transcript patterns (verbatim from concerns/transcript_patterns.rb;
#     re.ASCII matches Ruby's ASCII-only \w / \b / \s semantics) ---
REVERSAL_INDICATORS = re.compile(
    r"\b(actually|no|wait|scratch that|undo|revert|go back|different approach"
    r"|instead|never mind)\b", re.IGNORECASE | re.ASCII)

OPTION_REFERENCE_PATTERNS = [re.compile(p, re.IGNORECASE | re.ASCII) for p in (
    r"\b(?:option|approach|alternative|choice)\s*(?:[A-C]|\d)\b",
    r"\bgo\s+with\s+(?:option|approach|#?\s*\d|[A-C])\b",
    r"\b(?:the\s+)?(?:first|second|third|last)\s+(?:option|approach|one)\b",
    r"\blet'?s?\s+(?:do|go|try)\s+(?:option|approach|#?\s*\d|[A-C])\b",
    r"\b(?:prefer|like|pick|choose)\s+(?:option|approach)?\s*(?:\d|[A-C])\b",
)]

AMPLIFICATION_PATTERNS = [re.compile(p, re.IGNORECASE | re.ASCII) for p in (
    r"\b(?:major|key|great|important|critical)\s+(?:insight|point|observation|suggestion)\b",
    r"\bfundamentally\s+(?:different|change|better|rethink)\b",
    r"\b(?:game\s+changer|breakthrough|paradigm)\b",
    r"\b(?:you'?re?\s+right|excellent\s+(?:point|suggestion|idea)|this\s+changes)\b",
    r"\b(?:this\s+is\s+(?:much\s+)?better|much\s+cleaner|significantly\s+(?:improve|better|cleaner))\b",
    r"\b(?:hadn'?t?\s+(?:considered|thought)|never\s+(?:considered|occurred))\b",
    r"\b(?:completely|entirely|totally)\s+different\s+(?:task|approach|direction|problem|framing)\b",
    r"\b(?:core|central|fundamental)\s+(?:insight|realization|observation)\b",
    r"\bfundamental\s+(?:rearchitecture|reimagining|redesign|rebuild|rewrite|rethink|shift)\b",
    r"\buser(?:'s|s)?\s+(?:core|key|main|primary|central)\s+(?:insight|point|ask|request|concern)\b",
)]

PROACTIVE_REFRAME_PATTERNS = [re.compile(p, re.IGNORECASE | re.ASCII) for p in (
    r"\b(?:instead\s+of|shift\s+to|the\s+real\s+(?:issue|problem|question)\s+is)\b",
    r"\b(?:rethink|fundamentally|paradigm|categorically\s+different)\b",
    r"\b(?:the\s+whole\s+approach|what\s+if\s+we|completely\s+different)\b",
    r"\b(?:we\s+should\s+(?:actually|really)|the\s+better\s+way|forget\s+(?:that|this))\b",
)]

# --- extractor patterns (verbatim from decision_exchange_extractor.rb) ---
DOMAIN_PATTERNS = [(d, re.compile(p, re.IGNORECASE | re.ASCII)) for d, p in (
    ("architecture", r"(?:service|model|controller|pattern|layer|schema|migration"
                     r"|database|api|endpoint|module|class|concern|interface)"),
    ("debugging", r"(?:error|bug|fix|broken|failing|exception|debug|root cause"
                  r"|stack trace|crash)"),
    ("scope", r"(?:scope|feature|priority|defer|cut|mvp|ship|phase|v1|v2|later|backlog)"),
    ("quality", r"(?:test|spec|coverage|refactor|validation|security|lint|type|safety)"),
    ("product", r"(?:user|customer|ux|ui|experience|flow|onboarding|design|layout)"),
    ("tooling", r"(?:ci|cd|deploy|docker|config|dependency|build|pipeline|infra)"),
)]
ONE_WAY_PATTERNS = re.compile(
    r"(?:migration|schema|database|deploy|production|api.*contract"
    r"|public.*interface|delete.*data|breaking.*change)", re.IGNORECASE | re.ASCII)
REVERSIBLE_PATTERNS = re.compile(
    r"(?:config|env|style|format|lint|rename|variable|comment|log)",
    re.IGNORECASE | re.ASCII)
WORD_4PLUS = re.compile(r"\b\w{4,}\b", re.ASCII)
RATIONALE_PATTERNS = re.compile(r"because|reason|since|given that", re.IGNORECASE | re.ASCII)

# --- DecisionTextRedactor (verbatim port; Ruby /m = DOTALL, Ruby ^/$ are
#     always line anchors so Python needs re.MULTILINE where ^/$ appear) ---
FENCED_CODE = re.compile(r"```[\w]*\n[\s\S]*?```", re.ASCII)
INDENTED_CODE = re.compile(
    r"^[ \t]{2,}(?:def |class |module |end$|if |unless |do$|do |require "
    r"|include |extend |attr_|private|protected|public|return |raise |rescue "
    r"|begin$|ensure$|yield|puts |print |const |let |var |function |import "
    r"|export |from |=>|->|\{|\}|</|/>)", re.MULTILINE)
LONG_INLINE_CODE = re.compile(
    r"`(?:[^`\s]{10,}|(?=[^`]*[A-Z_:.#/(){}\[\]<>=+])[^`]{10,})`", re.ASCII)
SHORT_IDENTIFIER = re.compile(r"`(?=[\w:.#]*[A-Z_])[\w:.#]+`", re.ASCII)
FILE_PATHS = re.compile(
    r"(?:^|[\s(\[{,:;])(?:/[\w\-./]+|[\w\-]+(?:/[\w\-.]+)*"
    r"\.(?:rb|rake|ru|gemspec|py|js|ts|jsx|tsx|go|rs|java|cpp|c|h|css|html|erb"
    r"|yml|yaml|json|toml|sql|sh|bash))\b", re.MULTILINE | re.ASCII)
CODE_COLLAPSE = re.compile(r"(\[\w*\s*code[^\]]*\]\s*){2,}", re.ASCII)
FENCE_LANG = re.compile(r"```(\w+)", re.ASCII)


def regex_redact(text):
    """DecisionTextRedactor.regex_redact: 6 passes in order + 2000-char cap."""
    if blank(text):
        return ""

    def fenced_repl(m):
        lang = FENCE_LANG.search(m.group(0))
        lines = m.group(0).count("\n") - 1
        if lang:
            return "[%s code, ~%d lines]" % (lang.group(1), lines)
        return "[code block, ~%d lines]" % lines

    result = FENCED_CODE.sub(fenced_repl, text)
    result = INDENTED_CODE.sub("[code line]", result)
    # SHORT_IDENTIFIER must run BEFORE LONG_INLINE_CODE (see Ruby comment:
    # LONG would otherwise eat the prose between two backticked identifiers).
    result = SHORT_IDENTIFIER.sub("[identifier]", result)
    result = LONG_INLINE_CODE.sub("[identifier]", result)
    result = FILE_PATHS.sub(" [path]", result)
    result = CODE_COLLAPSE.sub("[code] ", result)
    return ruby_truncate(result.strip(), 2000)


# --- catalog ---

def load_catalog_keys(path=CATALOG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return [law["key"] for law in json.load(f)]


# --- sessions.jsonl IO ---

def load_sessions(path):
    """Returns ordered list of (session_id, events)."""
    sessions = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            sessions.append((obj.get("session_id"), obj.get("events") or []))
    return sessions


# --- extraction (DecisionExchangeExtractor#extract_from_session) ---

def find_response(events, proposal_idx):
    search_end = min(proposal_idx + RESPONSE_WINDOW + 1, len(events))
    for j in range(proposal_idx + 1, search_end):
        event = events[j]
        if event.get("type") == "user_directive":
            return {"event": event, "index": j}
        # Stop if we hit another agent_proposal (unanswered proposal)
        if event.get("type") == "agent_proposal":
            return None
    return None


def find_nearby_thinking(events, user_idx):
    search_start = max(user_idx - THINKING_SEARCH_WINDOW, 0)
    search_end = min(user_idx + THINKING_SEARCH_WINDOW, len(events) - 1)
    # Prefer preceding thinking (agent's current context)
    for i in range(user_idx - 1, search_start - 1, -1):
        if events[i].get("type") == "agent_thinking":
            return str(events[i].get("text") or "")
    for i in range(user_idx + 1, search_end + 1):
        if events[i].get("type") == "agent_thinking":
            return str(events[i].get("text") or "")
    return None


def extract_candidates(session_id, events):
    """Pass 1 (paired) + Pass 2 (unpaired) candidates, in Ruby's order:
    all paired candidates first, then unpaired ones."""
    candidates = []
    if not events:
        return candidates

    # Pass 1: pair agent_proposal → user_directive candidates
    paired_indices = set()
    for i, event in enumerate(events):
        if event.get("type") != "agent_proposal":
            continue
        response = find_response(events, i)
        if response:
            paired_indices.add(response["index"])
            candidates.append({
                "session_id": session_id,
                "source": "paired",
                "agent_text": str(event.get("text") or ""),
                "user_text": str(response["event"].get("text") or ""),
                "proposal_type": event.get("proposal_type"),
                "option_count": event.get("option_count"),
                "event_index": i,
                "response_index": response["index"],
            })

    # Pass 2: unpaired user_directives as proactive insight candidates
    for idx, event in enumerate(events):
        if event.get("type") != "user_directive" or idx in paired_indices:
            continue
        text = str(event.get("text") or "")
        if len(ruby_split_ws(text)) < MIN_PROACTIVE_WORDS:
            continue
        if text.startswith(SESSION_CONTINUATION_PREFIX):
            continue
        if text.startswith(PLAN_PASTE_PREFIX):
            continue
        context_text = find_nearby_thinking(events, idx)
        candidates.append({
            "session_id": session_id,
            "source": "unpaired",
            "agent_text": context_text or "",
            "user_text": text,
            "proposal_type": None,   # candidate[:proposal_event] carries no type
            "option_count": None,
            "event_index": idx,
            "response_index": idx,
        })

    return candidates


def build_prompt(batch):
    """DecisionClassifier#build_prompt, byte-for-byte (per-batch indexes)."""
    lines = []
    for idx, candidate in enumerate(batch):
        agent = ruby_truncate(str(candidate["agent_text"]), MAX_TEXT_LENGTH)
        user = ruby_truncate(str(candidate["user_text"]), MAX_TEXT_LENGTH)
        lines.append("Exchange %d:\nAgent: %s\nDeveloper: %s\n" % (idx, agent, user))
    return "Classify these %d exchanges:\n\n%s" % (len(batch), "\n".join(lines))


def build_batches(candidates):
    """Per-session slices of BATCH_SIZE, mirroring Ruby's per-session
    classifier.classify(candidates).each_slice(BATCH_SIZE)."""
    batches = []
    by_session = []
    for cand in candidates:
        if not by_session or by_session[-1][0] != cand["session_id"]:
            by_session.append((cand["session_id"], []))
        by_session[-1][1].append(cand)
    for session_id, cands in by_session:
        for start in range(0, len(cands), BATCH_SIZE):
            batch = cands[start:start + BATCH_SIZE]
            batches.append({
                "batch_id": len(batches),
                "session_id": session_id,
                "candidate_ids": [c["candidate_id"] for c in batch],
                "input_text": build_prompt(batch),
            })
    return batches


# --- classification finalization (create_classified_decisions etc.) ---

def detect_counter_proposal(candidate):
    if candidate.get("proposal_type") != "options":
        return False
    response_text = candidate["user_text"]
    # User must NOT reference any proposed option
    if any(p.search(response_text) for p in OPTION_REFERENCE_PATTERNS):
        return False
    # Dual signal: novel words OR reversal indicator
    has_reversal = bool(REVERSAL_INDICATORS.search(response_text))
    proposal_words = set(WORD_4PLUS.findall(str(candidate["agent_text"]).lower()))
    response_words = WORD_4PLUS.findall(response_text.lower())
    novel_count = sum(1 for w in response_words if w not in proposal_words)
    return novel_count >= MIN_NOVEL_WORDS or has_reversal


def detect_agent_amplification(events, response_index):
    if not events or response_index is None:
        return False
    search_start = max(response_index - THINKING_SEARCH_WINDOW, 0)
    search_end = min(response_index + THINKING_SEARCH_WINDOW, len(events) - 1)
    for i in range(search_start, search_end + 1):
        event = events[i]
        if event.get("type") != "agent_thinking":
            continue
        text = str(event.get("text") or "")
        if any(p.search(text) for p in AMPLIFICATION_PATTERNS):
            return True
    return False


def escalate_significance(current):
    return {"tactical": "moderate", "moderate": "strategic"}.get(current, current)


def classify_domain(text):
    for domain, pattern in DOMAIN_PATTERNS:
        if pattern.search(text):
            return domain
    return "general"


def classify_significance(response_text, combined_text):
    word_count = len(ruby_split_ws(response_text))
    domain = classify_domain(combined_text)
    if word_count > 30 and (domain == "architecture"
                            or ONE_WAY_PATTERNS.search(combined_text)
                            or RATIONALE_PATTERNS.search(response_text)):
        return "strategic"
    if word_count > 15 or domain not in ("debugging", "tooling"):
        return "moderate"
    return "tactical"


def classify_reversibility(text):
    if ONE_WAY_PATTERNS.search(text):
        return "one_way"
    if REVERSIBLE_PATTERNS.search(text):
        return "reversible"
    return "unknown"


def infer_significance(classification):
    return {
        "strategic_redirect": "strategic",
        "product_insight": "strategic",
        "technical_catch": "moderate",
        "option_selection": "tactical",
    }.get(classification.get("decision_type"), "tactical")


def normalize_classification(item, catalog_keys):
    """DecisionClassifier#parse_response item normalization."""
    law_key = item.get("law_key")
    if law_key not in catalog_keys:
        law_key = None
    return {
        "index": ruby_to_i(item.get("index")),
        "is_decision": item.get("is_decision") is True,
        "decision_type": item.get("decision_type"),
        "confidence": item.get("confidence"),
        "narrative": item.get("narrative"),
        "law_key": law_key,
    }


def make_decision(candidate, *, proposal_text, response_text, proposal_type,
                  combined, significance, agent_recognized, decision_type=None,
                  law_key=None, classification_confidence=None,
                  decision_narrative=None, option_count=None):
    return {
        "session_id": candidate["session_id"],
        "proposal_text": ruby_truncate(proposal_text, MAX_EXCHANGE_TEXT_LENGTH),
        "response_text": ruby_truncate(response_text, MAX_EXCHANGE_TEXT_LENGTH),
        "proposal_type": proposal_type,
        "decision_type": decision_type,
        "law_key": law_key,
        "classification_confidence": classification_confidence,
        "decision_narrative": decision_narrative,
        "option_count": option_count,
        "response_word_count": len(ruby_split_ws(response_text)),
        "domain": classify_domain(combined),
        "significance": significance,
        "reversibility": classify_reversibility(combined),
        "event_index": candidate["event_index"],
        "agent_recognized": agent_recognized,
    }


def create_classified_decisions(candidates, classifications, events):
    """LLM path: only is_decision=true become decisions. `classifications`
    keys collide across >1 batch exactly like Ruby index_by (last wins)."""
    classification_map = {}
    for c in classifications:
        classification_map[c["index"]] = c

    decisions = []
    for idx, candidate in enumerate(candidates):
        classification = classification_map.get(idx)
        if not (classification and classification["is_decision"]):
            continue
        combined = "%s %s" % (candidate["agent_text"], candidate["user_text"])
        proposal_type = candidate.get("proposal_type") or \
            ("proactive_insight" if candidate["source"] == "unpaired" else "options")
        if candidate["source"] == "paired":
            recognized = detect_agent_amplification(events, candidate["response_index"])
        else:
            recognized = True
        decisions.append(make_decision(
            candidate,
            proposal_text=candidate["agent_text"],
            response_text=candidate["user_text"],
            proposal_type=proposal_type,
            combined=combined,
            significance=infer_significance(classification),
            agent_recognized=recognized,
            decision_type=classification["decision_type"],
            law_key=classification["law_key"],
            classification_confidence=classification["confidence"],
            decision_narrative=classification["narrative"],
            option_count=candidate.get("option_count"),
        ))
    return decisions


def create_regex_decisions(candidates, events):
    """Regex fallback when a session's LLM classification came back empty."""
    decisions = []
    for candidate in candidates:
        if candidate["source"] == "paired":
            proposal_text = candidate["agent_text"]
            response_text = candidate["user_text"]
            combined = "%s %s" % (proposal_text, response_text)
            proposal_type = candidate.get("proposal_type") or "question"
            significance = classify_significance(response_text, combined)
            if detect_counter_proposal(candidate):
                proposal_type = "counter_proposal"
                significance = "strategic"
            recognized = detect_agent_amplification(events, candidate["response_index"])
            if recognized and significance != "strategic":
                significance = escalate_significance(significance)
            decisions.append(make_decision(
                candidate,
                proposal_text=proposal_text,
                response_text=response_text,
                proposal_type=proposal_type,
                combined=combined,
                significance=significance,
                agent_recognized=recognized,
                option_count=candidate.get("option_count"),
            ))
        else:
            user_text = candidate["user_text"]
            if not any(p.search(user_text) for p in PROACTIVE_REFRAME_PATTERNS):
                continue
            if not detect_agent_amplification(events, candidate["response_index"]):
                continue
            context = candidate["agent_text"]
            proposal_text = context if not blank(context) else \
                "[Proactive insight — no surrounding agent context]"
            combined = "%s %s" % (proposal_text, user_text)
            decisions.append(make_decision(
                candidate,
                proposal_text=proposal_text,
                response_text=user_text,
                proposal_type="proactive_insight",
                combined=combined,
                significance="strategic",
                agent_recognized=True,
            ))
    return decisions


# --- outcome analysis (InSessionAnalyzer, temporal_layer "immediate") ---

def analyze_outcome(decision, events):
    start_idx = decision["event_index"] + 1
    if start_idx >= len(events):
        return None

    test_runs = []
    errors = 0
    commits = 0
    reversed_flag = False

    for i in range(start_idx, len(events)):
        event = events[i]
        etype = event.get("type")
        # Stop at next agent_proposal (next decision boundary)
        if etype == "agent_proposal":
            break
        if etype == "test_run":
            test_runs.append({"passed": ruby_to_i(event.get("passed")),
                              "failed": ruby_to_i(event.get("failed"))})
        elif etype == "error_encountered":
            errors += 1
        elif etype == "git_commit":
            commits += 1
        elif etype == "user_directive":
            if REVERSAL_INDICATORS.search(str(event.get("text") or "")):
                reversed_flag = True

    return {
        "signal": determine_signal(test_runs, errors, commits, reversed_flag),
        "confidence": calculate_confidence(test_runs, errors, commits, reversed_flag),
        "evidence": build_evidence(test_runs, errors, commits, reversed_flag),
    }


def determine_signal(test_runs, errors, commits, reversed_flag):
    if reversed_flag:
        return "negative"
    has_failing_tests = any(t["failed"] > 0 for t in test_runs)
    last_test = test_runs[-1] if test_runs else None
    # If the LAST test run passes, the decision led to a good outcome
    # regardless of errors or earlier failures along the way.
    if last_test and last_test["failed"] == 0 and last_test["passed"] > 0:
        return "positive"
    # Still failing at the end = negative
    if last_test and last_test["failed"] > 0:
        return "negative"
    # No tests but committed = likely positive (developer was satisfied)
    if commits > 0 and not has_failing_tests:
        return "positive"
    # Errors without resolution signals = mixed (development in progress)
    if errors > 0:
        return "mixed"
    return "neutral"


def calculate_confidence(test_runs, errors, commits, reversed_flag):
    signal_count = (len(test_runs) + (1 if errors > 0 else 0)
                    + (1 if commits > 0 else 0) + (1 if reversed_flag else 0))
    return {0: 0.3, 1: 0.5, 2: 0.7}.get(signal_count, 0.9)


def build_evidence(test_runs, errors, commits, reversed_flag):
    parts = []
    if test_runs:
        total_passed = sum(t["passed"] for t in test_runs)
        total_failed = sum(t["failed"] for t in test_runs)
        parts.append("%d test run(s): %d passed, %d failed"
                     % (len(test_runs), total_passed, total_failed))
    if errors > 0:
        parts.append("%d error(s) encountered" % errors)
    if commits > 0:
        parts.append("%d commit(s) made" % commits)
    if reversed_flag:
        parts.append("User reversed direction")
    if not parts:
        parts.append("No signal events after decision")
    return ". ".join(parts)


# --- render (EpisodeSummarizer#summarize_decisions) ---

def summarize_decisions(decisions):
    """Verbatim port. Chains are NOT ported (no embeddings): in_chain? is
    always false → chain_count 0, chain_info ''. Returns None when empty."""
    if not decisions:
        return None

    lines = []
    for d in decisions[:10]:
        outcome = d.get("outcome")
        signal = " -> %s" % outcome["signal"] if outcome else ""
        tags = []
        if d.get("proposal_type") == "counter_proposal":
            tags.append("COUNTER-PROPOSAL")
        if d.get("proposal_type") == "proactive_insight":
            tags.append("PROACTIVE-INSIGHT")
        if d.get("agent_recognized"):
            tags.append("AGENT-RECOGNIZED")
        tag_str = " [%s]" % ", ".join(tags) if tags else ""
        chain_info = ""  # chains not ported
        lines.append("[%s/%s%s%s] Agent: %s | Dev: %s%s" % (
            d.get("domain"), d.get("significance"), tag_str, chain_info,
            ruby_truncate(str(d.get("proposal_text") or ""), 100),
            ruby_truncate(str(d.get("response_text") or ""), 100),
            signal))

    counter_count = sum(1 for d in decisions if d.get("proposal_type") == "counter_proposal")
    proactive_count = sum(1 for d in decisions if d.get("proposal_type") == "proactive_insight")
    recognized_count = sum(1 for d in decisions if d.get("agent_recognized"))
    chain_count = 0  # chains not ported

    summary = "%d decision exchange(s) detected." % len(decisions)
    if counter_count > 0:
        summary += " %d counter-proposal(s)." % counter_count
    if proactive_count > 0:
        summary += " %d proactive insight(s)." % proactive_count
    if recognized_count > 0:
        summary += " %d agent-recognized." % recognized_count
    if chain_count > 0:
        summary += " %d exchange chain(s)." % chain_count
    return "%s\n%s" % (summary, "\n".join(lines))


# --- subcommands ---

def cmd_extract(args):
    sessions = load_sessions(args.sessions)
    candidates = []
    for session_id, events in sessions:
        candidates.extend(extract_candidates(session_id, events))
    for i, cand in enumerate(candidates):
        cand["candidate_id"] = i
    batches = build_batches(candidates)
    write_json(args.out, candidates)
    write_json(args.batches, batches)
    sys.stderr.write("%d candidate(s), %d batch(es) from %d session(s)\n"
                     % (len(candidates), len(batches), len(sessions)))


def cmd_finalize(args):
    candidates = read_json(args.candidates)
    cls_batches = read_json(args.classifications)
    events_by_session = dict(load_sessions(args.sessions))
    catalog_keys = load_catalog_keys()

    # Rebuild the per-session batch packing to align cls.json (list of
    # per-batch arrays, by position) with each session's candidates.
    batches = build_batches(candidates)
    session_classifications = {}  # session_id -> normalized items (concat)
    for batch in batches:
        items = cls_batches[batch["batch_id"]] if batch["batch_id"] < len(cls_batches) else None
        if not isinstance(items, list):
            continue
        # parse_response takes only the first expected_count items
        for item in items[:len(batch["candidate_ids"])]:
            if not isinstance(item, dict):
                continue
            session_classifications.setdefault(batch["session_id"], []).append(
                normalize_classification(item, catalog_keys))

    decisions = []
    seen = []
    for cand in candidates:
        if cand["session_id"] not in seen:
            seen.append(cand["session_id"])
    for session_id in seen:
        session_candidates = [c for c in candidates if c["session_id"] == session_id]
        events = events_by_session.get(session_id) or []
        classifications = session_classifications.get(session_id, [])
        if classifications:
            decisions.extend(create_classified_decisions(
                session_candidates, classifications, events))
        else:
            # LLM classification returned empty → regex fallback (per session)
            decisions.extend(create_regex_decisions(session_candidates, events))

    for decision in decisions:
        events = events_by_session.get(decision["session_id"]) or []
        decision["outcome"] = analyze_outcome(decision, events)
        # Client pipeline's "Redacting code before upload" step: redacted_*
        # columns alongside the raw text. The render path (like Ruby's local
        # EpisodeSummarizer) reads the RAW text; redacted_* is the
        # upload-boundary form.
        decision["redacted_proposal_text"] = regex_redact(decision["proposal_text"])
        decision["redacted_response_text"] = regex_redact(decision["response_text"])

    write_json(args.out, decisions)
    sys.stderr.write("%d decision(s) from %d candidate(s)\n"
                     % (len(decisions), len(candidates)))


def cmd_render(args):
    decisions = read_json(args.decisions)
    session_ids = [s for s in args.session_ids.split(",") if s]
    selected = [d for d in decisions
                if d.get("session_id") in session_ids
                and d.get("significance") != "tactical"]
    selected.sort(key=lambda d: d.get("event_index") or 0)  # order(:event_index)
    summary = summarize_decisions(selected)
    out = (summary + "\n") if summary else ""
    if args.out and args.out != "-":
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out)
    else:
        sys.stdout.write(out)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("extract", help="pair candidates + build classifier batches")
    p.add_argument("--sessions", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--batches", required=True)
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("finalize", help="merge LLM classifications + outcomes")
    p.add_argument("--candidates", required=True)
    p.add_argument("--classifications", required=True)
    p.add_argument("--sessions", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser("render", help="episode decision summary (summarize_decisions)")
    p.add_argument("--decisions", required=True)
    p.add_argument("--session-ids", required=True)
    p.add_argument("--out", default="-")
    p.set_defaults(func=cmd_render)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

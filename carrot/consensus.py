"""Two models, made to argue, before you trust either of them.

A single small local model answering a hard question gives you one confident
paragraph and no way to tell whether it is right. That is fine for "what is a
list comprehension" and dangerous for "why does this deadlock". The models
available on a laptop are individually mediocre and *differently* mediocre —
a coding model and a reasoning model fail in different places — which is
exactly the condition under which making them disagree in public is worth more
than asking either one twice.

So: three rounds.

1. **Propose.** Every panel member answers the question independently, in
   parallel, seeing nothing from the others. Independence is the whole point;
   showing them each other's work first would just produce agreement.
2. **Critique.** Each member sees the others' answers, anonymised, and is asked
   what is wrong with them. Anonymised because "the local model said" and "GPT
   said" are not equally persuasive, and the label would do the arguing.
3. **Synthesise.** One model reads the proposals and the critiques and writes
   the answer, with instructions to state disagreement rather than average it.

The thing this must never do is launder disagreement into false confidence.
Three models that split two-to-one on a factual claim have told you something
important, and a synthesis that quietly picks the majority throws it away. So
the run reports where they diverged, separately from the answer, and the UI
shows it.

It costs N times the tokens and roughly N times the wall-clock of one answer.
That is why it is opt-in per question rather than a mode you leave on.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .config import get_config, set_config

# Eight, not four. The old ceiling was a guess about what a laptop could take,
# but the machine and the wallet are the user's to know about: someone with
# eight provider keys has eight models, and a panel is exactly where you would
# want them. The real limits — time, tokens — are visible in the trace as they
# are spent, which is a better brake than a number chosen here.
MAX_MEMBERS = 8
MIN_MEMBERS = 2
MEMBER_TIMEOUT = 300
MAX_ANSWER_CHARS = 8000
# Each member may gather its own evidence, within a small budget. This runs
# once per member per round, so the cost multiplies by the size of the panel.
MAX_TOOL_ROUNDS = 4
TOOL_RESULT_CHARS = 4000

ROUND_PROPOSE = "propose"
ROUND_CRITIQUE = "critique"
ROUND_SYNTHESIS = "synthesis"


class ConsensusError(RuntimeError):
    """Something the user needs to fix, phrased for them."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===== Prompts =====

PROPOSE_PROMPT = (
    "Answer the question below as well as you can. Be concrete and show your "
    "reasoning briefly. If you are unsure of something, say which part — an "
    "answer that flags its own weak point is worth more here than one that "
    "sounds certain throughout.\n\n"
    "If you have search or page-reading tools, use them for anything that "
    "depends on current fact rather than answering from memory, and name the "
    "sources you used. Another model is answering this same question "
    "separately; an answer grounded in something you actually looked up is "
    "what makes the comparison worth anything.\n\nQUESTION: {question}"
)

CRITIQUE_PROMPT = (
    "Below is a question and several independent answers to it, labelled A, B, "
    "C. One of them is yours; you are not told which, and it does not matter.\n\n"
    "For each answer, say specifically what is wrong, missing, or unsupported. "
    "Quote the exact claim you are disputing. If an answer is correct, say so "
    "in one line rather than inventing a criticism — manufactured disagreement "
    "is worse than none.\n\n"
    "Then state which single answer you would act on, and why.\n\n"
    "QUESTION: {question}\n\n{answers}"
)

SYNTHESIS_PROMPT = (
    "Below is a question, several independent answers, and the panel's "
    "critiques of those answers. Write the final answer.\n\n"
    "Rules:\n"
    "- Where the panel agreed, state the conclusion plainly.\n"
    "- Where it disagreed, say so explicitly and give both positions. Do not "
    "average them, and do not silently pick the majority — a split panel is a "
    "real finding and hiding it is the worst thing you can do here.\n"
    "- Where a critique found a concrete error, correct it.\n"
    "- Do not mention 'the panel', 'answer A', or this process in your prose. "
    "Write the answer the user asked for.\n\n"
    "QUESTION: {question}\n\n{answers}\n\nCRITIQUES:\n{critiques}"
)

LABELS = "ABCDEFGH"


# ===== Panels =====

def panel() -> List[Dict[str, Any]]:
    """The configured members. Each is ``{provider, model, role}``."""
    raw = get_config().get("consensus_panel", [])
    return [m for m in raw if isinstance(m, dict) and m.get("model")]


def set_panel(members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned = []
    for member in members or []:
        if not isinstance(member, dict) or not str(member.get("model", "")).strip():
            continue
        cleaned.append({
            "provider": str(member.get("provider") or "").strip(),
            "model": str(member["model"]).strip(),
            "role": str(member.get("role") or "").strip(),
        })
    if len(cleaned) > MAX_MEMBERS:
        raise ConsensusError(f"a panel is at most {MAX_MEMBERS} models")
    set_config("consensus_panel", cleaned)
    return cleaned


def synthesiser() -> Dict[str, Any]:
    """Which model writes the final answer.

    Defaults to the first panel member rather than a hardcoded one: on a
    laptop-only install there may be no stronger model to promote, and quietly
    routing the synthesis to a cloud provider nobody configured would be a
    surprise on the bill.
    """
    configured = get_config().get("consensus_synthesiser", {}) or {}
    if configured.get("model"):
        return configured
    members = panel()
    return members[0] if members else {}


def set_synthesiser(member: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    value = {
        "provider": str((member or {}).get("provider") or "").strip(),
        "model": str((member or {}).get("model") or "").strip(),
    } if member and member.get("model") else {}
    set_config("consensus_synthesiser", value)
    return value


def require_panel() -> List[Dict[str, Any]]:
    members = panel()
    if len(members) < MIN_MEMBERS:
        raise ConsensusError(
            f"a debate needs at least {MIN_MEMBERS} models. Add them in "
            f"Settings → Consensus — two models that fail differently are the "
            f"whole point, so pick ones that are not the same family."
        )
    return members


# ===== Running one member =====

def _ask(route_fn, stream_fn, member: Dict[str, Any], prompt: str,
         tools: Optional[List[Dict[str, Any]]] = None,
         run_tool: Optional[Callable] = None) -> Dict[str, Any]:
    """One model, one prompt, with a bounded tool loop. Never raises.

    Tools matter more here than in a single-model turn, not less. A panel asked
    about anything recent with no way to look it up is three models reciting
    training data at each other, and the disagreement it produces is noise
    about what each one memorised rather than signal about the question. So a
    member searches for its own evidence, independently — which also means two
    members can find *different* sources, and that disagreement is real.

    The loop is small on purpose: this runs once per member per round, and a
    member that wants fifteen rounds of tool calls is not debating any more.
    """
    started = time.time()
    calls = 0
    try:
        resolved = route_fn(task="reasoning", model=member["model"],
                            provider=member.get("provider") or None)
        messages = [{"role": "user", "content": prompt}]
        parts: List[str] = []

        for _ in range(MAX_TOOL_ROUNDS if (tools and run_tool) else 1):
            round_text: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            for event in stream_fn(resolved, messages, tools=tools or None):
                kind = event.get("type")
                if kind == "tool_calls":
                    tool_calls.extend(event.get("calls", []))
                elif kind in ("text", "content"):
                    round_text.append(event.get("text", ""))
            parts.extend(round_text)
            if not tool_calls or not run_tool:
                break

            messages.append({"role": "assistant", "content": "".join(round_text),
                             "tool_calls": tool_calls})
            for call in tool_calls:
                function = call.get("function", {})
                name = function.get("name", "")
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                try:
                    result = run_tool(name, arguments)
                except Exception as exc:
                    # A tool that breaks is something the model should be told
                    # about and work around, exactly as it would a 404. Letting
                    # it kill the member would lose that member's whole answer
                    # over one failed search.
                    result = f"error: {exc}"
                calls += 1
                messages.append({"role": "tool", "content": str(result)[:TOOL_RESULT_CHARS],
                                 "name": name, "tool_call_id": call.get("id", name)})
            # The prose from a tool-calling round is preamble, not the answer.
            parts = []

        text = "".join(parts).strip()[:MAX_ANSWER_CHARS]
        if not text:
            return {**member, "ok": False, "error": "returned nothing", "text": "",
                    "tool_calls": calls, "seconds": round(time.time() - started, 1)}
        return {**member, "ok": True, "error": "", "text": text,
                "tool_calls": calls, "seconds": round(time.time() - started, 1)}
    except Exception as exc:
        # One member being unreachable must not lose the other's work.
        return {**member, "ok": False, "error": str(exc)[:300], "text": "",
                "tool_calls": calls, "seconds": round(time.time() - started, 1)}


def _ask_all(route_fn, stream_fn, members: List[Dict[str, Any]],
             prompts: List[str], tools: Optional[List[Dict[str, Any]]] = None,
             run_tool: Optional[Callable] = None) -> List[Dict[str, Any]]:
    """Run every member in parallel. Order of results follows the panel.

    Parallel because serial would make a three-model debate three times the
    wait, which is the difference between a feature people use for hard
    questions and one they never use at all.
    """
    results: List[Optional[Dict[str, Any]]] = [None] * len(members)

    def work(index: int) -> None:
        results[index] = _ask(route_fn, stream_fn, members[index], prompts[index],
                              tools=tools, run_tool=run_tool)

    threads = [
        threading.Thread(target=work, args=(i,), daemon=True, name=f"consensus-{i}")
        for i in range(len(members))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=MEMBER_TIMEOUT)
    return [
        r or {**members[i], "ok": False, "error": f"timed out after {MEMBER_TIMEOUT}s",
              "text": "", "tool_calls": 0, "seconds": MEMBER_TIMEOUT}
        for i, r in enumerate(results)
    ]


def _labelled(answers: List[Dict[str, Any]]) -> str:
    """The proposals, anonymised.

    "The local model said" and "Claude said" are not equally persuasive, and a
    critique round that shows the labels is measuring reputation rather than
    reasoning.
    """
    blocks = []
    for index, answer in enumerate(answers):
        if answer["ok"]:
            blocks.append(f"ANSWER {LABELS[index]}:\n{answer['text']}")
    return "\n\n".join(blocks)


# ===== Disagreement =====

# Words that flip a claim. A sentence containing one of these on one side and
# not the other is the cheap signal that two answers are not saying the same
# thing; it is a prompt for the reader, never a verdict.
NEGATIONS = ("not", "never", "cannot", "isn't", "doesn't", "won't", "no ", "unsafe",
             "incorrect", "wrong", "false", "avoid")
DISAGREEMENT_MARKERS = (
    "disagree", "incorrect", "wrong", "mistaken", "actually", "however",
    "contrary", "not true", "misleading", "overstates", "understates",
)


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def find_disagreements(answers: List[Dict[str, Any]],
                       critiques: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Where the panel visibly disagreed, for the reader to judge.

    This is a signal, not an adjudication. It surfaces the sentences in the
    critiques that assert another answer is wrong, because those are the places
    a human should look — and because a synthesis that hides them is exactly
    the failure this whole feature exists to avoid.
    """
    found = []
    for critique in critiques:
        if not critique.get("ok"):
            continue
        for sentence in _sentences(critique["text"]):
            lowered = sentence.lower()
            if any(marker in lowered for marker in DISAGREEMENT_MARKERS):
                found.append({
                    "from": critique.get("model", ""),
                    "point": sentence[:400],
                })
            if len(found) >= 12:
                return found
    return found


def agreement_ratio(answers: List[Dict[str, Any]]) -> float:
    """A crude lexical overlap between the proposals, 0 to 1.

    Deliberately crude, and never shown as a percentage of correctness: two
    answers can agree word for word and both be wrong. It is only used to
    decide whether to tell the reader "these came out close" or "these came out
    far apart, look at the differences".
    """
    texts = [a["text"] for a in answers if a.get("ok") and a.get("text")]
    if len(texts) < 2:
        return 0.0
    sets = [set(re.findall(r"[a-z]{4,}", t.lower())) for t in texts]
    scores = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            union = sets[i] | sets[j]
            if union:
                scores.append(len(sets[i] & sets[j]) / len(union))
    return round(sum(scores) / len(scores), 3) if scores else 0.0


# ===== The run =====

def debate(question: str, route_fn: Callable, stream_fn: Callable,
           members: Optional[List[Dict[str, Any]]] = None,
           on_event: Optional[Callable] = None,
           tools: Optional[List[Dict[str, Any]]] = None,
           run_tool: Optional[Callable] = None) -> Dict[str, Any]:
    """Propose, critique, synthesise. Returns the answer and the whole argument.

    ``route_fn`` and ``stream_fn`` are injected so the orchestration can be
    tested without a model — the sequencing is the part most likely to be
    wrong, and it should not need a GPU to check.
    """
    text = (question or "").strip()
    if not text:
        raise ConsensusError("a debate needs a question")
    members = members or require_panel()

    def emit(event: Dict[str, Any]) -> None:
        if on_event:
            on_event(event)

    run_id = uuid.uuid4().hex[:12]
    started = time.time()

    # --- Round 1: independent proposals ---
    emit({"round": ROUND_PROPOSE, "members": len(members)})
    proposals = _ask_all(
        route_fn, stream_fn, members,
        [PROPOSE_PROMPT.format(question=text)] * len(members),
        tools=tools, run_tool=run_tool,
    )
    emit({"proposals": [
        {"model": p["model"], "ok": p["ok"], "seconds": p["seconds"],
         "error": p["error"], "tool_calls": p.get("tool_calls", 0)}
        for p in proposals
    ]})

    alive = [p for p in proposals if p["ok"]]
    if not alive:
        raise ConsensusError(
            "no model in the panel answered. " +
            "; ".join(f"{p['model']}: {p['error']}" for p in proposals)
        )
    if len(alive) == 1:
        # One survivor is not a debate, and calling it one would be a lie.
        return _single(run_id, text, proposals, alive[0], started)

    # --- Round 2: critique, anonymised ---
    emit({"round": ROUND_CRITIQUE})
    labelled = _labelled(proposals)
    # Critics get tools too: "that claim is out of date" is only worth saying
    # if the critic could actually go and check.
    critiques = _ask_all(
        route_fn, stream_fn, members,
        [CRITIQUE_PROMPT.format(question=text, answers=labelled)] * len(members),
        tools=tools, run_tool=run_tool,
    )
    emit({"critiques": [
        {"model": c["model"], "ok": c["ok"], "seconds": c["seconds"]} for c in critiques
    ]})

    # --- Round 3: synthesis ---
    emit({"round": ROUND_SYNTHESIS})
    judge = synthesiser() or members[0]
    critique_text = "\n\n".join(
        f"CRITIQUE {index + 1}:\n{c['text']}"
        for index, c in enumerate(critiques) if c["ok"]
    ) or "(no critiques were produced)"
    # No tools for the synthesis. Its job is to reconcile what the panel found,
    # and a judge that goes off to search has stopped judging and started
    # adding a fourth opinion nobody critiqued.
    final = _ask(route_fn, stream_fn, judge, SYNTHESIS_PROMPT.format(
        question=text, answers=labelled, critiques=critique_text))

    if not final["ok"]:
        # Falling back to the longest proposal beats returning nothing, and
        # saying which one it is beats passing it off as a synthesis.
        best = max(alive, key=lambda a: len(a["text"]))
        final = {**best, "fallback": True}

    return {
        "id": run_id,
        "question": text,
        "answer": final["text"],
        "synthesised": not final.get("fallback"),
        "synthesiser": judge.get("model", ""),
        "proposals": proposals,
        "critiques": critiques,
        "disagreements": find_disagreements(proposals, critiques),
        "agreement": agreement_ratio(proposals),
        "tool_calls": sum(p.get("tool_calls", 0) for p in proposals + critiques),
        "seconds": round(time.time() - started, 1),
        "created_at": _now(),
    }


def _single(run_id: str, question: str, proposals: List[Dict[str, Any]],
            survivor: Dict[str, Any], started: float) -> Dict[str, Any]:
    """One model answered and the rest failed. Say so; do not call it consensus."""
    return {
        "id": run_id,
        "question": question,
        "answer": survivor["text"],
        "synthesised": False,
        "synthesiser": survivor.get("model", ""),
        "proposals": proposals,
        "critiques": [],
        "disagreements": [],
        "agreement": 0.0,
        "degraded": (
            "Only one model answered, so this is its answer rather than a "
            "panel's. The others: "
            + "; ".join(f"{p['model']} ({p['error']})" for p in proposals if not p["ok"])
        ),
        "seconds": round(time.time() - started, 1),
        "created_at": _now(),
    }


# ===== History =====

MAX_HISTORY = 20


def save_run(run: Dict[str, Any]) -> None:
    stored = get_config().get("consensus_runs", []) or []
    trimmed = {
        k: run[k] for k in
        ("id", "question", "answer", "agreement", "disagreements", "seconds", "created_at")
        if k in run
    }
    trimmed["models"] = [p["model"] for p in run.get("proposals", [])]
    set_config("consensus_runs", ([trimmed] + stored)[:MAX_HISTORY])


def history() -> List[Dict[str, Any]]:
    return get_config().get("consensus_runs", []) or []

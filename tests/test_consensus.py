"""Two models, made to argue, before you trust either of them.

The orchestration is injected with fake model functions throughout, because
the sequencing is the part most likely to be wrong and it should not need a GPU
to check: that proposals really are independent, that critiques really are
anonymised, that one dead member does not lose the other's work, and above all
that a split panel is reported as split rather than quietly averaged.
"""
from unittest.mock import patch

import pytest

from carrot import consensus


PANEL = [
    {"provider": "ollama", "model": "qwen-coder", "role": "code"},
    {"provider": "ollama", "model": "deepseek-r1", "role": "reasoning"},
]


def route(**kwargs):
    return kwargs


def scripted(answers, critique="Answer A is fine.", synthesis="The final answer."):
    """A fake panel: `answers` per model, one critique, one synthesis."""
    def stream(resolved, messages, tools=None):
        prompt = messages[0]["content"]
        model = resolved.get("model", "")
        if "CRITIQUES:" in prompt:
            yield {"type": "text", "text": synthesis}
        elif "ANSWER A" in prompt:
            yield {"type": "text", "text": critique}
        else:
            yield {"type": "text", "text": answers.get(model, f"{model} says something")}
    return stream


@pytest.fixture
def panel(isolated_db):
    consensus.set_panel(PANEL)
    return PANEL


class TestPanelConfiguration:
    def test_no_panel_by_default(self, isolated_db):
        # Carrot cannot guess which two models you have, and picking for you
        # would silently spend tokens on one you did not choose.
        assert consensus.panel() == []

    def test_a_debate_needs_two_models(self, isolated_db):
        consensus.set_panel([PANEL[0]])
        with pytest.raises(consensus.ConsensusError) as caught:
            consensus.require_panel()
        assert "at least 2" in str(caught.value)

    def test_the_error_explains_what_makes_a_good_panel(self, isolated_db):
        with pytest.raises(consensus.ConsensusError) as caught:
            consensus.require_panel()
        assert "fail differently" in str(caught.value)

    def test_a_panel_can_be_saved(self, panel):
        assert [m["model"] for m in consensus.panel()] == ["qwen-coder", "deepseek-r1"]

    def test_members_without_a_model_are_dropped(self, isolated_db):
        consensus.set_panel([{"provider": "ollama"}, PANEL[0]])
        assert len(consensus.panel()) == 1

    def test_an_oversized_panel_is_refused(self, isolated_db):
        with pytest.raises(consensus.ConsensusError):
            consensus.set_panel([{"model": f"m{i}"} for i in range(consensus.MAX_MEMBERS + 1)])

    def test_the_synthesiser_defaults_to_the_first_member(self, panel):
        # Not to a hardcoded cloud model: quietly routing synthesis to a
        # provider nobody configured is a surprise on the bill.
        assert consensus.synthesiser()["model"] == "qwen-coder"

    def test_the_synthesiser_can_be_chosen(self, panel):
        consensus.set_synthesiser({"provider": "anthropic", "model": "claude-opus-5"})
        assert consensus.synthesiser()["model"] == "claude-opus-5"


class TestTheThreeRounds:
    def test_a_debate_produces_an_answer(self, panel):
        run = consensus.debate("why does this deadlock?", route,
                               scripted({}, synthesis="Because of lock ordering."))
        assert run["answer"] == "Because of lock ordering."
        assert run["synthesised"] is True

    def test_every_member_proposes(self, panel):
        run = consensus.debate("q", route, scripted({}))
        assert len(run["proposals"]) == 2
        assert all(p["ok"] for p in run["proposals"])

    def test_proposals_are_independent(self, panel):
        # Showing them each other's work first would just manufacture
        # agreement, which is the opposite of the point.
        seen = []

        def stream(resolved, messages, tools=None):
            prompt = messages[0]["content"]
            if "ANSWER A" not in prompt and "CRITIQUES:" not in prompt:
                seen.append(prompt)
            yield {"type": "text", "text": "x"}

        consensus.debate("q", route, stream)
        assert all("ANSWER" not in prompt for prompt in seen)

    def test_critiques_see_the_other_answers(self, panel):
        seen = {}

        def stream(resolved, messages, tools=None):
            prompt = messages[0]["content"]
            if "ANSWER A" in prompt and "CRITIQUES:" not in prompt:
                seen["prompt"] = prompt
            yield {"type": "text", "text": "coder says X" if "QUESTION" in prompt else "y"}

        consensus.debate("q", route,
                         scripted({"qwen-coder": "alpha claim", "deepseek-r1": "beta claim"}))
        # Run again with the capturing stream to inspect the critique prompt.
        consensus.debate("q", route, stream)
        assert "ANSWER A" in seen["prompt"] and "ANSWER B" in seen["prompt"]

    def test_critiques_are_anonymised(self, panel):
        # "The local model said" and "Claude said" are not equally persuasive;
        # the label would do the arguing instead of the reasoning.
        seen = {}

        def stream(resolved, messages, tools=None):
            prompt = messages[0]["content"]
            if "ANSWER A" in prompt:
                seen.setdefault("prompt", prompt)
            yield {"type": "text", "text": "something"}

        consensus.debate("q", route, stream)
        assert "qwen-coder" not in seen["prompt"]
        assert "deepseek-r1" not in seen["prompt"]

    def test_the_synthesis_sees_proposals_and_critiques(self, panel):
        seen = {}

        def stream(resolved, messages, tools=None):
            prompt = messages[0]["content"]
            if "CRITIQUES:" in prompt:
                seen["prompt"] = prompt
            yield {"type": "text", "text": "the critique text here"}

        consensus.debate("q", route, stream)
        assert "ANSWER A" in seen["prompt"] and "CRITIQUE 1" in seen["prompt"]

    def test_progress_is_reported_as_it_happens(self, panel):
        events = []
        consensus.debate("q", route, scripted({}), on_event=events.append)
        rounds = [e["round"] for e in events if "round" in e]
        assert rounds == ["propose", "critique", "synthesis"]


class TestDegradation:
    def test_one_dead_member_does_not_lose_the_others_work(self, panel):
        def stream(resolved, messages, tools=None):
            if resolved.get("model") == "deepseek-r1":
                raise RuntimeError("model not pulled")
            yield {"type": "text", "text": "the surviving answer"}

        run = consensus.debate("q", route, stream)
        assert run["answer"]
        assert any(not p["ok"] for p in run["proposals"])

    def test_a_single_survivor_is_not_called_a_consensus(self, panel):
        # Calling one model's answer a panel verdict would be a lie, and the
        # kind that makes the whole feature untrustworthy.
        def stream(resolved, messages, tools=None):
            if resolved.get("model") == "deepseek-r1":
                raise RuntimeError("unreachable")
            yield {"type": "text", "text": "just me then"}

        run = consensus.debate("q", route, stream)
        assert run["synthesised"] is False
        assert "Only one model answered" in run["degraded"]
        assert "deepseek-r1" in run["degraded"]

    def test_a_member_that_returns_nothing_counts_as_failed(self, panel):
        def stream(resolved, messages, tools=None):
            if resolved.get("model") == "deepseek-r1":
                return
                yield  # pragma: no cover
            yield {"type": "text", "text": "an answer"}

        run = consensus.debate("q", route, stream)
        failed = [p for p in run["proposals"] if not p["ok"]]
        assert failed and failed[0]["error"] == "returned nothing"

    def test_a_whole_panel_failing_is_an_error_not_an_empty_answer(self, panel):
        def stream(resolved, messages, tools=None):
            raise RuntimeError("ollama is not running")
            yield  # pragma: no cover

        with pytest.raises(consensus.ConsensusError) as caught:
            consensus.debate("q", route, stream)
        assert "ollama is not running" in str(caught.value)

    def test_a_failed_synthesis_falls_back_and_says_so(self, panel):
        def stream(resolved, messages, tools=None):
            if "CRITIQUES:" in messages[0]["content"]:
                raise RuntimeError("the judge died")
            yield {"type": "text", "text": "a proposal or a critique"}

        run = consensus.debate("q", route, stream)
        assert run["answer"] and run["synthesised"] is False

    def test_an_empty_question_is_refused(self, panel):
        with pytest.raises(consensus.ConsensusError):
            consensus.debate("   ", route, scripted({}))


class TestDisagreementIsNeverHidden:
    def test_a_critique_that_disputes_something_is_surfaced(self, panel):
        run = consensus.debate(
            "q", route,
            scripted({}, critique="Answer A is incorrect: the lock is not reentrant."))
        assert run["disagreements"]
        assert "not reentrant" in run["disagreements"][0]["point"]

    def test_agreement_between_answers_is_measured(self, panel):
        run = consensus.debate("q", route, scripted({
            "qwen-coder": "the lock ordering causes the deadlock here",
            "deepseek-r1": "the lock ordering causes the deadlock here",
        }))
        assert run["agreement"] > 0.9

    def test_divergent_answers_score_low(self, panel):
        run = consensus.debate("q", route, scripted({
            "qwen-coder": "alpha beta gamma delta epsilon",
            "deepseek-r1": "kappa lambda omicron sigma upsilon",
        }))
        assert run["agreement"] < 0.2

    def test_agreement_is_not_a_correctness_score(self):
        from pathlib import Path

        # Two answers can agree word for word and both be wrong; the docstring
        # is where that stays true for the next person reading it.
        source = (Path(__file__).resolve().parents[1] / "carrot" / "consensus.py").read_text()
        assert "both be wrong" in source

    def test_the_synthesis_prompt_forbids_averaging(self):
        assert "Do not " in consensus.SYNTHESIS_PROMPT
        assert "average them" in consensus.SYNTHESIS_PROMPT
        assert "silently pick the majority" in consensus.SYNTHESIS_PROMPT

    def test_the_critique_prompt_discourages_invented_disagreement(self):
        # A panel that always finds fault is as useless as one that never does.
        assert "manufactured disagreement" in consensus.CRITIQUE_PROMPT

    def test_a_clean_critique_produces_no_false_disagreement(self, panel):
        run = consensus.debate("q", route, scripted({}, critique="All three look right."))
        assert run["disagreements"] == []


class TestHistory:
    def test_a_run_is_remembered(self, panel):
        run = consensus.debate("why?", route, scripted({}))
        consensus.save_run(run)
        assert consensus.history()[0]["question"] == "why?"

    def test_history_records_which_models_argued(self, panel):
        consensus.save_run(consensus.debate("q", route, scripted({})))
        assert consensus.history()[0]["models"] == ["qwen-coder", "deepseek-r1"]

    def test_history_is_bounded(self, panel):
        run = consensus.debate("q", route, scripted({}))
        for _ in range(consensus.MAX_HISTORY + 5):
            consensus.save_run(run)
        assert len(consensus.history()) == consensus.MAX_HISTORY


class TestParallelism:
    def test_members_run_at_the_same_time(self, panel):
        # Serial would make a three-model debate three times the wait, which is
        # the difference between a feature people use and one they never do.
        import time

        def slow(resolved, messages, tools=None):
            time.sleep(0.3)
            yield {"type": "text", "text": "an answer"}

        started = time.time()
        consensus._ask_all(route, slow, PANEL, ["p", "p"])
        assert time.time() - started < 0.55


class TestEndpoints:
    def test_state_reports_it_is_not_ready(self, client):
        body = client.get("/api/consensus").json()
        assert body["ready"] is False and body["panel"] == []

    def test_a_panel_can_be_set_over_http(self, client):
        body = client.put("/api/consensus/panel", json={"members": PANEL})
        assert len(body.json()["panel"]) == 2
        assert client.get("/api/consensus").json()["ready"] is True

    def test_debating_without_a_panel_is_a_400(self, client):
        body = client.post("/api/consensus/debate", json={"question": "q"})
        assert body.status_code == 400 and "at least 2" in body.json()["detail"]

    def test_an_oversized_panel_is_a_400(self, client):
        body = client.put("/api/consensus/panel",
                          json={"members": [{"model": f"m{i}"} for i in range(9)]})
        assert body.status_code == 400

    def test_a_synthesiser_can_be_chosen_over_http(self, client):
        client.put("/api/consensus/panel", json={"members": PANEL})
        body = client.put("/api/consensus/synthesiser",
                          json={"provider": "anthropic", "model": "claude-opus-5"})
        assert body.json()["synthesiser"]["model"] == "claude-opus-5"

    def test_a_non_streamed_debate_returns_the_whole_run(self, client):
        from carrot import router as router_mod

        client.put("/api/consensus/panel", json={"members": PANEL})
        with patch.object(router_mod, "route", route), \
             patch.object(router_mod, "stream_events", scripted({}, synthesis="Done.")):
            body = client.post("/api/consensus/debate",
                               json={"question": "q", "stream": False})
        assert body.json()["answer"] == "Done."

    def test_the_endpoints_need_a_session(self, unauthenticated_client):
        assert unauthenticated_client.get("/api/consensus").status_code == 401


class TestTheInterface:
    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath("carrot", "web", *parts).read_text()

    def test_the_settings_panel_exists(self):
        assert 'id="consensus-panel"' in self.read("index.html")

    def test_it_loads_with_settings(self):
        assert "loadConsensusPanel()" in self.read("js", "dashboard.js")

    def test_there_is_a_debate_button_in_the_composer(self):
        assert 'id="debate-btn"' in self.read("index.html")

    def test_the_chip_is_always_visible(self):
        # Hiding it until a panel existed meant nobody discovered that a panel
        # was a thing you could make — discovering it required already knowing.
        js = self.read("js", "studio.js")
        assert "chip.classList.remove('hidden');" in js
        assert 'id="debate-btn" class="composer-chip needs-setup"' in self.read("index.html")

    def test_an_unconfigured_chip_takes_you_to_the_setup(self):
        # A chip that does nothing teaches people the feature is broken.
        js = self.read("js", "studio.js")
        start = js.split("async function debateCurrentQuestion")[1][:600]
        assert "switchTab('settings')" in start

    def test_the_chip_state_is_known_from_the_first_paint(self):
        # It lives in the composer, so loading it only on the Settings tab left
        # it wrong for everyone who never opened Settings.
        assert "loadConsensusPanel();" in self.read("js", "app.js")

    def test_the_panel_loader_survives_a_missing_settings_host(self):
        js = self.read("js", "studio.js")
        assert "renderCouncilChip();\n    if (!host) return;" in js

    def test_the_settings_copy_says_to_pick_models_that_differ(self):
        # Two models from the same family agree for the same reasons, which is
        # the failure mode this whole feature exists to avoid.
        assert "fail <em>differently</em>" in self.read("index.html")

    def test_the_settings_copy_is_honest_about_the_cost(self):
        assert "as many\n          tokens" in self.read("index.html")

    def test_models_are_picked_not_typed(self):
        # Typing a model name by hand was the wrong answer to a question the
        # app already knows: a typo produced a panel member that failed at
        # debate time rather than at pick time.
        html = self.read("index.html")
        assert '<select id="consensus-add"' in html
        assert '<select id="consensus-judge"' in html
        assert 'input type="text" id="consensus-model"' not in html

    def test_the_picker_offers_local_and_remote_models(self):
        js = self.read("js", "studio.js")
        assert "'/api/models'" in js
        assert "data.installed" in js and "data.remote" in js

    def test_models_already_on_the_panel_are_not_offered_twice(self):
        assert "if (chosen.has(memberKey(entry))) continue;" in self.read("js", "studio.js")

    def test_the_picker_stops_at_the_panel_ceiling(self):
        assert "Panel is full" in self.read("js", "studio.js")

    def test_the_judge_can_only_be_a_panel_member(self):
        # A judge that never saw the debate is not a judge.
        assert "not a judge" in self.read("js", "studio.js")

    def test_a_pairing_is_suggested_from_different_families(self):
        # Two models from one family agree for the same reasons.
        js = self.read("js", "studio.js")
        assert "function renderPanelSuggestion" in js
        assert "wrong about different things" in js

    def test_no_models_at_all_says_what_to_do(self):
        assert "pull one, or add a provider key" in self.read("js", "studio.js")

    def test_disagreement_renders_above_the_answer(self):
        js = self.read("js", "studio.js")
        render = js.split("function renderDebate(run, content)")[1]
        assert render.index("debate-split") < render.index("mdToHtml")

    def test_the_degraded_case_is_shown_not_hidden(self):
        assert "run.degraded" in self.read("js", "studio.js")

    def test_progress_is_narrated_while_it_runs(self):
        # A minute of silence looks like a hang.
        js = self.read("js", "studio.js")
        assert "answering independently" in js and "critiquing each other" in js

    def test_every_css_token_the_debate_ui_uses_is_defined(self):
        import re

        css = self.read("css", "style.css")
        block = css.split("/* ===== Consensus & debate =====")[1]
        used = set(re.findall(r"var\((--[a-z0-9-]+)", block))
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
        assert used <= defined, f"undefined CSS tokens: {sorted(used - defined)}"


class TestPanelMembersCanSearch:
    """A panel with no tools is models reciting training data at each other.

    The disagreement that produces is noise about what each one memorised, not
    signal about the question — so every member gathers its own evidence.
    """

    def toolset(self):
        return [{"type": "function", "function": {"name": "carrot__web_search"}}]

    def searching(self, answer="Reuters reports the vote failed."):
        """A model that searches once, then answers."""
        state = {}

        def stream(resolved, messages, tools=None):
            model = resolved.get("model", "")
            if tools and not state.get(model):
                state[model] = True
                yield {"type": "tool_calls", "calls": [{
                    "id": "1",
                    "function": {"name": "carrot__web_search",
                                 "arguments": {"query": "the question"}},
                }]}
                return
            yield {"type": "text", "text": answer}

        return stream

    def test_a_member_can_call_a_tool(self, panel):
        ran = []
        run = consensus.debate("recent news?", route, self.searching(),
                               tools=self.toolset(),
                               run_tool=lambda n, a: ran.append(n) or "Reuters: the vote failed")
        assert ran, "no member ever called a tool"
        assert run["answer"]

    def test_every_member_searches_independently(self, panel):
        # Two members can find *different* sources, and that disagreement is
        # real rather than an artefact of what each memorised.
        callers = []

        def stream(resolved, messages, tools=None):
            model = resolved.get("model", "")
            if tools and model not in callers:
                callers.append(model)
                yield {"type": "tool_calls", "calls": [{
                    "id": "1", "function": {"name": "carrot__web_search", "arguments": {}}}]}
                return
            yield {"type": "text", "text": "an answer"}

        consensus.debate("q", route, stream, tools=self.toolset(),
                         run_tool=lambda n, a: "result")
        assert set(callers) == {"qwen-coder", "deepseek-r1"}

    def test_the_tool_result_reaches_the_model(self, panel):
        seen = {}

        def stream(resolved, messages, tools=None):
            if any(m.get("role") == "tool" for m in messages):
                seen["content"] = messages[-1]["content"]
                yield {"type": "text", "text": "grounded answer"}
                return
            yield {"type": "tool_calls", "calls": [{
                "id": "1", "function": {"name": "carrot__web_search", "arguments": {}}}]}

        consensus.debate("q", route, stream, tools=self.toolset(),
                         run_tool=lambda n, a: "AP: the vote failed 51-49")
        assert "51-49" in seen["content"]

    def test_tool_calls_are_counted_and_reported(self, panel):
        run = consensus.debate("q", route, self.searching(), tools=self.toolset(),
                               run_tool=lambda n, a: "result")
        assert run["tool_calls"] > 0
        assert any(p.get("tool_calls") for p in run["proposals"])

    def test_critics_can_search_too(self, panel):
        # "That claim is out of date" is only worth saying if the critic could
        # go and check.
        rounds = []

        def stream(resolved, messages, tools=None):
            prompt = messages[0]["content"]
            if "ANSWER A" in prompt and "CRITIQUES:" not in prompt and tools:
                rounds.append("critique")
                yield {"type": "tool_calls", "calls": [{
                    "id": "1", "function": {"name": "carrot__web_search", "arguments": {}}}]}
                return
            yield {"type": "text", "text": "text"}

        consensus.debate("q", route, stream, tools=self.toolset(),
                         run_tool=lambda n, a: "result")
        assert "critique" in rounds

    def test_the_synthesis_gets_no_tools(self, panel):
        # A judge that goes off to search has stopped judging and started
        # adding a fourth opinion nobody critiqued.
        seen = []

        def stream(resolved, messages, tools=None):
            if "CRITIQUES:" in messages[0]["content"]:
                seen.append(tools)
            yield {"type": "text", "text": "text"}

        consensus.debate("q", route, stream, tools=self.toolset(),
                         run_tool=lambda n, a: "result")
        assert seen == [None]

    def test_a_member_looping_on_tools_is_stopped(self, panel):
        # A member that wants fifteen rounds of tool calls is not debating any
        # more. It gets a budget, and running out is a bounded failure rather
        # than an unbounded spend.
        calls = []

        def forever(resolved, messages, tools=None):
            if tools:
                yield {"type": "tool_calls", "calls": [{
                    "id": "1", "function": {"name": "carrot__web_search", "arguments": {}}}]}
                return
            yield {"type": "text", "text": "x"}

        with pytest.raises(consensus.ConsensusError):
            consensus.debate("q", route, forever, tools=self.toolset(),
                             run_tool=lambda n, a: calls.append(n) or "result")
        assert 0 < len(calls) <= consensus.MAX_TOOL_ROUNDS * len(PANEL)

    def test_a_tool_that_raises_does_not_kill_the_member(self, panel):
        # Losing a member's whole answer because one search failed is a bad
        # trade; it should be told, and work around it, exactly as it would a
        # 404 from a page.
        seen = {}

        def stream(resolved, messages, tools=None):
            if any(m.get("role") == "tool" for m in messages):
                seen["content"] = messages[-1]["content"]
                yield {"type": "text", "text": "I could not search, so from memory:"}
                return
            yield {"type": "tool_calls", "calls": [{
                "id": "1", "function": {"name": "carrot__web_search", "arguments": {}}}]}

        def exploding(name, arguments):
            raise RuntimeError("the search backend is down")

        run = consensus.debate("q", route, stream, tools=self.toolset(),
                               run_tool=exploding)
        assert "search backend is down" in seen["content"]
        assert all(p["ok"] for p in run["proposals"])

    def test_no_tools_still_works(self, panel):
        run = consensus.debate("q", route, scripted({}))
        assert run["answer"]

    def test_the_propose_prompt_asks_members_to_look_things_up(self):
        assert "search or page-reading tools" in consensus.PROPOSE_PROMPT
        assert "rather than answering from memory" in consensus.PROPOSE_PROMPT

    def test_the_endpoint_hands_the_panel_the_apps_tools(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "carrot" / "consensus_api.py").read_text()
        assert "_available_tools" in source and "run_tool=run_tool" in source

    def test_the_search_mode_can_be_chosen_per_debate(self, client):
        from carrot import router as router_mod

        client.put("/api/consensus/panel", json={"members": PANEL})
        with patch.object(router_mod, "route", route), \
             patch.object(router_mod, "stream_events", scripted({}, synthesis="Done.")):
            body = client.post("/api/consensus/debate",
                               json={"question": "q", "stream": False, "search_mode": "off"})
        assert body.status_code == 200

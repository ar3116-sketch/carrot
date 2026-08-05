"""A chat that is answered but not remembered.

Every assistant that quietly learns from everything you type needs an off
switch, and it needs to be per-conversation rather than global — the reason to
want one is usually a single question, not a change of policy.

The properties that matter are all negative, so that is what is tested: nothing
extracted into memory, nothing summarised, nothing filed in a workspace, and
gone after a restart *including* a crash, because "temporary" that depends on a
clean shutdown is only usually-temporary.
"""
from unittest.mock import patch

import pytest

from carrot import app as A, conversation as conv_mod


class TestMarking:
    def test_a_normal_chat_is_not_temporary(self, isolated_db):
        conv = conv_mod.create_conversation("normal")
        assert conv_mod.is_temporary(conv["id"]) is False

    def test_a_temporary_chat_is_marked(self, isolated_db):
        conv = conv_mod.create_conversation("temp", {conv_mod.TEMPORARY_KEY: True})
        assert conv_mod.is_temporary(conv["id"]) is True

    def test_an_unknown_conversation_is_not_temporary(self, isolated_db):
        assert conv_mod.is_temporary("nonexistent") is False

    def test_no_id_is_not_temporary(self, isolated_db):
        assert conv_mod.is_temporary("") is False

    def test_the_chat_request_carries_the_flag(self):
        assert "temporary" in A.ChatRequest.model_fields


class TestNothingIsRemembered:
    def test_the_post_turn_bookkeeping_is_skipped(self, isolated_db):
        conv = conv_mod.create_conversation("temp", {conv_mod.TEMPORARY_KEY: True})
        conv_mod.add_message(conv["id"], "user", "my bank pin is 1234")

        with patch.object(A.memory_mod, "extract_from_turn") as extract, \
             patch.object(A.summarize_mod, "maybe_summarize") as summarize:
            A._post_turn(conv["id"], "my bank pin is 1234", "noted", 1)
            import time
            time.sleep(0.2)   # the work runs on a thread
        assert not extract.called, "a temporary chat was mined for memories"
        assert not summarize.called

    def test_a_normal_chat_is_still_remembered(self, isolated_db):
        conv = conv_mod.create_conversation("normal")
        conv_mod.add_message(conv["id"], "user", "I live in Boston")

        with patch.object(A.memory_mod, "extract_from_turn") as extract:
            A._post_turn(conv["id"], "I live in Boston", "noted", 1)
            import time
            time.sleep(0.2)
        assert extract.called

    def test_a_temporary_chat_is_filed_nowhere(self, isolated_db):
        # A dangling workspace entry would be a trace of a chat that was
        # supposed to leave none.
        from carrot import workspaces

        conv = conv_mod.create_conversation("temp", {conv_mod.TEMPORARY_KEY: True})
        assert workspaces.workspace_of(workspaces.KIND_CONVERSATION, conv["id"]) in ("", None)


class TestPurging:
    def test_temporary_chats_are_found(self, isolated_db):
        conv_mod.create_conversation("normal")
        temp = conv_mod.create_conversation("temp", {conv_mod.TEMPORARY_KEY: True})
        assert conv_mod.temporary_ids() == [temp["id"]]

    def test_purging_deletes_only_the_temporary_ones(self, isolated_db):
        normal = conv_mod.create_conversation("normal")
        conv_mod.create_conversation("temp", {conv_mod.TEMPORARY_KEY: True})
        assert conv_mod.purge_temporary() == 1
        assert conv_mod.get_conversation(normal["id"]) is not None
        assert conv_mod.temporary_ids() == []

    def test_purging_takes_the_messages_with_it(self, isolated_db):
        temp = conv_mod.create_conversation("temp", {conv_mod.TEMPORARY_KEY: True})
        conv_mod.add_message(temp["id"], "user", "something private")
        conv_mod.purge_temporary()
        assert conv_mod.get_conversation(temp["id"]) is None

    def test_purging_nothing_is_not_an_error(self, isolated_db):
        assert conv_mod.purge_temporary() == 0

    def test_startup_sweeps_them(self):
        from pathlib import Path

        # "Temporary" that survives a crash is not temporary. Sweeping at
        # startup makes the promise unconditional rather than dependent on a
        # clean shutdown.
        source = (Path(__file__).resolve().parents[1] / "carrot" / "app.py").read_text()
        startup = source.split("def startup():")[1][:600]
        assert "purge_temporary()" in startup

    def test_they_can_be_purged_on_demand(self, client, isolated_db):
        conv_mod.create_conversation("temp", {conv_mod.TEMPORARY_KEY: True})
        assert client.post("/api/conversations/temporary/purge").json()["deleted"] == 1


class TestTheInterface:
    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath("carrot", "web", *parts).read_text()

    def test_there_is_a_toggle_in_the_composer(self):
        assert 'id="temp-btn"' in self.read("index.html")

    def test_the_flag_is_sent_with_the_turn(self):
        assert "temporary: temporaryChat," in self.read("js", "app.js")

    def test_switching_mode_starts_a_new_chat(self):
        # Flipping mid-conversation would be a lie either way: the earlier
        # turns are already remembered, or already not.
        js = self.read("js", "app.js")
        toggle = js.split("function toggleTemporaryChat")[1][:400]
        assert "newChat()" in toggle

    def test_the_banner_says_what_is_and_is_not_saved(self):
        js = self.read("js", "app.js")
        assert "saved to memory" in js and "deleted when Carrot" in js
        assert "summarised" in js and "filed in a workspace" in js

    def test_the_banner_does_not_overclaim(self):
        # Attachments still go through the normal pipeline; saying otherwise
        # would be the kind of privacy promise that is worse than none.
        assert "Attachments you send are still processed normally" in self.read("js", "app.js")

    def test_every_css_token_the_banner_uses_is_defined(self):
        import re

        css = self.read("css", "style.css")
        block = css.split("/* ===== Temporary chats =====")[1]
        used = set(re.findall(r"var\((--[a-z0-9-]+)", block))
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
        assert used <= defined, f"undefined CSS tokens: {sorted(used - defined)}"


class TestPanelCeiling:
    def test_eight_models_are_allowed(self, isolated_db):
        # Someone with eight provider keys has eight models, and a panel is
        # exactly where they would want them.
        from carrot import consensus

        consensus.set_panel([{"model": f"m{i}"} for i in range(8)])
        assert len(consensus.panel()) == 8

    def test_nine_is_still_refused(self, isolated_db):
        from carrot import consensus

        with pytest.raises(consensus.ConsensusError):
            consensus.set_panel([{"model": f"m{i}"} for i in range(9)])

    def test_there_is_a_label_for_every_seat(self):
        from carrot import consensus

        assert len(consensus.LABELS) >= consensus.MAX_MEMBERS

    def test_the_ceiling_explains_itself(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "carrot" / "consensus.py").read_text()
        assert "the machine and the wallet are the user's to know about" in source

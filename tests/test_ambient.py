"""The rules ambient capture has to obey before it is allowed to exist.

These tests are written against properties rather than message strings, because
the messages will be reworded and the promises must not be. The promises are:
nobody has to know to exclude incognito, a password field is never captured, a
credential manager is never captured, an exclusion the user typed is honoured,
and capture gets out of the way of the models and the battery.
"""
from unittest.mock import patch

import pytest

from carrot import ambient


def ctx(**kwargs):
    """A capture moment. Nothing is set unless a test sets it."""
    return kwargs


@pytest.fixture
def on(isolated_db):
    """Capture turned on, so privacy and resource tests reach their gate."""
    ambient.set_policy({"enabled": True})
    return ambient.policy()


class TestDefaults:
    def test_off_until_asked_for(self, isolated_db):
        # A feature that watches the screen does not get to be opt-out.
        assert ambient.policy()["enabled"] is False

    def test_disabled_is_the_reason_given(self, isolated_db):
        decision = ambient.should_capture(ctx(app="Terminal"))
        assert decision.allowed is False
        assert decision.rule == "disabled"

    def test_every_protection_starts_on(self, isolated_db):
        rules = ambient.policy()
        for key in ("skip_private_windows", "skip_password_fields",
                    "skip_known_secret_apps", "skip_sensitive_titles",
                    "yield_to_models"):
            assert rules[key] is True, f"{key} should default to on"


class TestPrivateBrowsing:
    """The one nobody should have to configure.

    Opening a private window is the user saying, in the only vocabulary the
    browser gave them, that this is not to be kept.
    """

    @pytest.mark.parametrize("title", [
        "Gmail — Google Chrome (Incognito)",
        "Search — Mozilla Firefox (Private Browsing)",
        "Bing — Microsoft Edge [InPrivate]",
        "Apple — Safari Private Window",
    ])
    def test_private_windows_refused_with_no_configuration(self, on, title):
        decision = ambient.should_capture(ctx(app="Chrome", title=title))
        assert decision.allowed is False
        assert decision.rule == "private_window"

    def test_a_probe_that_knows_outright_is_believed(self, on):
        decision = ambient.should_capture(ctx(app="Chrome", title="Some page",
                                              private_window=True))
        assert decision.allowed is False
        assert decision.rule == "private_window"

    def test_an_ordinary_window_is_not_caught_by_this(self, on):
        assert ambient.should_capture(ctx(app="Chrome", title="Docs — Chrome")).allowed

    def test_the_user_can_turn_it_off_because_it_is_their_machine(self, on):
        ambient.set_policy({"skip_private_windows": False})
        assert ambient.should_capture(ctx(title="Gmail (Incognito)")).allowed


class TestSecrets:
    def test_a_focused_password_field_stops_capture(self, on):
        decision = ambient.should_capture(ctx(app="Chrome", secure_input=True))
        assert decision.allowed is False
        assert decision.rule == "secure_input"

    @pytest.mark.parametrize("app", ["1Password 8", "Bitwarden", "KeePassXC",
                                     "Keychain Access"])
    def test_credential_apps_are_never_captured(self, on, app):
        decision = ambient.should_capture(ctx(app=app, title="Vault"))
        assert decision.allowed is False
        assert decision.rule == "known_secret_app"

    def test_sensitive_titles_are_skipped_whatever_the_app(self, on):
        decision = ambient.should_capture(ctx(app="Chrome", title="Chase — Online Banking"))
        assert decision.allowed is False
        assert decision.rule == "sensitive_title"


class TestUserExclusions:
    def test_an_excluded_app_is_refused(self, on):
        ambient.add_exclusion("app", "Signal")
        decision = ambient.should_capture(ctx(app="Signal", title="Chats"))
        assert decision.allowed is False
        assert decision.rule == "excluded_app"

    def test_matching_ignores_case_and_version_suffixes(self, on):
        ambient.add_exclusion("app", "signal")
        assert not ambient.should_capture(ctx(app="Signal Desktop 7.2")).allowed

    def test_an_excluded_title_fragment_is_refused(self, on):
        ambient.add_exclusion("title", "Performance Review")
        decision = ambient.should_capture(
            ctx(app="Chrome", title="Q3 Performance Review — Docs"))
        assert decision.rule == "excluded_title"

    def test_an_excluded_url_is_refused(self, on):
        ambient.add_exclusion("url", "mail.proton.me")
        decision = ambient.should_capture(
            ctx(app="Chrome", title="Inbox", url="https://mail.proton.me/u/0"))
        assert decision.rule == "excluded_url"

    def test_exclusions_survive_and_do_not_duplicate(self, on):
        ambient.add_exclusion("app", "Signal")
        ambient.add_exclusion("app", "signal")
        assert ambient.policy()["excluded_apps"] == ["Signal"]

    def test_an_exclusion_can_be_removed(self, on):
        ambient.add_exclusion("app", "Signal")
        ambient.remove_exclusion("app", "Signal")
        assert ambient.policy()["excluded_apps"] == []
        assert ambient.should_capture(ctx(app="Signal")).allowed

    def test_an_unknown_kind_is_an_error_not_a_silent_no_op(self, on):
        # Silently accepting "window" would leave the user believing they had
        # excluded something.
        with pytest.raises(ValueError):
            ambient.add_exclusion("window", "Signal")

    def test_an_empty_exclusion_is_refused(self, on):
        # "" is a substring of every title.
        with pytest.raises(ValueError):
            ambient.add_exclusion("app", "   ")


class TestResources:
    def test_capture_yields_to_a_model_that_is_working(self, on):
        decision = ambient.should_capture(ctx(app="Terminal", model_busy=True))
        assert decision.allowed is False
        assert decision.rule == "model_busy"

    def test_yielding_can_be_turned_off(self, on):
        ambient.set_policy({"yield_to_models": False})
        assert ambient.should_capture(ctx(app="Terminal", model_busy=True)).allowed

    def test_a_low_battery_stops_capture(self, on):
        decision = ambient.should_capture(
            ctx(app="Terminal", on_battery=True, battery_percent=12))
        assert decision.allowed is False
        assert decision.rule == "battery_low"

    def test_plugged_in_at_the_same_charge_is_fine(self, on):
        assert ambient.should_capture(
            ctx(app="Terminal", on_battery=False, battery_percent=12)).allowed

    def test_tight_memory_stops_capture(self, on):
        decision = ambient.should_capture(ctx(app="Terminal", free_memory_gb=0.4))
        assert decision.rule == "memory_low"

    def test_tight_vram_stops_capture(self, on):
        decision = ambient.should_capture(ctx(app="Terminal", free_vram_gb=0.2))
        assert decision.rule == "vram_low"

    def test_a_busy_cpu_stops_capture(self, on):
        decision = ambient.should_capture(ctx(app="Terminal", cpu_percent=97))
        assert decision.rule == "cpu_busy"

    def test_unknown_resources_are_not_treated_as_a_refusal(self, on):
        # A machine that will not report its battery is not a machine on a
        # low battery, and the feature should still work there.
        assert ambient.should_capture(ctx(app="Terminal")).allowed


class TestCadence:
    def test_capture_is_slower_on_battery(self, isolated_db):
        rules = ambient.set_policy({"enabled": True, "interval_seconds": 8,
                                    "battery_interval_seconds": 30})
        assert ambient.next_interval(ctx(), rules) == 8
        assert ambient.next_interval(ctx(on_battery=True), rules) == 30

    def test_a_busy_machine_is_backed_off_from(self, isolated_db):
        rules = ambient.set_policy({"enabled": True, "interval_seconds": 8})
        assert ambient.next_interval(ctx(cpu_percent=75), rules) == 16

    def test_the_interval_cannot_be_set_to_a_hot_loop(self, isolated_db):
        assert ambient.set_policy({"interval_seconds": 0})["interval_seconds"] \
            == ambient.MIN_INTERVAL_SECONDS
        assert ambient.set_policy({"interval_seconds": 9999})["interval_seconds"] \
            == ambient.MAX_INTERVAL_SECONDS

    def test_an_allowed_decision_says_when_to_look_again(self, on):
        # A caller given no interval will spin.
        assert ambient.should_capture(ctx(app="Terminal")).retry_after > 0

    def test_every_refusal_carries_a_retry_delay(self, on):
        for context in (ctx(secure_input=True), ctx(app="1Password"),
                        ctx(model_busy=True), ctx(cpu_percent=99)):
            assert ambient.should_capture(context).retry_after > 0


class TestPausingAndSchedule:
    def test_a_pause_is_time_boxed(self, on):
        ambient.pause_for(30)
        decision = ambient.should_capture(ctx(app="Terminal"))
        assert decision.allowed is False
        assert decision.rule == "paused"

    def test_a_pause_expires_on_its_own(self, on):
        rules = ambient.pause_for(30)
        later = rules["paused_until"] + 1
        assert ambient.should_capture(ctx(app="Terminal"), now=later).allowed

    def test_resuming_clears_the_pause(self, on):
        ambient.pause_for(60)
        ambient.resume()
        assert ambient.should_capture(ctx(app="Terminal")).allowed

    def test_being_away_from_the_machine_stops_capture(self, on):
        decision = ambient.should_capture(ctx(app="Terminal", idle_seconds=600))
        assert decision.rule == "idle"

    def test_capture_hours_are_honoured(self, on):
        import datetime

        ambient.set_policy({"active_hours": [9, 17]})
        at_eight = datetime.datetime.now().replace(hour=8, minute=0).timestamp()
        at_noon = datetime.datetime.now().replace(hour=12, minute=0).timestamp()
        assert ambient.should_capture(ctx(app="Terminal"), now=at_eight).rule == "outside_hours"
        assert ambient.should_capture(ctx(app="Terminal"), now=at_noon).allowed

    def test_overnight_hours_wrap(self, on):
        import datetime

        ambient.set_policy({"active_hours": [22, 6]})
        at_two = datetime.datetime.now().replace(hour=2, minute=0).timestamp()
        assert ambient.should_capture(ctx(app="Terminal"), now=at_two).allowed


class TestOrdering:
    def test_privacy_wins_over_resources(self, on):
        # A private window on a healthy machine and a private window on a busy
        # one are both refusals, and the reason should be the private window.
        decision = ambient.should_capture(
            ctx(title="Gmail (Incognito)", cpu_percent=99))
        assert decision.rule == "private_window"

    def test_being_off_is_reported_before_anything_else(self, isolated_db):
        # "your battery is low" would be a confusing thing to say about a
        # feature the user never turned on.
        assert ambient.should_capture(ctx(secure_input=True)).rule == "disabled"


class TestProbes:
    def test_a_missing_probe_never_grants_permission(self, isolated_db, monkeypatch):
        monkeypatch.setattr(ambient, "_free_vram_gb", lambda: None)
        monkeypatch.setattr(ambient, "_model_busy", lambda: False)
        context = ambient.probe_resources()
        assert "free_vram_gb" not in context

    def test_a_broken_gpu_query_is_not_an_error(self, isolated_db):
        with patch("subprocess.run", side_effect=OSError("no nvidia-smi")):
            assert ambient._free_vram_gb() is None

    def test_an_unreachable_ollama_is_read_as_not_busy(self, isolated_db):
        # Failing closed here would mean capture never runs on a machine with
        # no Ollama at all.
        with patch("requests.get", side_effect=OSError("connection refused")):
            assert ambient._model_busy() is False


class TestStatus:
    def test_status_explains_itself(self, on, monkeypatch):
        monkeypatch.setattr(ambient, "probe_resources", lambda: {})
        state = ambient.status()
        assert state["policy"]["enabled"] is True
        assert "allowed" in state["decision"]
        # The panel needs the built-in list to show what is already covered,
        # so the user is not asked to type "1Password" themselves.
        assert "1password" in state["defaults"]["apps"]
        assert "incognito" in state["defaults"]["private_markers"]


class TestNoCaptureExists:
    def test_the_module_captures_nothing(self):
        """The governor ships before the thing it governs.

        If a capture function ever appears here, it must route through
        ``should_capture`` — this test is the reminder, and it is cheap.
        """
        assert not hasattr(ambient, "capture")
        assert not hasattr(ambient, "screenshot")


class TestAPI:
    def test_the_state_endpoint_returns_the_rules(self, client):
        body = client.get("/api/ambient").json()
        assert body["policy"]["skip_private_windows"] is True

    def test_the_policy_can_be_updated(self, client):
        body = client.put("/api/ambient/policy",
                          json={"policy": {"enabled": True, "interval_seconds": 20}}).json()
        assert body["policy"]["enabled"] is True
        assert body["policy"]["interval_seconds"] == 20

    def test_exclusions_round_trip(self, client):
        added = client.post("/api/ambient/exclusions",
                            json={"kind": "app", "value": "Signal"}).json()
        assert "Signal" in added["policy"]["excluded_apps"]
        removed = client.post("/api/ambient/exclusions/remove",
                              json={"kind": "app", "value": "Signal"}).json()
        assert removed["policy"]["excluded_apps"] == []

    def test_a_bad_exclusion_is_a_400_not_a_500(self, client):
        response = client.post("/api/ambient/exclusions",
                               json={"kind": "window", "value": "Signal"})
        assert response.status_code == 400

    def test_the_check_endpoint_demonstrates_a_refusal(self, client):
        # Being able to type "Chase — Chrome" and watch it be refused, before
        # trusting the feature with a real day, is the whole point.
        client.put("/api/ambient/policy", json={"policy": {"enabled": True}})
        body = client.post("/api/ambient/check",
                           json={"app": "Chrome", "title": "Chase — Online Banking"}).json()
        assert body["decision"]["allowed"] is False
        assert body["privacy"]["rule"] == "sensitive_title"

    def test_the_check_endpoint_separates_the_gates(self, client):
        client.put("/api/ambient/policy", json={"policy": {"enabled": True}})
        body = client.post("/api/ambient/check",
                           json={"app": "Terminal", "title": "zsh"}).json()
        for gate in ("privacy", "resources", "schedule"):
            assert gate in body

    def test_pause_and_resume(self, client):
        client.put("/api/ambient/policy", json={"policy": {"enabled": True}})
        paused = client.post("/api/ambient/pause", json={"minutes": 15}).json()
        assert paused["policy"]["paused_until"] > 0
        resumed = client.post("/api/ambient/resume").json()
        assert resumed["policy"]["paused_until"] == 0

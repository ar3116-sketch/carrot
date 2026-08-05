"""What ambient capture is allowed to see, and when it may run at all.

This module exists before the thing it governs, deliberately. A feature that
watches your screen every few seconds is one where the exclusions are not a
setting bolted on afterwards — they are the feature. Written the other way
round, the first version captures everything and the exclusions become a patch
on a system that has already recorded your banking session.

So there is no capture here. There is only the decision, and it is the only way
capture will ever be reachable.

**Two independent gates, both of which must open.**

*Privacy* asks whether this moment may be looked at. Default-deny in the ways
that matter: a private browsing window, a password field, a password manager,
anything the user blacklisted. Nobody should have to think of incognito — the
whole point of incognito is that you have already said what you want, in the
only language the browser gave you, and a tool that records it anyway has
misunderstood something basic.

*Resources* asks whether the machine can afford it. On a laptop already running
Ollama, an OCR pass every few seconds is not free: it competes for the same
VRAM, and on battery it is the difference between an afternoon and an hour and
a half. So capture yields to a model that is mid-generation, backs off on
battery, stops entirely when the battery is low, and refuses when memory is
tight — and it says which of those it is doing, because a feature that silently
stops looks identical to one that is broken.

The decision is a pure function of a context dictionary. The probes that fill
that dictionary are platform-specific and unreliable; the policy is neither,
and keeping them apart is what makes this testable at all.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import get_config, set_config


@dataclass
class Decision:
    """Whether capture may happen, and the reason either way.

    The reason is not decoration. "Paused: on battery below 30%" is the
    difference between a user who trusts the feature and one who assumes it
    broke and turns it off.
    """

    allowed: bool
    reason: str
    rule: str = ""
    # How long to wait before the next attempt, in seconds. A refusal that
    # carries no interval invites a caller to spin.
    retry_after: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed, "reason": self.reason,
            "rule": self.rule, "retry_after": self.retry_after,
        }


ALLOW = "allowed"


# ===== Privacy =====
#
# Shipped on by default, all of them. An exclusion you have to know to add is
# an exclusion that protects the people who already knew.

# Window/app names that should never be captured. Matched case-insensitively
# against the app name and the window title.
DEFAULT_EXCLUDED_APPS = (
    "1password", "bitwarden", "lastpass", "dashlane", "keepass", "keeper",
    "keychain access", "credential manager", "gnome-keyring", "seahorse",
    "authy", "google authenticator", "yubikey",
)

# Title fragments every major browser uses for a private window. This is the
# single most important entry in the file: a private window is the user
# telling you, in the only vocabulary the browser offers, not to keep this.
PRIVATE_WINDOW_MARKERS = (
    "incognito",            # Chrome, Edge, Brave, Opera
    "private browsing",     # Firefox, Safari
    "inprivate",            # Edge (older)
    "private window",       # Safari, Zen
    "guest",                # Chrome guest profile
)

# Title fragments that suggest something sensitive regardless of the app.
DEFAULT_EXCLUDED_TITLES = (
    "password", "passphrase", "secret key", "seed phrase", "recovery phrase",
    "2fa", "one-time code", "verification code", "bank", "banking",
    "online banking", "tax return", "medical record", "patient",
)

MIN_INTERVAL_SECONDS = 2.0
MAX_INTERVAL_SECONDS = 120.0


def policy() -> Dict[str, Any]:
    """The current rules. Everything defaults to the cautious setting."""
    stored = get_config().get("ambient_policy", {}) or {}
    return {
        "enabled": bool(stored.get("enabled", False)),
        "excluded_apps": list(stored.get("excluded_apps", [])),
        "excluded_titles": list(stored.get("excluded_titles", [])),
        "excluded_urls": list(stored.get("excluded_urls", [])),
        # These four are the built-in protections. They can be turned off —
        # it is the user's machine — but each is on until they do.
        "skip_private_windows": bool(stored.get("skip_private_windows", True)),
        "skip_password_fields": bool(stored.get("skip_password_fields", True)),
        "skip_known_secret_apps": bool(stored.get("skip_known_secret_apps", True)),
        "skip_sensitive_titles": bool(stored.get("skip_sensitive_titles", True)),
        # Resources.
        "pause_on_battery": bool(stored.get("pause_on_battery", False)),
        "battery_floor_percent": int(stored.get("battery_floor_percent", 30)),
        "min_free_memory_gb": float(stored.get("min_free_memory_gb", 2.0)),
        "min_free_vram_gb": float(stored.get("min_free_vram_gb", 1.5)),
        "yield_to_models": bool(stored.get("yield_to_models", True)),
        "max_cpu_percent": int(stored.get("max_cpu_percent", 80)),
        # Cadence.
        "interval_seconds": float(stored.get("interval_seconds", 8.0)),
        "battery_interval_seconds": float(stored.get("battery_interval_seconds", 30.0)),
        "skip_when_idle": bool(stored.get("skip_when_idle", True)),
        "idle_seconds": int(stored.get("idle_seconds", 120)),
        # Scheduling: only capture during these hours, if set. [start, end).
        "active_hours": list(stored.get("active_hours", [])),
        # A manual "not now", as a unix timestamp to resume at.
        "paused_until": float(stored.get("paused_until", 0)),
    }


def set_policy(values: Dict[str, Any]) -> Dict[str, Any]:
    current = policy()
    merged = {**current, **{k: v for k, v in (values or {}).items() if k in current}}
    merged["battery_floor_percent"] = max(0, min(int(merged["battery_floor_percent"]), 100))
    merged["interval_seconds"] = _clamp_interval(merged["interval_seconds"])
    merged["battery_interval_seconds"] = _clamp_interval(merged["battery_interval_seconds"])
    set_config("ambient_policy", merged)
    return merged


def _clamp_interval(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 8.0
    return max(MIN_INTERVAL_SECONDS, min(seconds, MAX_INTERVAL_SECONDS))


def pause_for(minutes: float) -> Dict[str, Any]:
    """A manual "not now". Deliberately time-boxed rather than a toggle.

    A pause that has to be un-paused is one people leave on, and then the
    feature is off without anyone deciding it should be.
    """
    return set_policy({"paused_until": time.time() + max(0.0, float(minutes)) * 60})


def resume() -> Dict[str, Any]:
    return set_policy({"paused_until": 0})


def add_exclusion(kind: str, value: str) -> Dict[str, Any]:
    key = {"app": "excluded_apps", "title": "excluded_titles", "url": "excluded_urls"}.get(kind)
    if not key:
        raise ValueError(f"unknown exclusion kind '{kind}' — expected app, title or url")
    text = (value or "").strip()
    if not text:
        raise ValueError("an exclusion needs something to match")
    current = policy()
    if text.lower() not in [v.lower() for v in current[key]]:
        current[key] = current[key] + [text]
    return set_policy(current)


def remove_exclusion(kind: str, value: str) -> Dict[str, Any]:
    key = {"app": "excluded_apps", "title": "excluded_titles", "url": "excluded_urls"}.get(kind)
    if not key:
        raise ValueError(f"unknown exclusion kind '{kind}'")
    current = policy()
    current[key] = [v for v in current[key] if v.lower() != (value or "").strip().lower()]
    return set_policy(current)


# ===== The decision =====

def _matches(haystack: str, needles) -> Optional[str]:
    text = (haystack or "").lower()
    if not text:
        return None
    for needle in needles:
        candidate = str(needle or "").strip().lower()
        if candidate and candidate in text:
            return str(needle)
    return None


def check_privacy(context: Dict[str, Any], rules: Optional[Dict[str, Any]] = None) -> Decision:
    """May this moment be looked at?

    ``context`` is whatever the platform probe managed to learn: ``app``,
    ``title``, ``url``, ``private_window``, ``secure_input``. Every field is
    optional, and a missing field is never treated as permission — an unknown
    window is not the same as a safe one, but neither is it evidence of
    wrongdoing, so only positive signals block.
    """
    rules = rules or policy()
    app = str(context.get("app") or "")
    title = str(context.get("title") or "")
    url = str(context.get("url") or "")
    haystack = f"{app} {title}"

    # A password field has focus. The strongest signal there is, and the one
    # the OS itself is willing to tell us about.
    if rules["skip_password_fields"] and context.get("secure_input"):
        return Decision(False, "a password field has focus", "secure_input", 2.0)

    # Private browsing. Either the probe told us outright, or the title says so.
    if rules["skip_private_windows"]:
        if context.get("private_window"):
            return Decision(False, "this is a private browsing window", "private_window", 2.0)
        marker = _matches(title, PRIVATE_WINDOW_MARKERS)
        if marker:
            return Decision(False, f"the window title says '{marker}'", "private_window", 2.0)

    if rules["skip_known_secret_apps"]:
        hit = _matches(haystack, DEFAULT_EXCLUDED_APPS)
        if hit:
            return Decision(False, f"{hit} is a credential app", "known_secret_app", 5.0)

    if rules["skip_sensitive_titles"]:
        hit = _matches(title, DEFAULT_EXCLUDED_TITLES)
        if hit:
            return Decision(False, f"the window title mentions '{hit}'", "sensitive_title", 2.0)

    hit = _matches(haystack, rules["excluded_apps"])
    if hit:
        return Decision(False, f"you excluded '{hit}'", "excluded_app", 5.0)

    hit = _matches(title, rules["excluded_titles"])
    if hit:
        return Decision(False, f"you excluded titles containing '{hit}'", "excluded_title", 2.0)

    hit = _matches(url, rules["excluded_urls"])
    if hit:
        return Decision(False, f"you excluded '{hit}'", "excluded_url", 2.0)

    return Decision(True, ALLOW)


def check_resources(context: Dict[str, Any], rules: Optional[Dict[str, Any]] = None) -> Decision:
    """Can the machine afford it right now?

    An OCR pass every few seconds on a laptop already running Ollama is not
    free. It competes for the same VRAM, and on battery it is the difference
    between an afternoon and ninety minutes.
    """
    rules = rules or policy()

    # A model mid-generation gets the GPU. The user is waiting on that answer;
    # they are not waiting on the screen index.
    if rules["yield_to_models"] and context.get("model_busy"):
        return Decision(False, "a model is generating — capture yields to it",
                        "model_busy", 5.0)

    battery = context.get("battery_percent")
    on_battery = context.get("on_battery")
    if on_battery and battery is not None:
        if battery <= rules["battery_floor_percent"]:
            return Decision(
                False,
                f"on battery at {int(battery)}%, below the {rules['battery_floor_percent']}% floor",
                "battery_low", 300.0,
            )
    if on_battery and rules["pause_on_battery"]:
        return Decision(False, "paused while on battery", "on_battery", 60.0)

    free_memory = context.get("free_memory_gb")
    if free_memory is not None and free_memory < rules["min_free_memory_gb"]:
        return Decision(
            False, f"only {free_memory:.1f}GB of memory free", "memory_low", 30.0)

    free_vram = context.get("free_vram_gb")
    if free_vram is not None and free_vram < rules["min_free_vram_gb"]:
        return Decision(
            False, f"only {free_vram:.1f}GB of VRAM free", "vram_low", 30.0)

    cpu = context.get("cpu_percent")
    if cpu is not None and cpu > rules["max_cpu_percent"]:
        return Decision(False, f"the machine is busy ({int(cpu)}% CPU)", "cpu_busy", 15.0)

    return Decision(True, ALLOW)


def check_schedule(context: Dict[str, Any], rules: Optional[Dict[str, Any]] = None,
                   now: Optional[float] = None) -> Decision:
    """Is this a moment the user said yes to?"""
    rules = rules or policy()
    moment = now if now is not None else time.time()

    if not rules["enabled"]:
        return Decision(False, "ambient capture is turned off", "disabled", 60.0)

    paused_until = rules["paused_until"]
    if paused_until and moment < paused_until:
        left = int((paused_until - moment) / 60) + 1
        return Decision(False, f"paused for another {left} minute(s)", "paused",
                        min(paused_until - moment, 60.0))

    if rules["skip_when_idle"]:
        idle = context.get("idle_seconds")
        if idle is not None and idle >= rules["idle_seconds"]:
            # Capturing a screensaver a thousand times is pure cost.
            return Decision(False, "you have been away from the machine", "idle", 30.0)

    hours = rules["active_hours"]
    if hours and len(hours) == 2:
        import datetime

        hour = datetime.datetime.fromtimestamp(moment).hour
        start, end = int(hours[0]), int(hours[1])
        inside = start <= hour < end if start <= end else (hour >= start or hour < end)
        if not inside:
            return Decision(False, f"outside your capture hours ({start}:00–{end}:00)",
                            "outside_hours", 300.0)

    return Decision(True, ALLOW)


def should_capture(context: Dict[str, Any], rules: Optional[Dict[str, Any]] = None,
                   now: Optional[float] = None) -> Decision:
    """The one gate. Everything must agree, and the first refusal is reported.

    Order matters for the message, not the outcome: schedule first because
    "it is off" is a more useful thing to be told than "your battery is low",
    then privacy, then resources.
    """
    rules = rules or policy()
    for check in (
        lambda: check_schedule(context, rules, now),
        lambda: check_privacy(context, rules),
        lambda: check_resources(context, rules),
    ):
        decision = check()
        if not decision.allowed:
            return decision
    return Decision(True, ALLOW, "", next_interval(context, rules))


def next_interval(context: Dict[str, Any], rules: Optional[Dict[str, Any]] = None) -> float:
    """How long to wait before looking again.

    Slower on battery, and slower still while something else is working. A
    fixed cadence is the thing that turns a background feature into the reason
    a fan is running.
    """
    rules = rules or policy()
    seconds = rules["interval_seconds"]
    if context.get("on_battery"):
        seconds = max(seconds, rules["battery_interval_seconds"])
    cpu = context.get("cpu_percent")
    if cpu is not None and cpu > 60:
        seconds *= 2
    return _clamp_interval(seconds)


# ===== Probes =====
#
# Everything above is a pure function of a dictionary. This is where the
# dictionary comes from, and it is deliberately the only part allowed to be
# unreliable: a probe that cannot answer returns nothing rather than guessing,
# and a missing field never counts as permission.

def probe_resources() -> Dict[str, Any]:
    """Whatever this machine will tell us about its own load."""
    context: Dict[str, Any] = {}
    try:
        import psutil

        memory = psutil.virtual_memory()
        context["free_memory_gb"] = round(memory.available / (1024 ** 3), 2)
        context["cpu_percent"] = psutil.cpu_percent(interval=None)
        battery = psutil.sensors_battery()
        if battery is not None:
            context["battery_percent"] = battery.percent
            context["on_battery"] = not battery.power_plugged
    except Exception:
        pass
    vram = _free_vram_gb()
    if vram is not None:
        context["free_vram_gb"] = vram
    context["model_busy"] = _model_busy()
    return context


def _free_vram_gb() -> Optional[float]:
    """Free GPU memory, if there is a GPU and it will say.

    nvidia-smi only, and only when it answers quickly. Guessing VRAM wrong in
    the permissive direction is how you get an out-of-memory error in the
    middle of someone's answer.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return None
        values = [int(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        return round(min(values) / 1024, 2) if values else None
    except Exception:
        return None


def _model_busy() -> bool:
    """Whether Ollama currently has a model resident in memory.

    `/api/ps` is the cheapest honest signal: a loaded model means the GPU is
    already committed, and adding an OCR pass on top of it is what makes the
    machine stutter. It is not a perfect "is generating right now", but the
    error is in the safe direction — yielding slightly too often costs a few
    seconds of index freshness, and yielding too rarely costs someone's answer.
    """
    try:
        import requests

        from .config import get_config

        host = get_config().get("ollama_host", "http://localhost:11434").rstrip("/")
        response = requests.get(f"{host}/api/ps", timeout=2)
        if response.status_code != 200:
            return False
        return bool((response.json() or {}).get("models"))
    except Exception:
        return False


def status() -> Dict[str, Any]:
    """Everything the settings panel needs: the rules, and what they say now."""
    rules = policy()
    context = probe_resources()
    decision = should_capture(context, rules)
    return {
        "policy": rules,
        "context": context,
        "decision": decision.as_dict(),
        "defaults": {
            "apps": list(DEFAULT_EXCLUDED_APPS),
            "private_markers": list(PRIVATE_WINDOW_MARKERS),
            "titles": list(DEFAULT_EXCLUDED_TITLES),
        },
    }

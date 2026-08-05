"""Semester planning: a syllabus photo in, a week you can actually live in out.

The ask behind this module is deceptively large. Someone hands Carrot a photo
of their class schedule, says "I want to go to the gym and eat three meals a
day", names their college, and expects a real week back. Everything hard about
that is in the gaps between those sentences:

* A schedule photo says ``MWF 10:00–10:50, Kemeny 007``. It does not say how
  long it takes to get from Kemeny to the dining hall, and a plan that puts
  lunch eleven minutes after a class ending across campus is not a plan.
* Nobody volunteers where they live, whether they have a meal plan, when they
  actually sleep, or that they work Thursday nights. A planner that does not
  *ask* produces a beautiful schedule for a person who does not exist.
* "Three meals a day" is a constraint with windows and a duration, not three
  events. So is the gym, which additionally cannot be jammed against a class
  because showering exists.

So this module is three things: an **intake** that knows what it does not know
and asks, a **campus model** that turns two building names into a number of
minutes, and a **scheduler** that places fixed commitments first and then fits
the flexible ones into what is genuinely left.

The scheduler is deliberately pure — dictionaries in, dictionaries out, no
model, no network, no clock. That is what makes it testable, and a planner
nobody can test is a planner nobody should trust with their week.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import get_config, set_config

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}
# How syllabi actually write days. "R" for Thursday and "U" for Sunday are the
# registrar conventions that trip up every naive parser.
DAY_CODES = {
    "M": "mon", "T": "tue", "W": "wed", "R": "thu", "F": "fri", "S": "sat", "U": "sun",
    "TH": "thu", "TU": "tue", "SU": "sun", "SA": "sat", "MO": "mon", "WE": "wed", "FR": "fri",
}

KIND_CLASS = "class"
KIND_MEAL = "meal"
KIND_GYM = "gym"
KIND_STUDY = "study"
KIND_COMMUTE = "commute"
KIND_WORK = "work"
KIND_SLEEP = "sleep"
KIND_FIXED = "fixed"

# Commitments that cannot move. Everything else is fitted around them.
FIXED_KINDS = frozenset({KIND_CLASS, KIND_WORK, KIND_FIXED})

# Movement speeds, metres per minute. A campus walk is not a stroll and is not
# a race; 80 m/min (~3 mph) is the figure campus planners use.
WALK_METRES_PER_MINUTE = 80.0
# Waiting for a shuttle is most of the cost of taking one, which is why a bus
# only wins over walking at real distance.
BUS_WAIT_MINUTES = 7.0
BUS_METRES_PER_MINUTE = 400.0
BUS_WORTH_IT_METRES = 1200.0
# Getting out of one room and into another, before any distance at all.
DOOR_TO_DOOR_MINUTES = 4.0


class PlannerError(ValueError):
    """Something the user needs to fix, phrased for them."""


# ===== Time helpers =====
#
# Minutes since midnight throughout. Wall-clock arithmetic with datetimes
# invites timezone bugs into a problem that has none: a class at 10:00 is at
# 10:00 wherever the laptop thinks it is.

TIME_PATTERN = re.compile(r"^\s*(\d{1,2})[:.]?(\d{2})?\s*([ap]\.?m\.?)?\s*$", re.I)


def to_minutes(value: Any) -> int:
    """Parse "10:00", "10:00 AM", "1400", 600 — all the shapes a syllabus uses."""
    if isinstance(value, (int, float)):
        return int(value)
    found = TIME_PATTERN.match(str(value or ""))
    if not found:
        raise PlannerError(f"could not read the time {value!r}")
    hour = int(found.group(1))
    minute = int(found.group(2) or 0)
    suffix = (found.group(3) or "").lower().replace(".", "")
    if suffix == "pm" and hour != 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise PlannerError(f"{value!r} is not a real time")
    return hour * 60 + minute


def to_clock(minutes: int) -> str:
    minutes = int(minutes) % (24 * 60)
    hour, minute = divmod(minutes, 60)
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:{minute:02d} {suffix}"


def parse_days(value: Any) -> List[str]:
    """"MWF", "TR", ["mon","wed"], "Mon/Wed" — all mean the same thing."""
    if isinstance(value, (list, tuple)):
        return [d for d in (str(v).strip().lower()[:3] for v in value) if d in DAYS]
    text = str(value or "").strip()
    if not text:
        return []
    # Long forms first, so "Thu" is not read as T + h + u.
    lowered = text.lower()
    named = [d for d in DAYS if d in lowered]
    if named:
        return [d for d in DAYS if d in named]
    # Only letter-scan something that actually looks like a registrar code.
    # Scanning prose finds "S" and "U" in "as announced" and confidently
    # reports Saturday and Sunday, which is worse than reporting nothing.
    upper = re.sub(r"[^A-Za-z]", "", text.upper())
    if not upper or len(upper) > 10 or set(upper) - set("MTWRFSUH"):
        return []

    found: List[str] = []
    index = 0
    while index < len(upper):
        pair = upper[index:index + 2]
        if pair in DAY_CODES:
            found.append(DAY_CODES[pair])
            index += 2
            continue
        single = upper[index]
        if single in DAY_CODES:
            found.append(DAY_CODES[single])
        index += 1
    return [d for d in DAYS if d in found]


def overlaps(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Touching is not overlapping: a class ending at 10:50 and one starting
    at 10:50 are back to back, which is a travel problem, not a clash."""
    return a["day"] == b["day"] and a["start"] < b["end"] and b["start"] < a["end"]


# ===== Intake =====
#
# The questions that must be answered before a week can be planned at all.
# Each carries *why* it is being asked, because a stranger demanding your dorm
# building deserves to explain itself.

INTAKE: Tuple[Dict[str, Any], ...] = (
    {"id": "school", "question": "Which college or university?", "kind": "text",
     "why": "Building names, walking distances and shuttle routes are all campus-specific.",
     "required": True},
    {"id": "home", "question": "Where do you live — dorm or building name, or off campus?",
     "kind": "text", "required": True,
     "why": "Every day starts and ends here, and it sets your first and last commute."},
    {"id": "wake", "question": "What time do you usually wake up?", "kind": "time",
     "default": "7:30 AM", "required": True,
     "why": "Nothing gets scheduled before this, including a 'convenient' 6am gym slot."},
    {"id": "sleep", "question": "What time do you go to bed?", "kind": "time",
     "default": "11:30 PM", "required": True,
     "why": "The plan stops here. A study block at 2am is a fantasy, not a plan."},
    {"id": "meals", "question": "How many meals a day, and where do you usually eat?",
     "kind": "text", "default": "3", "required": False,
     "why": "Meals need a window and a walk, not just a slot."},
    {"id": "dining", "question": "Do you have a meal plan, and which dining hall is nearest?",
     "kind": "text", "required": False,
     "why": "A dining hall across campus changes when lunch can physically happen."},
    {"id": "gym", "question": "How often do you want to train, and for how long?",
     "kind": "text", "default": "4 times a week, 75 minutes", "required": False,
     "why": "Frequency and duration decide whether it fits at all."},
    {"id": "gym_place", "question": "Which gym or athletic centre?", "kind": "text",
     "required": False,
     "why": "So the walk there is scheduled instead of discovered."},
    {"id": "gym_time", "question": "Do you prefer to train in the morning, afternoon or evening?",
     "kind": "choice", "options": ["morning", "afternoon", "evening", "no preference"],
     "default": "no preference", "required": False,
     "why": "A gym slot you will not actually use is worse than none."},
    {"id": "work", "question": "Any job, lab hours, practice or standing commitments?",
     "kind": "text", "required": False,
     "why": "These are immovable like classes, and planning around them after the "
            "fact means replanning everything."},
    {"id": "study", "question": "Roughly how many hours a week do you want to study?",
     "kind": "text", "default": "15", "required": False,
     "why": "So study is scheduled deliberately rather than being whatever is left."},
    {"id": "transport", "question": "Do you use the campus shuttle, a bike, or walk everywhere?",
     "kind": "choice", "options": ["walk", "shuttle", "bike", "drive"], "default": "walk",
     "required": False,
     "why": "It changes every travel estimate in the plan."},
)

REQUIRED_IDS = tuple(q["id"] for q in INTAKE if q.get("required"))


def missing_intake(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What still has to be asked. Answered-with-a-default counts as answered."""
    answers = (profile or {}).get("answers", {}) or {}
    return [
        question for question in INTAKE
        if question.get("required") and not str(answers.get(question["id"], "")).strip()
    ]


def intake_complete(profile: Dict[str, Any]) -> bool:
    return not missing_intake(profile)


def answer_value(profile: Dict[str, Any], key: str, fallback: Any = "") -> Any:
    answers = (profile or {}).get("answers", {}) or {}
    value = answers.get(key)
    if str(value or "").strip():
        return value
    for question in INTAKE:
        if question["id"] == key and question.get("default") is not None:
            return question["default"]
    return fallback


# ===== Campus model =====
#
# Two building names in, minutes out. Coordinates come from the user or from a
# model that knows the campus; either way they are cached, because asking a
# model the same question about the same quad forty times is absurd.

def campus(school: str = "") -> Dict[str, Any]:
    stored = get_config().get("planner_campus", {}) or {}
    if school:
        return stored.get(_campus_key(school), {"school": school, "buildings": {}, "routes": []})
    return stored


def _campus_key(school: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (school or "").strip().lower()).strip("-") or "campus"


def save_buildings(school: str, buildings: Dict[str, Any]) -> Dict[str, Any]:
    """Merge new building coordinates into what is already known."""
    stored = dict(get_config().get("planner_campus", {}) or {})
    key = _campus_key(school)
    entry = dict(stored.get(key) or {"school": school, "buildings": {}, "routes": []})
    known = dict(entry.get("buildings") or {})
    for name, place in (buildings or {}).items():
        if not isinstance(place, dict):
            continue
        try:
            known[_norm_place(name)] = {
                "name": place.get("name") or name,
                "lat": float(place["lat"]),
                "lon": float(place["lon"]),
                "aliases": [str(a) for a in place.get("aliases", [])],
            }
        except (KeyError, TypeError, ValueError):
            continue  # A building without coordinates is not a building.
    entry["buildings"] = known
    entry["school"] = school or entry.get("school", "")
    stored[key] = entry
    set_config("planner_campus", stored)
    return entry


def _norm_place(name: str) -> str:
    """Match "Kemeny Hall", "kemeny", "KEMENY HALL 007" to one key."""
    text = re.sub(r"\b(hall|building|bldg|center|centre|room|rm)\b", " ", (name or "").lower())
    text = re.sub(r"[^a-z ]+", " ", text)
    return " ".join(text.split())


def find_building(school: str, name: str) -> Optional[Dict[str, Any]]:
    entry = campus(school)
    known = entry.get("buildings") or {}
    key = _norm_place(name)
    if not key:
        return None
    if key in known:
        return known[key]
    for stored_key, place in known.items():
        if key in stored_key or stored_key in key:
            return place
        if any(_norm_place(alias) == key for alias in place.get("aliases", [])):
            return place
    return None


def haversine_metres(a: Dict[str, float], b: Dict[str, float]) -> float:
    radius = 6371000.0
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def travel_minutes(school: str, origin: str, destination: str,
                   transport: str = "walk") -> Dict[str, Any]:
    """How long it takes to get from one place to another, and how.

    Unknown buildings get a deliberately generous default rather than zero:
    assuming two unknown places are the same room is how a planner produces a
    schedule that cannot be walked.
    """
    if not origin or not destination or _norm_place(origin) == _norm_place(destination):
        return {"minutes": 0, "mode": "none", "known": True, "metres": 0}

    start = find_building(school, origin)
    end = find_building(school, destination)
    if not (start and end):
        return {
            "minutes": int(DOOR_TO_DOOR_MINUTES + 10),
            "mode": "unknown",
            "known": False,
            "metres": 0,
            "note": f"no coordinates for {origin if not start else destination}",
        }

    metres = haversine_metres(start, end)
    walk = DOOR_TO_DOOR_MINUTES + metres / WALK_METRES_PER_MINUTE
    best, mode = walk, "walk"
    if transport == "bike":
        best, mode = DOOR_TO_DOOR_MINUTES + metres / 250.0, "bike"
    elif transport in ("shuttle", "drive") and metres >= BUS_WORTH_IT_METRES:
        ride = DOOR_TO_DOOR_MINUTES + BUS_WAIT_MINUTES + metres / BUS_METRES_PER_MINUTE
        # Waiting for a shuttle you then beat on foot is the classic bad advice.
        if ride < walk:
            best, mode = ride, "shuttle"
    return {
        "minutes": int(math.ceil(best)), "mode": mode, "known": True,
        "metres": int(metres),
    }


# ===== Courses =====

def normalize_course(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One course row from a syllabus into one block per meeting day."""
    days = parse_days(raw.get("days"))
    if not days:
        raise PlannerError(f"no meeting days for {raw.get('code') or raw.get('title') or 'a course'}")
    try:
        start = to_minutes(raw.get("start"))
        end = to_minutes(raw.get("end"))
    except PlannerError:
        raise PlannerError(f"could not read the times for {raw.get('code') or 'a course'}")
    if end <= start:
        raise PlannerError(
            f"{raw.get('code') or 'a course'} ends before it starts "
            f"({to_clock(start)}–{to_clock(end)})"
        )
    title = raw.get("title") or raw.get("code") or "Class"
    return [{
        "id": uuid.uuid4().hex[:10],
        "kind": KIND_CLASS,
        "title": f"{raw['code']} — {title}" if raw.get("code") else title,
        "day": day,
        "start": start,
        "end": end,
        "place": raw.get("location") or raw.get("place") or "",
        "movable": False,
    } for day in days]


def courses_to_blocks(courses: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Expand every course, collecting problems rather than dying on the first."""
    blocks, problems = [], []
    for raw in courses or []:
        try:
            blocks.extend(normalize_course(raw))
        except PlannerError as exc:
            problems.append(str(exc))
    return blocks, problems


# ===== The scheduler =====

def _free_gaps(day_blocks: List[Dict[str, Any]], day_start: int, day_end: int) -> List[Tuple[int, int]]:
    """Everything not already committed on one day."""
    gaps, cursor = [], day_start
    for block in sorted(day_blocks, key=lambda b: b["start"]):
        if block["start"] > cursor:
            gaps.append((cursor, min(block["start"], day_end)))
        cursor = max(cursor, block["end"])
        if cursor >= day_end:
            break
    if cursor < day_end:
        gaps.append((cursor, day_end))
    return [(a, b) for a, b in gaps if b > a]


def _place_of(blocks: List[Dict[str, Any]], day: str, at: int, home: str) -> str:
    """Where the person physically is at a given minute."""
    for block in sorted(blocks, key=lambda b: b["start"]):
        if block["day"] == day and block["start"] <= at <= block["end"] and block.get("place"):
            return block["place"]
    previous = [
        b for b in blocks
        if b["day"] == day and b["end"] <= at and b.get("place")
    ]
    if previous:
        return max(previous, key=lambda b: b["end"])["place"]
    return home


def _fit(gap: Tuple[int, int], duration: int, travel_in: int, travel_out: int,
         window: Optional[Tuple[int, int]] = None) -> Optional[Tuple[int, int]]:
    """Can this activity fit in this gap, once travel is paid for at both ends?

    Both ends matter. A slot that gets you to the gym but not back to your
    next class is not a slot.
    """
    earliest = gap[0] + travel_in
    latest = gap[1] - travel_out - duration
    if window:
        earliest = max(earliest, window[0])
        latest = min(latest, window[1] - duration)
    if latest < earliest:
        return None
    return (earliest, earliest + duration)


DEFAULT_MEALS = (
    {"name": "Breakfast", "window": ("6:30 AM", "10:00 AM"), "minutes": 30},
    {"name": "Lunch", "window": ("11:00 AM", "2:30 PM"), "minutes": 40},
    {"name": "Dinner", "window": ("5:00 PM", "8:30 PM"), "minutes": 45},
)


def plan_week(profile: Dict[str, Any], courses: List[Dict[str, Any]],
              options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a week. Fixed things first, then meals, gym, and study in the gaps.

    The order is the whole design. Classes cannot move, so they anchor
    everything. Meals have windows and are non-negotiable, so they go next.
    The gym needs a big contiguous block and so competes badly for scraps, so
    it goes before study rather than after. Study fills what actually remains,
    which is honest about how much there is.
    """
    options = options or {}
    school = str(answer_value(profile, "school"))
    home = str(answer_value(profile, "home"))
    transport = str(answer_value(profile, "transport", "walk")).lower()
    day_start = to_minutes(answer_value(profile, "wake", "7:30 AM"))
    day_end = to_minutes(answer_value(profile, "sleep", "11:30 PM"))
    if day_end <= day_start:
        raise PlannerError("bedtime is before wake-up — check those two answers")

    blocks, problems = courses_to_blocks(courses)
    for extra in options.get("fixed", []) or []:
        try:
            blocks.extend(normalize_course({**extra, "code": extra.get("code", "")}))
        except PlannerError as exc:
            problems.append(str(exc))

    conflicts = find_conflicts(blocks)
    scheduled = list(blocks)
    notes: List[str] = []

    # --- Travel between consecutive fixed commitments ---
    commutes = _commutes(scheduled, school, home, transport)
    tight = [c for c in commutes if c["gap"] < c["minutes"]]
    for clash in tight:
        notes.append(
            f"{DAY_LABELS[clash['day']]}: only {clash['gap']} minutes between "
            f"{clash['from']} and {clash['to']}, which needs about "
            f"{clash['minutes']}. You will be late."
        )
    scheduled.extend(c["block"] for c in commutes if c["gap"] >= c["minutes"] and c["minutes"] > 0)

    # --- Meals ---
    meal_count = _int_from(answer_value(profile, "meals", "3"), 3)
    dining = str(answer_value(profile, "dining", "")) or home
    for day in DAYS:
        for meal in DEFAULT_MEALS[:max(0, min(meal_count, 3))]:
            placed = _place_flexible(
                scheduled, day, day_start, day_end, meal["minutes"], dining,
                school, home, transport,
                window=(to_minutes(meal["window"][0]), to_minutes(meal["window"][1])),
            )
            if placed:
                scheduled.append({**placed, "kind": KIND_MEAL, "title": meal["name"],
                                  "place": dining, "movable": True})
            else:
                notes.append(
                    f"{DAY_LABELS[day]}: no room for {meal['name'].lower()} in its "
                    f"usual window — your day is genuinely full there."
                )

    # --- Gym ---
    sessions, gym_minutes = _parse_gym(answer_value(profile, "gym", ""))
    gym_place = str(answer_value(profile, "gym_place", "")) or home
    preference = str(answer_value(profile, "gym_time", "no preference")).lower()
    placed_sessions = 0
    for day in _gym_days(sessions):
        if placed_sessions >= sessions:
            break
        placed = _place_flexible(
            scheduled, day, day_start, day_end, gym_minutes, gym_place,
            school, home, transport, window=_preference_window(preference),
        )
        if placed:
            scheduled.append({**placed, "kind": KIND_GYM, "title": "Gym",
                              "place": gym_place, "movable": True})
            placed_sessions += 1
    if placed_sessions < sessions:
        notes.append(
            f"Only {placed_sessions} of {sessions} gym sessions fit. Shortening "
            f"them or moving one to the weekend is the usual fix."
        )

    # --- Study ---
    study_target = _int_from(answer_value(profile, "study", "15"), 15) * 60
    study_placed = 0
    for day in DAYS:
        while study_placed < study_target:
            placed = _place_flexible(
                scheduled, day, day_start, day_end, 90, home, school, home, transport,
            )
            if not placed:
                break
            scheduled.append({**placed, "kind": KIND_STUDY, "title": "Study",
                              "place": home, "movable": True})
            study_placed += 90
    if study_placed < study_target:
        notes.append(
            f"Found {study_placed // 60} of the {study_target // 60} study hours you "
            f"wanted. The rest has to come from somewhere — fewer gym days, or a "
            f"later bedtime."
        )

    scheduled.sort(key=lambda b: (DAYS.index(b["day"]), b["start"]))
    return {
        "days": _by_day(scheduled),
        "blocks": scheduled,
        "conflicts": conflicts,
        "problems": problems,
        "notes": notes,
        "totals": _totals(scheduled),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _place_flexible(scheduled, day, day_start, day_end, duration, place,
                    school, home, transport, window=None):
    """Find the first slot on a day where this activity genuinely fits."""
    day_blocks = [b for b in scheduled if b["day"] == day]
    for gap in _free_gaps(day_blocks, day_start, day_end):
        origin = _place_of(scheduled, day, gap[0], home)
        after = _place_of(scheduled, day, gap[1], home)
        travel_in = travel_minutes(school, origin, place, transport)["minutes"]
        travel_out = travel_minutes(school, place, after, transport)["minutes"]
        slot = _fit(gap, duration, travel_in, travel_out, window)
        if slot:
            return {"id": uuid.uuid4().hex[:10], "day": day,
                    "start": slot[0], "end": slot[1],
                    "travel_in": travel_in, "travel_out": travel_out}
    return None


def _commutes(blocks, school, home, transport):
    """The walk between each pair of consecutive fixed commitments."""
    found = []
    for day in DAYS:
        fixed = sorted(
            [b for b in blocks if b["day"] == day and b["kind"] in FIXED_KINDS],
            key=lambda b: b["start"],
        )
        for first, second in zip(fixed, fixed[1:]):
            if not (first.get("place") and second.get("place")):
                continue
            trip = travel_minutes(school, first["place"], second["place"], transport)
            if trip["minutes"] <= 0:
                continue
            found.append({
                "day": day,
                "from": first["place"],
                "to": second["place"],
                "minutes": trip["minutes"],
                "mode": trip["mode"],
                "gap": second["start"] - first["end"],
                "block": {
                    "id": uuid.uuid4().hex[:10], "kind": KIND_COMMUTE,
                    "title": f"{trip['mode'].title()} to {second['place']}",
                    "day": day, "start": first["end"],
                    "end": first["end"] + trip["minutes"],
                    "place": second["place"], "movable": False,
                },
            })
    return found


def find_conflicts(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Two immovable things at once. Always worth surfacing, never auto-fixed."""
    clashes = []
    fixed = [b for b in blocks if b["kind"] in FIXED_KINDS]
    for index, first in enumerate(fixed):
        for second in fixed[index + 1:]:
            if overlaps(first, second):
                clashes.append({
                    "day": first["day"],
                    "a": first["title"],
                    "b": second["title"],
                    "when": f"{to_clock(max(first['start'], second['start']))}–"
                            f"{to_clock(min(first['end'], second['end']))}",
                })
    return clashes


def _by_day(blocks):
    return [{
        "day": day,
        "label": DAY_LABELS[day],
        "blocks": [
            {**b, "start_label": to_clock(b["start"]), "end_label": to_clock(b["end"])}
            for b in blocks if b["day"] == day
        ],
    } for day in DAYS]


def _totals(blocks):
    totals: Dict[str, int] = {}
    for block in blocks:
        totals[block["kind"]] = totals.get(block["kind"], 0) + (block["end"] - block["start"])
    return {kind: round(minutes / 60, 1) for kind, minutes in sorted(totals.items())}


def _int_from(value: Any, fallback: int) -> int:
    found = re.search(r"\d+", str(value or ""))
    return int(found.group()) if found else fallback


def _parse_gym(answer: str) -> Tuple[int, int]:
    """"4 times a week, 75 minutes" -> (4, 75). Missing parts get sane defaults."""
    text = str(answer or "")
    if not text.strip():
        return 0, 0
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    sessions = numbers[0] if numbers else 4
    minutes = numbers[1] if len(numbers) > 1 else 75
    # A "session" longer than a session is a misread — 4x90 not 4x490.
    if minutes > 240:
        minutes = 90
    return max(0, min(sessions, 7)), max(20, minutes)


def _gym_days(sessions: int) -> List[str]:
    """Spread sessions out rather than stacking them Monday to Thursday."""
    spread = {
        0: [], 1: ["wed"], 2: ["tue", "thu"], 3: ["mon", "wed", "fri"],
        4: ["mon", "tue", "thu", "fri"], 5: ["mon", "tue", "wed", "thu", "fri"],
        6: ["mon", "tue", "wed", "thu", "fri", "sat"], 7: list(DAYS),
    }
    return spread.get(sessions, list(DAYS))


def _preference_window(preference: str) -> Optional[Tuple[int, int]]:
    if preference.startswith("morning"):
        return (to_minutes("6:00 AM"), to_minutes("11:00 AM"))
    if preference.startswith("afternoon"):
        return (to_minutes("12:00 PM"), to_minutes("5:00 PM"))
    if preference.startswith("evening"):
        return (to_minutes("5:00 PM"), to_minutes("10:00 PM"))
    return None


# ===== Storage =====

def profile() -> Dict[str, Any]:
    stored = get_config().get("planner_profile", {}) or {}
    stored.setdefault("answers", {})
    stored.setdefault("courses", [])
    return stored


def save_answers(answers: Dict[str, Any]) -> Dict[str, Any]:
    current = profile()
    merged = {**(current.get("answers") or {}), **{k: v for k, v in (answers or {}).items()}}
    current["answers"] = merged
    set_config("planner_profile", current)
    return current


def save_courses(courses: List[Dict[str, Any]]) -> Dict[str, Any]:
    current = profile()
    current["courses"] = list(courses or [])
    set_config("planner_profile", current)
    return current


def save_plan(plan: Dict[str, Any]) -> None:
    set_config("planner_last_plan", plan)


def last_plan() -> Dict[str, Any]:
    return get_config().get("planner_last_plan", {}) or {}


# ===== Prompts for the model half =====
#
# Extraction and campus lookup are the two jobs a model does better than code.
# Both are asked for as strict JSON so the result can be validated rather than
# parsed hopefully.

# Reading a sentence into answers. Fourteen labelled boxes is a form, and
# nobody fills in a form to try something out. "I'm at Dartmouth, living in
# Russell Sage, gym 4x a week" answers four of them at once, and the ones it
# cannot answer are the only ones worth asking about.
UNDERSTAND_PROMPT = """The user described their situation. Pull out only what
they actually said. Return ONLY JSON:

{{"answers": {{"school": "", "home": "", "wake": "", "sleep": "", "meals": "",
              "dining": "", "gym": "", "gym_place": "", "gym_time": "",
              "work": "", "study": "", "transport": ""}}}}

Rules:
- Omit any field they did not mention. Do not guess, and do not fill a field
  from a general assumption about people like them — a plan built on an
  invented bedtime is a plan for someone else.
- Times as "7:30 AM". gym as "4 times a week, 75 minutes" if both are given.
- gym_time is one of morning, afternoon, evening, no preference.
- transport is one of walk, shuttle, bike, drive.

THEY SAID: {text}"""


EXTRACT_PROMPT = """Read this class schedule or syllabus and return ONLY a JSON object:

{"courses": [{"code": "", "title": "", "days": "", "start": "", "end": "",
              "location": "", "instructor": ""}]}

Rules:
- "days" uses registrar letters: M T W R F S U, where R is Thursday and U is Sunday.
- "start" and "end" are like "10:00 AM". Never a range in one field.
- "location" is the building and room exactly as written.
- A course meeting at two different times (lecture and lab) is TWO entries.
- Omit anything you cannot actually read. Do not guess a time.
Return the JSON and nothing else."""

CAMPUS_PROMPT = """For {school}, give approximate coordinates for these places:
{places}

Return ONLY JSON:
{{"buildings": {{"<name as given>": {{"name": "", "lat": 0.0, "lon": 0.0,
                                     "aliases": []}}}}}}

Use the real campus building if you know it. Omit any place you are not
reasonably confident about — a wrong coordinate produces a schedule that
cannot be walked, which is worse than an unknown one."""


def parse_json_block(text: str) -> Dict[str, Any]:
    """Pull a JSON object out of a model reply that may be wrapped in prose."""
    body = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        raise PlannerError("the model did not return any JSON")
    try:
        return json.loads(body[start:end + 1])
    except json.JSONDecodeError as exc:
        raise PlannerError(f"the model's JSON was malformed: {exc}")


def places_in(courses: List[Dict[str, Any]], profile_data: Dict[str, Any]) -> List[str]:
    """Every place a plan will need to measure distance between."""
    found = []
    for course in courses or []:
        place = course.get("location") or course.get("place") or ""
        if place:
            found.append(place)
    for key in ("home", "dining", "gym_place"):
        value = str(answer_value(profile_data, key, "")).strip()
        if value:
            found.append(value)
    # Preserve order, drop duplicates, and drop room numbers — a schedule needs
    # the distance between buildings, not between rooms in one building.
    seen, unique = set(), []
    for place in found:
        base = re.sub(r"\s*\b\d{1,4}[A-Za-z]?\b\s*$", "", place).strip() or place
        key = _norm_place(base)
        if key and key not in seen:
            seen.add(key)
            unique.append(base)
    return unique

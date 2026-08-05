"""Semester planning: a syllabus photo in, a week you can actually live in out.

The scheduler is pure, so it can be tested against the things that make a plan
wrong rather than merely ugly: lunch eleven minutes after a class across
campus, a gym slot with no time to get back, two classes at once passing
silently, "TR" read as Tuesday and Wednesday.
"""
import pytest

from carrot import planner


# A small real-shaped campus. Kemeny and Baker are close; the gym is a genuine
# walk away, which is what makes the travel tests mean something.
CAMPUS = {
    "Kemeny": {"name": "Kemeny Hall", "lat": 43.7036, "lon": -72.2884},
    "Baker": {"name": "Baker Library", "lat": 43.7052, "lon": -72.2886},
    "Alumni Gym": {"name": "Alumni Gym", "lat": 43.7010, "lon": -72.2865},
    "Foco": {"name": "Class of 1953 Commons", "lat": 43.7028, "lon": -72.2879},
    "Russell Sage": {"name": "Russell Sage Hall", "lat": 43.7043, "lon": -72.2900},
}


@pytest.fixture
def campus(isolated_db):
    planner.save_buildings("Dartmouth", CAMPUS)
    return True


def profile(**answers):
    base = {
        "school": "Dartmouth", "home": "Russell Sage", "wake": "7:30 AM",
        "sleep": "11:30 PM", "meals": "3", "dining": "Foco",
        "gym": "4 times a week, 75 minutes", "gym_place": "Alumni Gym",
        "gym_time": "no preference", "study": "15", "transport": "walk",
    }
    base.update(answers)
    return {"answers": base, "courses": []}


COURSES = [
    {"code": "COSC 30", "title": "Discrete Math", "days": "MWF",
     "start": "10:00 AM", "end": "11:05 AM", "location": "Kemeny 007"},
    {"code": "ENGS 21", "title": "Design", "days": "TR",
     "start": "2:00 PM", "end": "3:50 PM", "location": "Baker 105"},
]


# ===== Reading a schedule the way a registrar writes one =====

class TestTimeParsing:
    @pytest.mark.parametrize("value,expected", [
        ("10:00 AM", 600), ("10:00", 600), ("2:00 PM", 840), ("14:00", 840),
        ("12:00 AM", 0), ("12:30 PM", 750), ("9:05am", 545), ("11:59 PM", 1439),
    ])
    def test_every_shape_a_syllabus_uses(self, value, expected):
        assert planner.to_minutes(value) == expected

    def test_an_unreadable_time_is_refused_not_guessed(self):
        with pytest.raises(planner.PlannerError):
            planner.to_minutes("sometime after lunch")

    def test_an_impossible_time_is_refused(self):
        with pytest.raises(planner.PlannerError):
            planner.to_minutes("25:00")

    def test_clock_formatting_round_trips(self):
        assert planner.to_clock(planner.to_minutes("2:05 PM")) == "2:05 PM"

    def test_midnight_reads_as_twelve(self):
        assert planner.to_clock(0) == "12:00 AM"


class TestDayParsing:
    def test_mwf(self):
        assert planner.parse_days("MWF") == ["mon", "wed", "fri"]

    def test_r_is_thursday_not_a_mistake(self):
        # The single most common parser bug with US registrar codes.
        assert planner.parse_days("TR") == ["tue", "thu"]

    def test_u_is_sunday(self):
        assert planner.parse_days("U") == ["sun"]

    def test_two_letter_forms(self):
        assert planner.parse_days("TuTh") == ["tue", "thu"]

    def test_long_names(self):
        assert planner.parse_days("Monday/Wednesday") == ["mon", "wed"]

    def test_a_list_works_too(self):
        assert planner.parse_days(["Mon", "Fri"]) == ["mon", "fri"]

    def test_days_come_back_in_week_order(self):
        assert planner.parse_days("FMW") == ["mon", "wed", "fri"]

    def test_nothing_readable_is_empty(self):
        assert planner.parse_days("as announced") == []


class TestCourseNormalization:
    def test_one_course_becomes_one_block_per_day(self):
        blocks = planner.normalize_course(COURSES[0])
        assert len(blocks) == 3
        assert {b["day"] for b in blocks} == {"mon", "wed", "fri"}

    def test_the_room_is_carried_through(self):
        assert planner.normalize_course(COURSES[0])[0]["place"] == "Kemeny 007"

    def test_a_course_ending_before_it_starts_is_refused(self):
        with pytest.raises(planner.PlannerError) as caught:
            planner.normalize_course({"code": "X", "days": "M",
                                      "start": "2:00 PM", "end": "10:00 AM"})
        assert "ends before it starts" in str(caught.value)

    def test_a_course_with_no_days_is_refused(self):
        with pytest.raises(planner.PlannerError):
            planner.normalize_course({"code": "X", "days": "", "start": "9", "end": "10"})

    def test_one_bad_row_does_not_lose_the_good_ones(self):
        # An unreadable line on a photo should cost that line, not the term.
        blocks, problems = planner.courses_to_blocks(
            COURSES + [{"code": "BAD", "days": "M", "start": "?", "end": "?"}])
        assert len(blocks) == 5 and len(problems) == 1


# ===== Getting across campus =====

class TestTravel:
    def test_the_same_building_costs_nothing(self, campus):
        assert planner.travel_minutes("Dartmouth", "Kemeny 007", "Kemeny 105")["minutes"] == 0

    def test_a_real_walk_costs_real_minutes(self, campus):
        trip = planner.travel_minutes("Dartmouth", "Kemeny", "Alumni Gym")
        assert trip["minutes"] > 4 and trip["known"] is True

    def test_a_room_number_does_not_defeat_the_lookup(self, campus):
        # "Kemeny 007" and "Kemeny Hall" are the same place.
        assert planner.travel_minutes("Dartmouth", "Kemeny 007", "Baker 105")["known"] is True

    def test_an_unknown_building_gets_a_generous_guess_not_zero(self, campus):
        # Assuming two unknown places are the same room is how you produce a
        # schedule that cannot physically be walked.
        trip = planner.travel_minutes("Dartmouth", "Kemeny", "Somewhere Else")
        assert trip["known"] is False and trip["minutes"] > 10

    def test_the_shuttle_only_wins_at_distance(self, campus):
        # Waiting seven minutes for a bus you would beat on foot is bad advice.
        near = planner.travel_minutes("Dartmouth", "Kemeny", "Baker", "shuttle")
        assert near["mode"] == "walk"

    def test_the_shuttle_wins_when_it_should(self, isolated_db):
        planner.save_buildings("Far", {
            "A": {"name": "A", "lat": 43.70, "lon": -72.29},
            "B": {"name": "B", "lat": 43.74, "lon": -72.29},
        })
        assert planner.travel_minutes("Far", "A", "B", "shuttle")["mode"] == "shuttle"

    def test_a_bike_beats_a_walk(self, campus):
        walk = planner.travel_minutes("Dartmouth", "Kemeny", "Alumni Gym", "walk")
        bike = planner.travel_minutes("Dartmouth", "Kemeny", "Alumni Gym", "bike")
        assert bike["minutes"] <= walk["minutes"]

    def test_an_alias_resolves(self, isolated_db):
        planner.save_buildings("X", {
            "Class of 1953 Commons": {"name": "Foco", "lat": 43.70, "lon": -72.28,
                                      "aliases": ["Foco"]},
        })
        assert planner.find_building("X", "Foco") is not None

    def test_a_building_without_coordinates_is_not_stored(self, isolated_db):
        planner.save_buildings("X", {"Nowhere": {"name": "Nowhere"}})
        assert planner.find_building("X", "Nowhere") is None


# ===== The intake =====

class TestIntake:
    def test_an_empty_profile_knows_what_it_must_ask(self, isolated_db):
        missing = planner.missing_intake({"answers": {}})
        assert [q["id"] for q in missing][:2] == ["school", "home"]

    def test_every_required_question_explains_itself(self):
        # A stranger demanding your dorm building owes you a reason.
        assert all(q["why"] for q in planner.INTAKE)

    def test_where_you_live_is_required(self):
        # It sets the first and last commute of every single day.
        assert "home" in planner.REQUIRED_IDS

    def test_a_complete_profile_asks_nothing(self, isolated_db):
        assert planner.intake_complete(profile()) is True

    def test_an_optional_answer_falls_back_to_its_default(self, isolated_db):
        assert planner.answer_value({"answers": {}}, "study") == "15"

    def test_answers_merge_rather_than_replace(self, isolated_db):
        planner.save_answers({"school": "Dartmouth"})
        planner.save_answers({"home": "Russell Sage"})
        assert planner.profile()["answers"]["school"] == "Dartmouth"


# ===== The week =====

class TestPlanWeek:
    def test_classes_land_where_the_syllabus_says(self, campus):
        plan = planner.plan_week(profile(), COURSES)
        monday = [b for b in plan["blocks"] if b["day"] == "mon" and b["kind"] == "class"]
        assert monday[0]["start"] == planner.to_minutes("10:00 AM")

    def test_three_meals_a_day_actually_appear(self, campus):
        plan = planner.plan_week(profile(), COURSES)
        meals = [b for b in plan["blocks"] if b["kind"] == "meal" and b["day"] == "mon"]
        assert len(meals) == 3

    def test_meals_stay_inside_their_windows(self, campus):
        plan = planner.plan_week(profile(), COURSES)
        lunches = [b for b in plan["blocks"] if b["title"] == "Lunch"]
        assert all(planner.to_minutes("11:00 AM") <= b["start"] <= planner.to_minutes("2:30 PM")
                   for b in lunches)

    def test_nothing_is_scheduled_before_you_wake_up(self, campus):
        plan = planner.plan_week(profile(wake="9:00 AM"), COURSES)
        flexible = [b for b in plan["blocks"] if b["movable"]]
        assert all(b["start"] >= planner.to_minutes("9:00 AM") for b in flexible)

    def test_nothing_is_scheduled_after_bedtime(self, campus):
        plan = planner.plan_week(profile(sleep="10:00 PM"), COURSES)
        flexible = [b for b in plan["blocks"] if b["movable"]]
        assert all(b["end"] <= planner.to_minutes("10:00 PM") for b in flexible)

    def test_nothing_flexible_overlaps_a_class(self, campus):
        plan = planner.plan_week(profile(), COURSES)
        classes = [b for b in plan["blocks"] if b["kind"] == "class"]
        flexible = [b for b in plan["blocks"] if b["movable"]]
        assert not any(planner.overlaps(a, b) for a in classes for b in flexible)

    def test_no_two_scheduled_things_overlap_at_all(self, campus):
        plan = planner.plan_week(profile(), COURSES)
        blocks = plan["blocks"]
        clashes = [
            (a["title"], b["title"]) for i, a in enumerate(blocks) for b in blocks[i + 1:]
            if planner.overlaps(a, b)
        ]
        assert clashes == []

    def test_travel_time_is_paid_for_before_a_meal(self, campus):
        # The whole point: lunch eleven minutes after a class across campus is
        # not lunch.
        plan = planner.plan_week(profile(), COURSES)
        lunch = next(b for b in plan["blocks"] if b["title"] == "Lunch" and b["day"] == "mon")
        classes = [b for b in plan["blocks"]
                   if b["day"] == "mon" and b["kind"] == "class" and b["end"] <= lunch["start"]]
        if classes:
            previous = max(classes, key=lambda b: b["end"])
            walk = planner.travel_minutes("Dartmouth", previous["place"], "Foco")["minutes"]
            assert lunch["start"] - previous["end"] >= walk

    def test_the_gym_gets_the_sessions_it_was_asked_for(self, campus):
        plan = planner.plan_week(profile(), COURSES)
        assert len([b for b in plan["blocks"] if b["kind"] == "gym"]) == 4

    def test_gym_sessions_are_spread_across_the_week(self, campus):
        plan = planner.plan_week(profile(), COURSES)
        days = {b["day"] for b in plan["blocks"] if b["kind"] == "gym"}
        assert len(days) == 4

    def test_a_morning_preference_is_honoured(self, campus):
        plan = planner.plan_week(profile(gym_time="morning"), COURSES)
        gym = [b for b in plan["blocks"] if b["kind"] == "gym"]
        assert gym and all(b["start"] < planner.to_minutes("11:00 AM") for b in gym)

    def test_an_impossible_gym_target_is_reported_not_faked(self, campus):
        # A three-hour waking day with classes in it has no room for seven
        # sessions. Saying so beats inventing slots that do not exist.
        plan = planner.plan_week(profile(gym="7 times a week, 90 minutes",
                                          wake="9:00 AM", sleep="12:00 PM"), COURSES)
        placed = len([b for b in plan["blocks"] if b["kind"] == "gym"])
        assert placed < 7 and any("gym session" in n for n in plan["notes"])

    def test_study_time_is_scheduled_not_left_over(self, campus):
        plan = planner.plan_week(profile(), COURSES)
        assert any(b["kind"] == "study" for b in plan["blocks"])

    def test_a_commute_between_back_to_back_classes_is_shown(self, campus):
        back_to_back = [
            {"code": "A", "days": "M", "start": "10:00 AM", "end": "11:00 AM",
             "location": "Kemeny"},
            {"code": "B", "days": "M", "start": "11:30 AM", "end": "12:30 PM",
             "location": "Alumni Gym"},
        ]
        plan = planner.plan_week(profile(), back_to_back)
        assert any(b["kind"] == "commute" for b in plan["blocks"])

    def test_an_unwalkable_gap_is_called_out(self, campus):
        # Five minutes to cross campus is a warning, not something to silently
        # schedule around.
        impossible = [
            {"code": "A", "days": "M", "start": "10:00 AM", "end": "11:00 AM",
             "location": "Kemeny"},
            {"code": "B", "days": "M", "start": "11:02 AM", "end": "12:00 PM",
             "location": "Alumni Gym"},
        ]
        plan = planner.plan_week(profile(), impossible)
        assert any("late" in note for note in plan["notes"])

    def test_two_classes_at_once_are_surfaced(self, campus):
        clashing = [
            {"code": "A", "days": "M", "start": "10:00 AM", "end": "11:00 AM",
             "location": "Kemeny"},
            {"code": "B", "days": "M", "start": "10:30 AM", "end": "11:30 AM",
             "location": "Baker"},
        ]
        plan = planner.plan_week(profile(), clashing)
        assert plan["conflicts"] and plan["conflicts"][0]["day"] == "mon"

    def test_back_to_back_is_not_a_conflict(self, campus):
        # Touching is a travel problem, not a clash — conflating them would
        # flag half of every real timetable.
        touching = [
            {"code": "A", "days": "M", "start": "10:00 AM", "end": "11:00 AM",
             "location": "Kemeny"},
            {"code": "B", "days": "M", "start": "11:00 AM", "end": "12:00 PM",
             "location": "Kemeny"},
        ]
        assert planner.plan_week(profile(), touching)["conflicts"] == []

    def test_a_job_is_treated_as_immovable(self, campus):
        plan = planner.plan_week(profile(), COURSES, {"fixed": [
            {"title": "Dining hall shift", "days": "R", "start": "5:00 PM",
             "end": "9:00 PM", "location": "Foco"},
        ]})
        shift = [b for b in plan["blocks"] if b["title"] == "Dining hall shift"]
        assert shift and shift[0]["movable"] is False
        thursday = [b for b in plan["blocks"] if b["day"] == "thu" and b["movable"]]
        assert not any(planner.overlaps(shift[0], b) for b in thursday)

    def test_bedtime_before_wake_up_is_refused(self, campus):
        with pytest.raises(planner.PlannerError) as caught:
            planner.plan_week(profile(wake="11:00 PM", sleep="7:00 AM"), COURSES)
        assert "bedtime" in str(caught.value)

    def test_the_plan_reports_where_the_week_goes(self, campus):
        totals = planner.plan_week(profile(), COURSES)["totals"]
        assert totals["class"] > 0 and totals["meal"] > 0

    def test_days_come_back_labelled_for_display(self, campus):
        plan = planner.plan_week(profile(), COURSES)
        monday = plan["days"][0]
        assert monday["label"] == "Monday"
        assert all("start_label" in b for b in monday["blocks"])


class TestGymAnswerParsing:
    @pytest.mark.parametrize("answer,expected", [
        ("4 times a week, 75 minutes", (4, 75)),
        ("3x a week for 60 min", (3, 60)),
        ("5", (5, 75)),
        ("", (0, 0)),
    ])
    def test_a_sentence_becomes_numbers(self, answer, expected):
        assert planner._parse_gym(answer) == expected

    def test_an_absurd_duration_is_corrected(self):
        # "4 times a week, 490" is a misread, not a four-hour lift.
        assert planner._parse_gym("4 times a week, 490 minutes")[1] == 90

    def test_more_than_seven_days_a_week_is_capped(self):
        assert planner._parse_gym("12 times a week, 60 minutes")[0] == 7


# ===== Model output, validated rather than believed =====

class TestModelOutput:
    def test_json_inside_a_fence_is_found(self):
        assert planner.parse_json_block('```json\n{"courses": []}\n```') == {"courses": []}

    def test_json_wrapped_in_prose_is_found(self):
        text = 'Sure! Here you go:\n{"courses": [{"code": "X"}]}\nHope that helps.'
        assert planner.parse_json_block(text)["courses"][0]["code"] == "X"

    def test_no_json_at_all_is_an_error(self):
        with pytest.raises(planner.PlannerError):
            planner.parse_json_block("I could not read the image.")

    def test_malformed_json_is_an_error(self):
        with pytest.raises(planner.PlannerError) as caught:
            planner.parse_json_block('{"courses": [oops}')
        assert "malformed" in str(caught.value)

    def test_the_extraction_prompt_spells_out_the_thursday_trap(self):
        assert "R is Thursday" in planner.EXTRACT_PROMPT

    def test_the_campus_prompt_prefers_silence_to_a_wrong_coordinate(self):
        assert "Omit any place you are not" in planner.CAMPUS_PROMPT


class TestPlacesNeeded:
    def test_every_place_a_plan_needs_is_collected(self, isolated_db):
        places = planner.places_in(COURSES, profile())
        assert "Kemeny" in places and "Alumni Gym" in places

    def test_room_numbers_are_dropped(self, isolated_db):
        # The plan needs the distance between buildings, not between rooms.
        assert "Kemeny 007" not in planner.places_in(COURSES, profile())

    def test_duplicates_are_collapsed(self, isolated_db):
        doubled = COURSES + [{**COURSES[0], "days": "T", "location": "Kemeny 105"}]
        assert planner.places_in(doubled, profile()).count("Kemeny") == 1


# ===== Endpoints =====

class TestPlannerEndpoints:
    def test_state_says_what_is_still_missing(self, client):
        body = client.get("/api/planner/state").json()
        assert body["ready"] is False
        assert body["next_question"]["id"] == "school"

    def test_answers_can_be_saved_one_at_a_time(self, client):
        body = client.put("/api/planner/answers", json={"answers": {"school": "Dartmouth"}})
        assert body.json()["next_question"]["id"] == "home"

    def test_courses_round_trip_and_report_problems(self, client):
        body = client.put("/api/planner/courses", json={"courses": COURSES})
        assert body.json()["meetings"] == 5 and body.json()["problems"] == []

    def test_planning_without_the_intake_refuses_with_the_question(self, client):
        client.put("/api/planner/courses", json={"courses": COURSES})
        body = client.post("/api/planner/plan", json={})
        assert body.status_code == 400 and "Which college" in body.json()["detail"]

    def test_planning_without_courses_says_so(self, client):
        client.put("/api/planner/answers", json={"answers": profile()["answers"]})
        body = client.post("/api/planner/plan", json={})
        assert body.status_code == 400 and "classes" in body.json()["detail"]

    def test_a_full_run_produces_a_week(self, client):
        planner.save_buildings("Dartmouth", CAMPUS)
        client.put("/api/planner/answers", json={"answers": profile()["answers"]})
        client.put("/api/planner/courses", json={"courses": COURSES})
        body = client.post("/api/planner/plan", json={})
        assert body.status_code == 200
        assert len(body.json()["days"]) == 7

    def test_the_plan_is_remembered(self, client):
        planner.save_buildings("Dartmouth", CAMPUS)
        client.put("/api/planner/answers", json={"answers": profile()["answers"]})
        client.put("/api/planner/courses", json={"courses": COURSES})
        client.post("/api/planner/plan", json={})
        assert client.get("/api/planner/plan").json()["blocks"]

    def test_a_syllabus_with_neither_image_nor_text_is_a_400(self, client):
        assert client.post("/api/planner/syllabus", json={}).status_code == 400

    def test_campus_lookup_without_a_school_is_a_400(self, client):
        assert client.post("/api/planner/campus/lookup").status_code == 400

    def test_travel_can_be_queried_directly(self, client):
        planner.save_buildings("Dartmouth", CAMPUS)
        client.put("/api/planner/answers", json={"answers": {"school": "Dartmouth"}})
        body = client.get("/api/planner/travel?origin=Kemeny&destination=Baker")
        assert body.json()["known"] is True

    def test_buildings_can_be_corrected_by_hand(self, client):
        body = client.put("/api/planner/campus", json={
            "school": "Dartmouth",
            "buildings": {"Kemeny": {"name": "Kemeny Hall", "lat": 43.7, "lon": -72.28}},
        })
        assert "kemeny" in body.json()["buildings"]

    def test_the_endpoints_need_a_session_token(self, unauthenticated_client):
        assert unauthenticated_client.get("/api/planner/state").status_code == 401


class TestPlannerTabIsReachable:
    """The engine shipped with no tab, so none of it could be used."""

    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath("carrot", "web", *parts).read_text()

    def test_there_is_a_nav_entry(self):
        assert 'data-tab="planner"' in self.read("index.html")

    def test_the_view_exists(self):
        assert 'id="view-planner"' in self.read("index.html")

    def test_the_tab_loader_is_registered(self):
        # A view with no loader renders empty forever.
        assert "planner: loadPlanner," in self.read("js", "app.js")

    def test_the_script_is_included(self):
        assert "/js/planner.js" in self.read("index.html")

    def test_all_four_steps_are_present(self):
        html = self.read("index.html")
        for step in ("planner-intake", "planner-courses", "planner-buildings", "planner-week"):
            assert f'id="{step}"' in html

    def test_each_question_shows_why_it_is_asked(self):
        # A stranger asking which dorm you live in owes you a reason.
        assert "intake-why" in self.read("js", "planner.js")

    def test_a_photo_can_be_dropped_or_pasted(self):
        js = self.read("js", "planner.js")
        assert "dataTransfer?.files" in js and "clipboardData?.files" in js

    def test_extracted_courses_are_editable_before_saving(self):
        # A misread room number is easier to fix here than on the walk there.
        js = self.read("js", "planner.js")
        assert "courses[index][field] = input.value.trim()" in js
        assert "These look right" in js

    def test_conflicts_and_warnings_render_above_the_grid(self):
        js = self.read("js", "planner.js")
        assert "planner-problem" in js and "plan.conflicts" in js and "plan.notes" in js

    def test_unknown_buildings_are_disclosed(self):
        # Those are the walks the planner has to guess at.
        assert "generous guess rather than a real number" in self.read("js", "planner.js")

    def test_every_css_token_the_planner_uses_is_defined(self):
        import re

        css = self.read("css", "style.css")
        block = css.split("/* ===== Semester planner =====")[1]
        used = set(re.findall(r"var\((--[a-z0-9-]+)", block))
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
        assert used <= defined, f"undefined CSS tokens: {sorted(used - defined)}"

    def test_every_block_kind_the_planner_emits_has_a_colour(self):
        from carrot import planner

        css = self.read("css", "style.css")
        for kind in (planner.KIND_CLASS, planner.KIND_MEAL, planner.KIND_GYM,
                     planner.KIND_STUDY, planner.KIND_COMMUTE, planner.KIND_WORK):
            assert f".day-block.kind-{kind}" in css, kind


class TestPlannerFromChat:
    """The tab exists because a course table and a week grid need real estate.
    But nobody opens a tab they were never told about, and "plan my semester"
    is a thing people say out loud — so the same engine answers from chat."""

    def call(self, **kwargs):
        import json

        from carrot import agent_tools

        return json.loads(agent_tools._tool_plan_semester(**kwargs))

    def test_it_reports_the_next_question_rather_than_guessing(self, isolated_db):
        body = self.call(action="state")
        assert body["status"] == "INCOMPLETE"
        assert body["next_question"]["id"] == "school"
        assert body["next_question"]["why"]

    def test_an_answer_can_be_recorded_from_the_conversation(self, isolated_db):
        body = self.call(action="answer", answers={"school": "Dartmouth"})
        assert planner.profile()["answers"]["school"] == "Dartmouth"
        assert body["next_question"]["id"] == "home"

    def test_planning_too_early_asks_instead_of_failing(self, isolated_db):
        body = self.call(action="plan")
        assert body["status"] == "NEEDS_ANSWERS" and body["ask"]

    def test_planning_without_classes_points_at_the_tab(self, isolated_db):
        # Reading a schedule photo needs the image, which a tool call has not got.
        planner.save_answers(profile()["answers"])
        body = self.call(action="plan")
        assert body["status"] == "NEEDS_COURSES" and "Planner tab" in body["message"]

    def test_a_complete_profile_produces_a_week(self, campus):
        planner.save_answers(profile()["answers"])
        planner.save_courses(COURSES)
        body = self.call(action="plan")
        assert body["status"] == "PLANNED"
        assert len(body["days"]) == 7
        assert any("Class" in b or "COSC" in b for d in body["days"] for b in d["blocks"])

    def test_the_chat_plan_and_the_tab_share_one_state(self, campus):
        # Two doors into one room, not two features.
        planner.save_answers(profile()["answers"])
        planner.save_courses(COURSES)
        self.call(action="plan")
        assert planner.last_plan()["blocks"]

    def test_the_tool_forbids_inventing_answers(self):
        from carrot import agent_tools

        description = agent_tools.TOOLS["plan_semester"]["description"]
        assert "Never invent an answer" in description


class TestOnboardingRevision:
    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath("carrot", "web", *parts).read_text()

    def test_using_your_own_subscription_is_offered(self):
        assert "I want to use my own AI subscription" in self.read("index.html")

    def test_the_subscription_step_exists(self):
        assert 'data-step="subscription"' in self.read("index.html")

    def test_it_states_what_it_does_not_do(self):
        # The claim is only worth making if it is specific.
        html = self.read("index.html")
        assert "does <b>not</b> read your browser's cookies" in html

    def test_an_unconfigured_provider_says_so_before_the_button_fails(self):
        assert "does not have sign-in details" in self.read("js", "app.js")

    def test_the_outdated_api_key_claim_was_corrected(self):
        # It used to say a chat subscription cannot be used. It now can.
        html = self.read("index.html")
        assert "Carrot can use a\n              subscription directly instead" in html

    def test_every_path_ends_on_the_tour(self):
        js = self.read("js", "app.js")
        assert "onboardStep('tour')" in js
        assert "function startLocalSetup" in js

    def test_the_tour_shows_where_help_is(self):
        html = self.read("index.html")
        assert "More → Help" in html

    def test_help_is_the_highlighted_row(self):
        # It is the one they will need first and never find on their own.
        html = self.read("index.html")
        tour = html.split('class="onboard-tour"')[1].split("</div>\n      <div class=\"onboard-actions\"")[0]
        assert 'tour-item highlight' in tour
        assert tour.index("highlight") < tour.index("Take me to Help") if "Take me to Help" in tour else True

    def test_the_last_button_lands_on_help_rather_than_describing_it(self):
        html = self.read("index.html")
        assert "finishOnboarding(false, 'help')" in html

    def test_finish_can_route_somewhere(self):
        assert "async function finishOnboarding(skipped, goTo)" in self.read("js", "app.js")

    def test_every_css_token_the_tour_uses_is_defined(self):
        import re

        css = self.read("css", "style.css")
        block = css.split("/* ===== Onboarding: the closing tour =====")[1]
        used = set(re.findall(r"var\((--[a-z0-9-]+)", block))
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
        assert used <= defined, f"undefined CSS tokens: {sorted(used - defined)}"


class TestTellingCarrotInASentence:
    """Fourteen labelled boxes is a form, and nobody fills in a form to try
    something out. One sentence answers four of them at once."""

    def reply(self, payload):
        import json
        from unittest.mock import patch

        from carrot import router as router_mod

        def fake(resolved, messages, tools=None):
            yield {"type": "text", "text": json.dumps(payload)}

        return patch.object(router_mod, "stream_events", fake), \
            patch.object(router_mod, "route", lambda **k: None)

    def test_a_sentence_fills_several_answers_at_once(self, client):
        stream, route = self.reply({"answers": {
            "school": "Dartmouth", "home": "Russell Sage",
            "gym": "4 times a week, 75 minutes"}})
        with stream, route:
            body = client.post("/api/planner/understand", json={
                "text": "I'm at Dartmouth in Russell Sage, gym 4x a week for 75 min"}).json()
        assert body["understood"]["school"] == "Dartmouth"
        assert len(body["understood"]) == 3

    def test_it_comes_back_with_the_next_question_not_a_form(self, client):
        stream, route = self.reply({"answers": {"school": "Dartmouth"}})
        with stream, route:
            body = client.post("/api/planner/understand",
                               json={"text": "I'm at Dartmouth"}).json()
        assert body["next_question"]["id"] == "home"
        assert body["next_question"]["why"]

    def test_fields_it_does_not_know_are_ignored(self, client):
        # A model inventing an extra key must not become a stored answer.
        stream, route = self.reply({"answers": {"school": "Dartmouth", "favourite_colour": "blue"}})
        with stream, route:
            body = client.post("/api/planner/understand", json={"text": "x"}).json()
        assert "favourite_colour" not in body["answers"]

    def test_empty_values_are_not_stored_as_answers(self, client):
        # An empty string would count as answered and stop it asking.
        stream, route = self.reply({"answers": {"school": "Dartmouth", "home": "   "}})
        with stream, route:
            body = client.post("/api/planner/understand", json={"text": "x"}).json()
        assert body["next_question"]["id"] == "home"

    def test_saying_nothing_is_a_400(self, client):
        assert client.post("/api/planner/understand", json={"text": "  "}).status_code == 400

    def test_the_prompt_forbids_guessing(self):
        # A plan built on an invented bedtime is a plan for someone else.
        assert "Do not guess" in planner.UNDERSTAND_PROMPT
        assert "invented bedtime" in planner.UNDERSTAND_PROMPT


class TestPlannerIsNotStudentware:
    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath("carrot", "web", *parts).read_text()

    def test_it_is_called_planner(self):
        html = self.read("index.html")
        assert "<h2>Planner</h2>" in html
        assert "Semester planner" not in html

    def test_the_copy_is_not_campus_only(self):
        # A timetable, a shift rota, a training schedule — the engine does not
        # care which, and calling it student-ware narrows it for no reason.
        html = self.read("index.html")
        assert "shift rota" in html or "a rota" in html

    def test_the_form_is_behind_a_disclosure(self):
        # Meeting a wall of fourteen required questions is what makes people
        # close the tab.
        assert '<details class="settings-card" id="planner-intake-card">' in self.read("index.html")

    def test_you_can_just_say_what_you_want(self):
        html = self.read("index.html")
        assert 'id="planner-freeform"' in html
        assert "function tellPlanner" in self.read("js", "planner.js")

    def test_every_css_token_the_conversation_uses_is_defined(self):
        import re

        css = self.read("css", "style.css")
        block = css.split("/* ===== Planner: the conversation =====")[1]
        used = set(re.findall(r"var\((--[a-z0-9-]+)", css.split("/* ===== Planner: the conversation =====")[1]))
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
        assert used <= defined, f"undefined CSS tokens: {sorted(used - defined)}"


class TestTheTruthAboutWhereItRuns:
    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath("carrot", "web", *parts).read_text()

    def test_the_empty_state_no_longer_claims_local_unconditionally(self):
        # With a hosted model selected it was simply false, and a privacy claim
        # that is false in the one place people read it is worse than none.
        html = self.read("index.html")
        assert "Everything runs on your machine" not in html
        assert 'id="chat-empty-line"' in html

    def test_it_says_cloud_when_the_route_is_cloud(self):
        js = self.read("js", "app.js")
        assert "over the internet" in js
        assert "function renderEmptyStateLine" in js

    def test_it_updates_when_the_model_changes(self):
        js = self.read("js", "app.js")
        assert js.count("renderEmptyStateLine()") >= 3


class TestTheOverlayIsAFullTurn:
    def test_the_non_streaming_path_runs_the_tool_loop(self):
        from pathlib import Path

        # It used to call the model once, directly, with no tools — so the
        # quick-ask overlay could not search and had none of the
        # never-answer-with-nothing guarantees.
        source = (Path(__file__).resolve().parents[1] / "carrot" / "app.py").read_text()
        chat = source.split("async def chat(req: ChatRequest):")[1][:2000]
        assert "_agentic_chat_events" in chat
        assert "router_mod.complete(resolved, history)" not in chat

    def test_the_overlay_can_ask_for_a_search_mode(self):
        from pathlib import Path

        main = (Path(__file__).resolve().parents[1] / "gui" / "main.js").read_text()
        assert "body.search_mode = opts.search_mode" in main

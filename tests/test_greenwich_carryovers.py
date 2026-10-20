"""Offline carryover regressions using invented property and publication data."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1] / "greenwich"


def load_module(name, filename, dependencies=None):
    """Keep these imports independent of the user's runtime test modules."""
    spec = importlib.util.spec_from_file_location(name, SRC / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with patch.dict(sys.modules, dependencies or {}):
        spec.loader.exec_module(module)
    return module


storage = load_module("greenwich_carryover_storage", "storage.py")
council = load_module("greenwich_carryover_council", "council.py", {"storage": storage})

NOW = datetime(2026, 12, 21, 12, tzinfo=council.LONDON)
TARGET = date(2026, 12, 28)
KINDS = ("recycling", "food", "general")
CALENDAR = [
    (date(2026, 12, 14), "Week B"),
    (date(2026, 12, 21), "Week A"),
    (TARGET, "Week B"),
    (date(2027, 1, 4), "Week A"),
    (date(2027, 1, 11), "Week B"),
    (date(2027, 1, 18), "Week A"),
]
REVISION = [(date(2026, 12, 25), TARGET)]
BANKS = [date(2026, 12, 25), TARGET, date(2027, 1, 1)]


def invented_property():
    return {
        "address_label": "99999 - Invented Carryover Road - Fictional Town - ZZ9 9ZZ",
        "property": {"sao": "", "pao": "99999", "streetName": "Invented Carryover Road",
                     "postTown": "Fictional Town", "Postcode": "ZZ9 9ZZ"},
    }


def published_date(day):
    return f"{day:%A} {day.day} {day:%B %Y}"


def calendar_html(weeks):
    # Separate publication headings avoid an ambiguous multi-year heading.
    tables = {}
    for number, (start, label) in enumerate(weeks, 1):
        text = f"{published_date(start)} to {published_date(start + timedelta(days=4))}"
        a, b = (text, "") if label == "Week A" else ("", text)
        tables.setdefault(start.year, []).append(
            f"<tr><td>{number}</td><td>{a}</td><td>{b}</td></tr>")
    return "".join(
        f"<h2>Invented calendar {year}</h2><table>"
        "<tr><th>Week</th><th>Week A</th><th>Week B</th></tr>"
        + "".join(rows) + "</table>" for year, rows in tables.items())


def holiday_html(revisions):
    return ("<h2>Invented holiday revisions</h2><table>"
            "<tr><th>Usual collection day</th><th>Revised collection day</th></tr>"
            + "".join(f"<tr><td>{published_date(usual)}</td>"
                      f"<td>{published_date(actual)}</td></tr>"
                      for usual, actual in revisions) + "</table>")


class CarryoverTests(unittest.TestCase):
    def setUp(self):
        self.config = invented_property()
        # No live transport or mounted property file is reachable from these tests.
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("Offline test.")))
        self.enterContext(patch.object(socket, "create_connection", side_effect=AssertionError("Offline test.")))
        self.enterContext(patch.object(council, "build_opener", side_effect=AssertionError("Offline test.")))
        self.enterContext(patch.object(council, "load_property", side_effect=AssertionError("Invented data only.")))

    def sources(self, *, day="Friday", frequency="Week A", weeks=None,
                revisions=None, banks=None):
        return {
            council.ADDRESS_URL: json.dumps([self.config["address_label"]]),
            council.PROPERTY_URL: json.dumps({**self.config["property"], "Day": day,
                                              "Frequency": frequency}),
            council.BLACK_URL: calendar_html(CALENDAR if weeks is None else weeks),
            council.HOLIDAY_URL: holiday_html(REVISION if revisions is None else revisions),
            council.BANK_HOLIDAY_URL: json.dumps({"england-and-wales": {
                "division": "england-and-wales", "events": [
                    {"title": "Invented holiday", "date": value.isoformat()}
                    for value in (BANKS if banks is None else banks)]}}),
        }

    def fetch(self, *, now=NOW, raw=None, **kwargs):
        raw = self.sources(**kwargs) if raw is None else raw

        def offline_fetch(url, data=None):
            return raw[url.split("?", 1)[0]]

        with patch.object(council, "_fetch", side_effect=offline_fetch) as fetch:
            result = council.fetch_schedule(now, self.config)
        self.assertEqual(fetch.call_count, 5)
        # One record per collection kind and original date, even for identity revisions.
        keys = [(item["kind"], item["usual_date"]) for item in result["collections"]]
        self.assertEqual(len(keys), len(set(keys)))
        for item in result["collections"]:
            self.assertEqual(set(item), {"kind", "usual_date", "date", "note"})
        self.assertNotIn("ZZ9", json.dumps(result))
        return result

    def items(self, result, usual):
        return [item for item in result["collections"] if item["usual_date"] == usual]

    def assert_collection(self, result, usual, actual, kinds=KINDS):
        items = self.items(result, usual)
        self.assertEqual([item["kind"] for item in items], list(kinds))
        self.assertEqual([item["date"] for item in items], [actual] * len(kinds))
        if actual is not None and actual != usual:
            for item in items:
                self.assertIn(usual, item["note"])
                self.assertIn("Published council holiday date", item["note"])

    def test_friday_week_a_carries_all_kinds_into_week_b(self):
        result = self.fetch()
        self.assertEqual((result["target_start"], result["target_end"]),
                         ("2026-12-28", "2027-01-03"))
        self.assert_collection(result, "2026-12-25", "2026-12-28")
        self.assert_collection(result, "2027-01-01", None, ("recycling", "food"))
        self.assertTrue(result["general_this_week"])
        self.assertEqual(result["general_next_date"], "2026-12-28")
        self.assertTrue(any("2027-01-01" in warning for warning in result["warnings"]))

    def test_original_week_not_target_week_controls_general_membership(self):
        result = self.fetch(frequency="Week B", revisions=REVISION + [
            (date(2027, 1, 1), date(2027, 1, 2))])
        self.assert_collection(result, "2026-12-25", "2026-12-28", ("recycling", "food"))
        self.assert_collection(result, "2027-01-01", "2027-01-02")
        self.assertEqual(result["general_next_date"], "2027-01-02")
        self.assertTrue(result["general_this_week"])

    def test_weekly_property_carries_all_kinds_regardless_of_ab_labels(self):
        result = self.fetch(frequency="Weekly")
        self.assert_collection(result, "2026-12-25", "2026-12-28")
        self.assert_collection(result, "2027-01-01", None)
        self.assertEqual(result["general_next_date"], "2026-12-28")

    def test_revisions_for_another_property_weekday_are_not_collections(self):
        result = self.fetch(day="Thursday")
        self.assertEqual(len(result["collections"]), 2)
        self.assert_collection(result, "2026-12-31", None, ("recycling", "food"))
        self.assertFalse(result["general_this_week"])

    def test_target_week_revision_and_identity_revision_do_not_duplicate_items(self):
        for actual in (date(2027, 1, 1), date(2027, 1, 3)):
            with self.subTest(actual=actual):
                result = self.fetch(frequency="Week B", revisions=[(date(2027, 1, 1), actual)])
                self.assertEqual(len(result["collections"]), 3)
                self.assert_collection(result, "2027-01-01", actual.isoformat())
                self.assertEqual(result["general_next_date"], actual.isoformat())

    def test_identical_revisions_repeated_across_tables_do_not_duplicate_items(self):
        raw = self.sources()
        raw[council.HOLIDAY_URL] += holiday_html(REVISION)
        result = self.fetch(raw=raw)
        self.assert_collection(result, "2026-12-25", "2026-12-28")
        self.assertEqual(len(result["collections"]), 5)

    def test_revision_is_one_lookup_even_when_destination_is_another_mapping_key(self):
        result = self.fetch(revisions=REVISION + [(TARGET, date(2026, 12, 29))])
        self.assert_collection(result, "2026-12-25", "2026-12-28")
        self.assertFalse(self.items(result, "2026-12-28"))
        self.assertEqual(result["general_next_date"], "2026-12-28")

    def test_chain_between_two_fridays_keeps_both_original_dates(self):
        result = self.fetch(frequency="Weekly", revisions=[
            (date(2026, 12, 25), date(2027, 1, 1)),
            (date(2027, 1, 1), date(2027, 1, 2))])
        self.assert_collection(result, "2026-12-25", "2027-01-01")
        self.assert_collection(result, "2027-01-01", "2027-01-02")
        self.assertEqual(len(result["collections"]), 6)
        self.assertEqual(result["general_next_date"], "2027-01-01")

    def test_multiple_carryovers_choose_earliest_actual_not_earliest_original(self):
        result = self.fetch(frequency="Weekly", revisions=[
            (date(2026, 12, 18), date(2027, 1, 1)),  # Maximum supported 14-day delay.
            (date(2026, 12, 25), TARGET),
            (date(2027, 1, 1), date(2027, 1, 2))])
        self.assert_collection(result, "2026-12-18", "2027-01-01")
        self.assert_collection(result, "2026-12-25", "2026-12-28")
        self.assertEqual(len(result["collections"]), 9)
        self.assertEqual(result["general_next_date"], "2026-12-28")

    def test_two_week_carryover_uses_its_own_calendar_membership(self):
        result = self.fetch(frequency="Week B", revisions=[
            (date(2026, 12, 18), date(2027, 1, 1)),
            (date(2026, 12, 25), TARGET),
            (date(2027, 1, 1), date(2027, 1, 2))])
        self.assert_collection(result, "2026-12-18", "2027-01-01")
        self.assert_collection(result, "2026-12-25", "2026-12-28", ("recycling", "food"))
        self.assertEqual(result["general_next_date"], "2027-01-01")

    def test_distinct_original_dates_at_same_destination_retain_their_provenance(self):
        result = self.fetch(frequency="Weekly", revisions=[
            (date(2026, 12, 18), TARGET), (date(2026, 12, 25), TARGET)])
        for usual in ("2026-12-18", "2026-12-25"):
            self.assert_collection(result, usual, "2026-12-28")
        self.assertEqual(result["general_next_date"], "2026-12-28")

    def test_target_sunday_is_included_but_following_monday_is_not(self):
        for actual, included in ((date(2027, 1, 3), True), (date(2027, 1, 4), False)):
            with self.subTest(actual=actual):
                result = self.fetch(revisions=[(date(2026, 12, 25), actual)])
                if included:
                    self.assert_collection(result, "2026-12-25", actual.isoformat())
                else:
                    self.assertFalse(self.items(result, "2026-12-25"))
                self.assertEqual(result["general_this_week"], included)
                # An outstanding carryover can still be the next general date after the target week.
                self.assertEqual(result["general_next_date"], actual.isoformat())

    def test_revision_finishing_before_target_is_not_a_pending_carryover(self):
        result = self.fetch(revisions=[
            (date(2026, 12, 18), date(2026, 12, 27)),
            (date(2027, 1, 8), date(2027, 1, 9))])
        self.assertFalse(self.items(result, "2026-12-18"))
        self.assertFalse(result["general_this_week"])
        self.assertEqual(result["general_next_date"], "2027-01-09")

    def test_target_general_moved_out_is_retained_but_not_counted_in_target_week(self):
        result = self.fetch(frequency="Week B", revisions=[
            (date(2027, 1, 1), date(2027, 1, 4))])
        self.assert_collection(result, "2027-01-01", "2027-01-04")
        self.assertFalse(result["general_this_week"])
        self.assertEqual(result["general_next_date"], "2027-01-04")

    def test_incoming_general_still_counts_when_target_regular_collection_moves_out(self):
        result = self.fetch(frequency="Weekly", revisions=REVISION + [
            (date(2027, 1, 1), date(2027, 1, 4))])
        self.assert_collection(result, "2026-12-25", "2026-12-28")
        self.assert_collection(result, "2027-01-01", "2027-01-04")
        self.assertTrue(result["general_this_week"])
        self.assertEqual(result["general_next_date"], "2026-12-28")

    def test_next_general_compares_delayed_target_with_later_usual_dates(self):
        result = self.fetch(frequency="Weekly", revisions=[
            (date(2027, 1, 1), date(2027, 1, 15)),
            (date(2027, 1, 8), date(2027, 1, 9))])
        self.assertFalse(result["general_this_week"])
        self.assertEqual(result["general_next_date"], "2027-01-09")
        self.assert_collection(result, "2027-01-01", "2027-01-15")
        self.assertFalse(self.items(result, "2027-01-08"))

    def test_january_target_accepts_previous_year_original_week(self):
        result = self.fetch(now=datetime(2026, 12, 28, 12, tzinfo=council.LONDON),
                            frequency="Week B", revisions=[
                                (date(2027, 1, 1), date(2027, 1, 4))])
        self.assertEqual(result["target_start"], "2027-01-04")
        self.assert_collection(result, "2027-01-01", "2027-01-04")
        self.assertTrue(result["general_this_week"])
        self.assertEqual(result["general_next_date"], "2027-01-04")

    def test_confirmed_carryover_needs_no_future_calendar_extrapolation(self):
        result = self.fetch(weeks=CALENDAR[:3])
        self.assert_collection(result, "2026-12-25", "2026-12-28")
        self.assertEqual(result["general_next_date"], "2026-12-28")
        self.assertFalse(any("beyond the published calendar" in note for note in result["warnings"]))

    def test_missing_original_ab_week_fails_closed_instead_of_inventing_membership(self):
        with self.assertRaisesRegex(council.CouncilError, "calendar.*original|original.*calendar"):
            self.fetch(weeks=CALENDAR[2:])

    def test_weekly_carryover_does_not_need_original_ab_week(self):
        result = self.fetch(frequency="Weekly", weeks=CALENDAR[2:])
        self.assert_collection(result, "2026-12-25", "2026-12-28")
        self.assertEqual(result["general_next_date"], "2026-12-28")

    def test_missing_target_calendar_still_fails_even_with_published_carryover(self):
        with self.assertRaisesRegex(council.CouncilError, "covers the target week"):
            self.fetch(weeks=CALENDAR[:2])

    def test_published_labels_are_not_inferred_by_alternating_from_target(self):
        weeks = [(week, "Week A") for week, _ in CALENDAR]
        result = self.fetch(weeks=weeks, revisions=REVISION + [
            (date(2027, 1, 1), date(2027, 1, 2))])
        self.assert_collection(result, "2026-12-25", "2026-12-28")
        self.assert_collection(result, "2027-01-01", "2027-01-02")
        self.assertEqual(result["general_next_date"], "2026-12-28")

    def test_unknown_target_general_is_not_replaced_with_a_later_confirmed_date(self):
        result = self.fetch(frequency="Weekly", revisions=[
            (date(2026, 12, 25), date(2027, 1, 2))])
        self.assert_collection(result, "2026-12-25", "2027-01-02")
        self.assert_collection(result, "2027-01-01", None)
        self.assertTrue(result["general_this_week"])
        self.assertIsNone(result["general_next_date"])
        self.assertTrue(any("2027-01-01" in note for note in result["warnings"]))

    def test_bank_holidays_alone_never_create_unpublished_carryovers(self):
        result = self.fetch(revisions=[(date(2026, 12, 22), date(2026, 12, 23))])
        self.assertFalse(self.items(result, "2026-12-25"))
        self.assert_collection(result, "2027-01-01", None, ("recycling", "food"))
        self.assertFalse(result["general_this_week"])
        self.assertIsNone(result["general_next_date"])
        self.assertTrue(any("Next general waste collection" in note for note in result["warnings"]))

    def test_unknown_due_general_keeps_unknown_contract_and_suppresses_false_absence(self):
        result = self.fetch(frequency="Week B", revisions=[
            (date(2026, 12, 22), date(2026, 12, 23))])
        self.assert_collection(result, "2027-01-01", None)
        self.assertTrue(result["general_this_week"])
        self.assertIsNone(result["general_next_date"])

    def test_no_published_future_general_returns_unknown_without_extrapolation(self):
        result = self.fetch(weeks=CALENDAR[:3], revisions=[
            (date(2026, 12, 22), date(2026, 12, 23))])
        self.assertIsNone(result["general_next_date"])
        self.assertTrue(any("beyond the published calendar" in note for note in result["warnings"]))

    def test_bank_data_must_still_cover_unknown_target_period(self):
        with self.assertRaisesRegex(council.CouncilError, "does not cover"):
            self.fetch(banks=[date(2026, 12, 25)])

    def test_malformed_conflicting_or_excessively_delayed_revisions_fail_closed(self):
        valid = holiday_html(REVISION)
        invalid = [
            valid.replace("Monday 28 December 2026", "Tuesday 28 December 2026"),
            valid + holiday_html([(date(2026, 12, 25), date(2026, 12, 29))]),
            holiday_html([(date(2026, 12, 25), date(2027, 1, 9))]),
        ]
        for html in invalid:
            with self.subTest(html=html):
                raw = self.sources()
                raw[council.HOLIDAY_URL] = html
                with self.assertRaises(council.CouncilError):
                    self.fetch(raw=raw)

    def test_normal_schedule_keeps_due_off_week_and_weekly_behaviour(self):
        start = date(2026, 10, 19)
        weeks = [(start + timedelta(weeks=i), "Week A" if i % 2 == 0 else "Week B")
                 for i in range(4)]
        now = datetime(2026, 10, 19, 12, tzinfo=council.LONDON)
        for frequency, due, next_date in (("Week A", False, "2026-11-06"),
                                          ("Week B", True, "2026-10-30"),
                                          ("Weekly", True, "2026-10-30")):
            with self.subTest(frequency=frequency):
                result = self.fetch(now=now, frequency=frequency, weeks=weeks)
                kinds = KINDS if due else ("recycling", "food")
                self.assert_collection(result, "2026-10-30", "2026-10-30", kinds)
                self.assertEqual(len(result["collections"]), len(kinds))
                self.assertEqual(result["general_this_week"], due)
                self.assertEqual(result["general_next_date"], next_date)
                self.assertEqual(result["warnings"], [])


if __name__ == "__main__":
    unittest.main()

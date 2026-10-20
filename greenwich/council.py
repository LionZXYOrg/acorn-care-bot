"""Verify published Greenwich dates for an explicitly configured private property.

Only Python 3.12 standard-library modules are required. No address is built in.
A failed source request never falls back to a cache. Dates outside the published
calendar remain unknown. Naive clocks mean London wall time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from storage import atomic_json, strict_json

LONDON = ZoneInfo("Europe/London")
BASE_URL = "https://www.royalgreenwich.gov.uk"
ADDRESS_URL = BASE_URL + "/site/custom_scripts/apps/waste-collection/source.php"
PROPERTY_URL = BASE_URL + "/site/custom_scripts/apps/waste-collection/ajax-response-uprn.php"
BLACK_URL = BASE_URL + "/recycling-and-rubbish/bins-and-collections/black-top-bin-collections"
HOLIDAY_URL = BASE_URL + "/recycling-and-rubbish/bins-and-collections/bank-holiday-collection-dates"
BANK_HOLIDAY_URL = "https://www.gov.uk/bank-holidays.json"
SOURCES = (ADDRESS_URL, PROPERTY_URL, BLACK_URL, HOLIDAY_URL, BANK_HOLIDAY_URL)
PROPERTY_FIELDS = {"sao", "pao", "streetName", "postTown", "Postcode"}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")
_DATE_PATTERN = re.compile(
    rf"({'|'.join(WEEKDAYS)})\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"({'|'.join(MONTHS)})(?:\s+(\d{{4}}))?", re.IGNORECASE
)


class CouncilError(RuntimeError):
    """A fixed error message that contains no private source data."""


def _space(text: str) -> str:
    return " ".join(text.split())


def _key(text: str) -> str:
    return _space(text).casefold()


def validate_property(value: dict) -> dict:
    """Require the full label and all five property fields, without extra keys.

    The label has four parts separated by ' - '. Its house part is SAO plus PAO,
    separated by a space when SAO is present. Matching ignores case and repeated
    whitespace, but never accepts a substring or a missing property field.
    """
    if (not isinstance(value, dict) or set(value) != {"address_label", "property"}
            or not isinstance(value["address_label"], str)
            or not isinstance(value["property"], dict)
            or set(value["property"]) != PROPERTY_FIELDS):
        raise CouncilError("The private property configuration has an invalid schema.")
    fields = value["property"]
    for name, text in fields.items():
        if (not isinstance(text, str) or len(text) > 300
                or any(ord(c) < 32 or ord(c) == 127 for c in text)
                or (name != "sao" and not text.strip())):
            raise CouncilError("The private property configuration has invalid fields.")
    label = value["address_label"]
    expected = [" ".join(x for x in (fields["sao"], fields["pao"]) if x),
                fields["streetName"], fields["postTown"], fields["Postcode"]]
    if (len(label) > 1200 or any(ord(c) < 32 or ord(c) == 127 for c in label)
            or [_key(p) for p in label.split(" - ")] != [_key(p) for p in expected]):
        raise CouncilError("The private property label does not match its fields.")
    return {"address_label": label, "property": dict(fields)}


def load_property(path: Path | None = None) -> dict:
    if path is None:
        path = Path(os.environ.get("BINS_PROPERTY_PATH", "/etc/bins/property.json"))
    try:
        return validate_property(strict_json(Path(path).read_text(encoding="utf-8")))
    except Exception:
        raise CouncilError("The private property configuration is missing or invalid.") from None


def property_identity(config: dict) -> str:
    config = validate_property(config)
    encoded = json.dumps(config, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _local(now: datetime) -> datetime:
    if not isinstance(now, datetime):
        raise CouncilError("The schedule clock must be a datetime.")
    if now.tzinfo is None or now.utcoffset() is None:
        return now.replace(tzinfo=LONDON)
    return now.astimezone(LONDON)


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _json(text: str, source: str):
    try:
        return strict_json(text)
    except (ValueError, TypeError):
        raise CouncilError("An official source returned invalid JSON.") from None


def _address(text: str, config: dict) -> str:
    addresses = _json(text, "council address search")
    if not isinstance(addresses, list) or not all(isinstance(a, str) for a in addresses):
        raise CouncilError("Unrecognised council address search format.")
    expected = [_key(part) for part in config["address_label"].split(" - ")]
    matches = [a for a in addresses
               if [_key(p) for p in re.split(r"\s+-\s+", a.strip())] == expected]
    if len(matches) != 1:
        raise CouncilError("The council address search did not return one exact match.")
    return matches[0]


def _property(text: str, config: dict) -> tuple[str, str]:
    details = _json(text, "council property lookup")
    if not isinstance(details, dict):
        raise CouncilError("Unrecognised council property format.")
    for name, expected in config["property"].items():
        actual = details.get(name)
        if not isinstance(actual, str) or _key(actual) != _key(expected):
            raise CouncilError("The council property does not exactly match the configuration.")
    day, frequency = details.get("Day"), details.get("Frequency")
    if day not in WEEKDAYS or frequency not in ("Week A", "Week B", "Weekly"):
        raise CouncilError("Unrecognised council collection day or frequency.")
    return day, frequency


@dataclass
class _Table:
    headings: tuple[str, ...]
    rows: list[list[str]] = field(default_factory=list)
    caption: str = ""


class _Tables(HTMLParser):
    """Collect table text and its enclosing headings, not footer years.

    Inline markup, entities, line breaks, captions, and optional td/tr end
    tags are supported. Nested or unclosed tables cannot be read safely.
    """

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.tables: list[_Table] = []
        self.headings: dict[int, str] = {}
        self.table: _Table | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.heading: list[str] | None = None
        self.level = 0
        self.caption: list[str] | None = None
        self.ignored = 0
        self.feed(html)
        self.close()
        if self.table is not None:
            raise CouncilError("Unclosed council HTML table")

    def _end_cell(self):
        if self.cell is not None:
            if self.row is None:
                raise CouncilError("Council table cell has no row")
            self.row.append(_space("".join(self.cell)))
            self.cell = None

    def _end_row(self):
        self._end_cell()
        if self.row is not None:
            if self.table is not None and self.row:
                self.table.rows.append(self.row)
            self.row = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignored += 1
            return
        if self.ignored:
            return
        if re.fullmatch(r"h[1-6]", tag) and self.table is None:
            self.level, self.heading = int(tag[1]), []
        elif tag == "table":
            if self.table is not None:
                raise CouncilError("Nested council tables are not supported")
            self.table = _Table(tuple(self.headings[k] for k in sorted(self.headings)))
        elif tag == "tr" and self.table is not None:
            self._end_row()
            self.row = []
        elif tag in ("td", "th") and self.table is not None:
            if self.row is None:
                raise CouncilError("Council table cell has no row")
            if any(k in ("colspan", "rowspan") and v != "1" for k, v in attrs):
                raise CouncilError("Spanning council table cells are not supported")
            self._end_cell()
            self.cell = []
        elif tag == "caption" and self.table is not None:
            self.caption = []
        elif tag in ("br", "p", "div"):
            self.handle_data(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.ignored = max(0, self.ignored - 1)
            return
        if self.ignored:
            return
        if tag == f"h{self.level}" and self.heading is not None:
            self.headings = {k: v for k, v in self.headings.items() if k < self.level}
            self.headings[self.level] = _space("".join(self.heading))
            self.heading = None
        elif tag in ("td", "th"):
            self._end_cell()
        elif tag == "tr":
            self._end_row()
        elif tag == "caption" and self.caption is not None and self.table is not None:
            self.table.caption = _space("".join(self.caption))
            self.caption = None
        elif tag == "table" and self.table is not None:
            self._end_row()
            self.tables.append(self.table)
            self.table = None
        elif tag in ("p", "div"):
            self.handle_data(" ")

    def handle_data(self, data):
        if self.ignored:
            return
        if self.cell is not None:
            self.cell.append(data)
        elif self.caption is not None:
            self.caption.append(data)
        elif self.heading is not None:
            self.heading.append(data)


@dataclass(frozen=True)
class _DateParts:
    weekday: int
    day: int
    month: int
    year: int | None

    def at_year(self, year: int) -> date:
        value = date(self.year if self.year is not None else year, self.month, self.day)
        if value.weekday() != self.weekday:
            raise ValueError("Published weekday does not match the date")
        return value

    @property
    def month_day(self) -> tuple[int, int]:
        return self.month, self.day


def _date_parts(text: str, *, holiday: bool = False) -> _DateParts:
    if holiday:
        text = re.sub(r"\s*\(bank holiday\)\s*", " ", text, flags=re.IGNORECASE)
    match = _DATE_PATTERN.fullmatch(_space(text))
    if not match:
        raise CouncilError("Unrecognised published date.")
    weekday, day, month, year = match.groups()
    return _DateParts(tuple(map(str.casefold, WEEKDAYS)).index(weekday.casefold()),
                      int(day), tuple(map(str.casefold, MONTHS)).index(month.casefold()) + 1,
                      int(year) if year else None)


def _heading_year(table: _Table) -> int | None:
    years = set(re.findall(r"\b\d{4}\b", " ".join((*table.headings, table.caption))))
    if len(years) > 1:
        raise CouncilError("Ambiguous year in council table headings")
    return int(next(iter(years))) if years else None


def _calendar_range(text: str, year: int) -> tuple[date, date]:
    pieces = re.split(r"\s+(?:to|[–—-])\s+", _space(text), flags=re.IGNORECASE)
    if len(pieces) != 2:
        raise CouncilError("Unrecognised calendar range.")
    first, last = map(_date_parts, pieces)
    candidates = set()
    for start_year in (year - 1, year, year + 1):
        for end_year in (year - 1, year, year + 1):
            try:
                start, end = first.at_year(start_year), last.at_year(end_year)
            except ValueError:
                continue
            if (start.weekday() == 0 and end.weekday() == 4
                    and end - start == timedelta(days=4)
                    and start <= date(year, 12, 31) and end >= date(year, 1, 1)):
                candidates.add((start, end))
    if len(candidates) != 1:
        raise CouncilError("Cannot verify calendar range and weekdays.")
    return candidates.pop()


def _calendar(html: str) -> dict[date, str]:
    weeks: dict[date, str] = {}
    found = False
    for table in _Tables(html).tables:
        if not table.rows:
            continue
        headers = [_key(c) for c in table.rows[0]]
        if set(headers) != {"week", "week a", "week b"} or len(headers) != 3:
            continue
        found = True
        year = _heading_year(table)
        if year is None:
            raise CouncilError("The black bin calendar has no publication year heading")
        for row in table.rows[1:]:
            if len(row) != 3 or not row[headers.index("week")].isdigit():
                raise CouncilError("Unrecognised black bin calendar row")
            for label in ("Week A", "Week B"):
                cell = row[headers.index(label.casefold())]
                if not cell:
                    continue
                start, _ = _calendar_range(cell, year)
                if start in weeks and weeks[start] != label:
                    raise CouncilError("Conflicting Week A/B calendar membership")
                weeks[start] = label
    if not found or not weeks:
        raise CouncilError("No recognised published Week A/B calendar table")
    ordered = sorted(weeks)
    if any(b - a != timedelta(days=7) for a, b in zip(ordered, ordered[1:])):
        raise CouncilError("The published black bin calendar has missing weeks")
    return weeks


def _holiday_candidates(rows: list[tuple[_DateParts, _DateParts]], year: int,
                        anchored: bool) -> list[dict[date, date]]:
    """Infer one coherent table year; allow a December-to-January rollover."""
    candidates = []
    first = rows[0][0]
    start_years = (first.year,) if first.year is not None else (year - 1, year)
    for start_year in start_years:
        mapping: dict[date, date] = {}
        previous: date | None = None
        inferred_year = start_year
        try:
            for usual_parts, revised_parts in rows:
                if previous and usual_parts.month_day < (previous.month, previous.day):
                    if previous.month != 12 or usual_parts.month != 1:
                        raise ValueError("Holiday rows are not in date order")
                    inferred_year = previous.year + 1
                usual = usual_parts.at_year(inferred_year)
                revised_year = usual.year + (revised_parts.month_day < usual_parts.month_day)
                revised = revised_parts.at_year(revised_year)
                if previous and usual <= previous:
                    raise ValueError("Duplicate or unordered usual holiday dates")
                if not timedelta(0) <= revised - usual <= timedelta(days=14):
                    raise ValueError("Holiday revision is outside a verifiable fortnight")
                mapping[usual] = revised
                previous, inferred_year = usual, usual.year
        except ValueError:
            continue
        explicit = any(a.year is not None or b.year is not None for a, b in rows)
        if (explicit and not anchored) or any(d.year == year for d in mapping):
            if mapping not in candidates:
                candidates.append(mapping)
    return candidates


def _holiday_mapping(html: str, asof_year: int) -> tuple[dict[date, date], list[str]]:
    mapping: dict[date, date] = {}
    warnings: list[str] = []
    found = False
    for table in _Tables(html).tables:
        if not table.rows:
            continue
        headers = [_key(c).replace("date", "day") for c in table.rows[0]]
        if set(headers) != {"usual collection day", "revised collection day"} or len(headers) != 2:
            continue
        found = True
        rows = []
        for row in table.rows[1:]:
            if len(row) != 2 or not all(row):
                raise CouncilError("Unrecognised council holiday table row")
            rows.append((_date_parts(row[headers.index("usual collection day")], holiday=True),
                         _date_parts(row[headers.index("revised collection day")], holiday=True)))
        if not rows:
            raise CouncilError("The council holiday table is empty")
        year = _heading_year(table)
        candidates = _holiday_candidates(rows, year if year is not None else asof_year,
                                         anchored=year is not None)
        if not candidates and year is None and all(a.year is None and b.year is None for a, b in rows):
            # A leftover yearless table must not be moved to an arbitrary year
            # just to make its weekdays fit. Treat all of its dates as unconfirmed.
            warnings.append(f"Yearless council holiday table does not match {asof_year} weekdays; ignored.")
            continue
        if len(candidates) != 1:
            raise CouncilError("Cannot infer a unique council holiday year and valid weekdays")
        for usual, revised in candidates[0].items():
            if usual in mapping and mapping[usual] != revised:
                raise CouncilError("Conflicting council holiday revisions")
            mapping[usual] = revised
    if not found:
        raise CouncilError("No recognised usual/revised council holiday table")
    return mapping, warnings


def _bank_holidays(text: str) -> set[date]:
    data = _json(text, "GOV.UK bank holidays")
    try:
        division = data["england-and-wales"]
        events = division["events"]
        if division["division"] != "england-and-wales" or not isinstance(events, list) or not events:
            raise ValueError("Missing England/Wales events")
        dates = set()
        for event in events:
            value = event["date"]
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value or not isinstance(event["title"], str):
                raise ValueError("Unrecognised bank holiday event")
            dates.add(parsed)
        return dates
    except (KeyError, TypeError, ValueError) as exc:
        raise CouncilError("Unrecognised GOV.UK England/Wales bank holiday format") from None


def _collection_date(usual: date, mapping: dict[date, date], banks: set[date]
                     ) -> tuple[str | None, str]:
    # Deliberately one lookup: Monday -> Tuesday must not then become Wednesday.
    if usual in mapping:
        return mapping[usual].isoformat(), f"Published council holiday date (usual date {usual.isoformat()})."
    week = _monday(usual)
    start, end = week - timedelta(days=7), week + timedelta(days=6)
    if not {start.year, end.year} <= {d.year for d in banks}:
        raise CouncilError("GOV.UK bank holiday data does not cover the collection/backlog period")
    if any(start <= holiday <= end for holiday in banks):
        return None, f"Council holiday date not confirmed for usual date {usual.isoformat()}."
    return usual.isoformat(), "Usual collection date; no published holiday change applies."


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CouncilError("Official source redirects are not accepted.")


def _fetch(url: str, data: bytes | None = None) -> str:
    headers = {"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        request = Request(url, data=data, headers=headers)
        with build_opener(_NoRedirect()).open(request, timeout=30) as response:
            if response.status != 200:
                raise CouncilError("An official source returned an unexpected HTTP status.")
            body = response.read(4_000_001)
            if len(body) > 4_000_000:
                raise CouncilError("An official source exceeded the response size limit.")
            return body.decode("utf-8-sig")
    except Exception:
        raise CouncilError("Could not fetch an official source. No cached fallback was used.") from None


def _cached(path: Path, now: datetime, identity: str) -> dict | None:
    try:
        cached = strict_json(Path(path).read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(cached["fetched_at"])
        raw = cached["raw"]
        if (cached["version"] != 2 or cached["property_identity"] != identity
                or fetched.tzinfo is None or fetched.utcoffset() is None
                or not isinstance(raw, dict) or set(raw) != set(SOURCES)
                or not all(isinstance(value, str) for value in raw.values())):
            return None
        age = now.astimezone(timezone.utc) - fetched.astimezone(timezone.utc)
        if (timedelta(0) <= age < timedelta(days=7)
                and _monday(fetched.astimezone(LONDON).date()) == _monday(now.date())):
            return cached
    except (OSError, ValueError, TypeError, KeyError, OverflowError, AttributeError):
        pass
    return None


def fetch_schedule(now: datetime, property_config: dict, cache_path: Path | None = None,
                   force: bool = True) -> dict:
    """Return next week's verified dates without exposing the address in the result.

    Only force=False permits a cache from the same London publication week.
    The cache must also contain the exact configuration identity. A failed fresh
    request never uses a cache. Each cached response is verified again.
    Keep target-week usual collections, including unknown or moved-out dates,
    and add confirmed incoming revisions. General waste uses the original week's
    published A/B membership, never the revised date's week.
    """
    config = validate_property(property_config)
    identity = property_identity(config)
    now = _local(now)
    target = _monday(now.date()) + timedelta(days=7)
    cached = _cached(cache_path, now, identity) if cache_path is not None and not force else None
    if cached is not None:
        raw, fetched_at = cached["raw"], cached["fetched_at"]
    else:
        lookup_url = ADDRESS_URL + "?" + urlencode({"term": config["property"]["Postcode"]})
        raw = {ADDRESS_URL: _fetch(lookup_url)}
        label = _address(raw[ADDRESS_URL], config)
        raw[PROPERTY_URL] = _fetch(PROPERTY_URL, urlencode({"address": label}).encode("ascii"))
        for url in (BLACK_URL, HOLIDAY_URL, BANK_HOLIDAY_URL):
            raw[url] = _fetch(url)
        fetched_at = now.isoformat()

    _address(raw[ADDRESS_URL], config)
    day, frequency = _property(raw[PROPERTY_URL], config)
    calendar = _calendar(raw[BLACK_URL])
    if target not in calendar:
        raise CouncilError("No published black bin calendar covers the target week. Dates were not extrapolated.")
    holiday_mapping, warnings = _holiday_mapping(raw[HOLIDAY_URL], now.year)
    banks = _bank_holidays(raw[BANK_HOLIDAY_URL])
    target_end = target + timedelta(days=6)
    weekday = WEEKDAYS.index(day)
    usual = target + timedelta(days=weekday)
    # Include only direct revisions for this property's usual weekday. Keep later
    # carryovers as next-date candidates, but do not list them in the target week.
    carryovers = {original for original, revised in holiday_mapping.items()
                  if original < target and original.weekday() == weekday and revised >= target}
    resolved: dict[date, tuple[str | None, str]] = {}
    general_originals: set[date] = set()
    collections = []
    for original in sorted({usual} | carryovers):
        original_week = _monday(original)
        if frequency != "Weekly" and original_week not in calendar:
            raise CouncilError("No published black bin calendar covers the original collection week. "
                               "Dates were not extrapolated.")
        general_due = frequency == "Weekly" or calendar[original_week] == frequency
        actual, note = resolved[original] = _collection_date(original, holiday_mapping, banks)
        if general_due:
            general_originals.add(original)
        # Preserve target-week unknowns and moved-out dates for the renderer's
        # usual_date/changed-from contract. Incoming dates must be confirmed.
        if original == usual or holiday_mapping[original] <= target_end:
            kinds = ["recycling", "food"] + (["general"] if general_due else [])
            collections.extend({"kind": kind, "usual_date": original.isoformat(),
                                "date": actual, "note": note} for kind in kinds)
            if actual is None:
                warnings.append(note)
    # Unknown due dates must not cause a false "no general waste" message.
    general_this_week = any(item["kind"] == "general" and (
        item["date"] is None or target.isoformat() <= item["date"] <= target_end.isoformat())
        for item in collections)

    general_originals.update(week + timedelta(days=weekday) for week in calendar
                             if week > target and (frequency == "Weekly" or calendar[week] == frequency))
    general_next = None
    if not general_originals:
        warnings.append("Next general waste date is not confirmed beyond the published calendar.")
    for original in sorted(general_originals):
        # Revisions never move backwards. Once a confirmed date is no later than
        # the next usual date, later published weeks cannot improve that answer.
        if general_next is not None and general_next <= original.isoformat():
            break
        actual, note = (resolved[original] if original in resolved
                        else _collection_date(original, holiday_mapping, banks))
        if actual is None:
            # Do not skip an earlier uncertain collection to advertise a later
            # confirmed one. Target-week unknowns already have a warning above.
            general_next = None
            if original not in resolved:
                warnings.append("Next general waste collection: " + note)
            break
        if general_next is None or actual < general_next:
            general_next = actual
    result = {"fetched_at": fetched_at, "target_start": target.isoformat(),
              "target_end": target_end.isoformat(),
              "property_identity": identity, "day": day, "frequency": frequency,
              "collections": collections, "general_this_week": general_this_week,
              "general_next_date": general_next, "warnings": warnings, "sources": list(SOURCES)}
    if cache_path is not None and cached is None:
        try:
            atomic_json(Path(cache_path), {"version": 2, "property_identity": identity,
                                         "fetched_at": fetched_at, "raw": raw})
        except OSError:
            warnings.append("Could not save the council cache.")
    return result

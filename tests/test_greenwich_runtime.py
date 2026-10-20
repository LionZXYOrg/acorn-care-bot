"""Offline tests with invented property data and temporary state only."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
from datetime import date, datetime, timedelta, timezone
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "greenwich"

# Use private module names so other application tests cannot replace these modules.
def load(name, filename, dependencies=None):
    spec = importlib.util.spec_from_file_location(name, SRC / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with patch.dict(sys.modules, dependencies or {}):
        spec.loader.exec_module(module)
    return module


storage = load("greenwich_test_storage", "storage.py")
council = load("greenwich_test_council", "council.py", {"storage": storage})
bot = load("greenwich_test_bot", "bot.py", {"storage": storage, "council": council})


def property_config(number="9001"):
    return {"address_label": f"{number} - Example Test Street - Sample Town - ZZ1 1ZZ",
            "property": {"sao": "", "pao": number, "streetName": "Example Test Street",
                         "postTown": "Sample Town", "Postcode": "ZZ1 1ZZ"}}


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=council.LONDON)
ACORN_ID = 999000
ACORN_USERNAME = "example_acorn_care_bot"


def calendar_html(start=date(2026, 10, 19), count=8):
    rows = ["<h1>Black-top collections 2026</h1><table><tr><th>Week</th><th>Week A</th><th>Week B</th></tr>"]
    for i in range(count):
        first = start + timedelta(days=i * 7)
        last = first + timedelta(days=4)
        text = f"{first:%A} {first.day} {first:%B %Y} to {last:%A} {last.day} {last:%B %Y}"
        a, b = (text, "") if i % 2 == 0 else ("", text)
        rows.append(f"<tr><td>{i + 1}</td><td>{a}</td><td>{b}</td></tr>")
    return "".join(rows) + "</table>"


def holiday_html(rows=None, year="2026"):
    rows = rows or [("Friday 25 December 2026", "Monday 28 December 2026")]
    return (f"<h2>Holiday collections {year}</h2><table>"
            "<tr><th>Usual collection day</th><th>Revised collection day</th></tr>"
            + "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in rows) + "</table>")


def sources(config=None, frequency="Week B", banks=None):
    config = config or property_config()
    events = banks or ["2025-01-01", "2026-01-01", "2026-12-25", "2027-01-01"]
    return {council.ADDRESS_URL: json.dumps([config["address_label"]]),
            council.PROPERTY_URL: json.dumps({**config["property"], "Day": "Monday", "Frequency": frequency}),
            council.BLACK_URL: calendar_html(), council.HOLIDAY_URL: holiday_html(),
            council.BANK_HOLIDAY_URL: json.dumps({"england-and-wales": {"division": "england-and-wales",
                "events": [{"title": "Synthetic holiday", "date": day} for day in events]}})}


def schedule(now=NOW, identity=None):
    start = date.fromisoformat(bot.week_key(now)) + timedelta(days=7)
    day = start.isoformat()
    return {"fetched_at": now.isoformat(), "target_start": day,
            "target_end": (start + timedelta(days=6)).isoformat(),
            "property_identity": identity or council.property_identity(property_config()),
            "collections": [{"kind": kind, "date": day, "usual_date": day}
                            for kind in ("recycling", "food", "general")],
            "general_this_week": True, "general_next_date": day, "warnings": []}


class Response:
    status = 200

    def __init__(self, value):
        self.body = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")

    def read(self, limit=-1):
        return self.body[:limit] if limit >= 0 else self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeTelegram:
    def __init__(self, failure=None, receipt_change=None):
        self._bot_id = ACORN_ID
        self.calls = []
        self.failure = failure
        self.receipt_change = receipt_change
        self.next_id = 100

    def verify(self):
        return {"username": ACORN_USERNAME, "can_pin_messages": True}

    def call(self, method, payload):
        self.calls.append((method, copy.deepcopy(payload)))
        if self.failure:
            failure = self.failure(method, payload)
            if failure:
                raise failure
        if method in {"sendMessage", "sendRichMessage"}:
            self.next_id += 1
            result = {"message_id": self.next_id, "date": int(NOW.timestamp()),
                      "chat": {"id": bot.CHAT_ID}, "message_thread_id": bot.TOPIC_ID}
            if method == "sendRichMessage":
                result["rich_message"] = payload["rich_message"]
            if self.receipt_change:
                self.receipt_change(result)
            return result
        return True


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="offline-tests-", dir=SRC)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.state = self.directory / "state.json"
        self.cache = self.directory / "cache.json"
        self.property_path = self.directory / "property.json"
        self.property_path.write_text(json.dumps(property_config()))
        self.token_path = self.directory / "synthetic-token"
        self.token_path.write_text(f"{ACORN_ID}:synthetic_offline_value\n")
        self.env = {"TELEGRAM_BOT_TOKEN_FILE": str(self.token_path), "BINS_STATE_PATH": str(self.state),
                    "BINS_CACHE_PATH": str(self.cache), "BINS_PROPERTY_PATH": str(self.property_path)}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env).start()
        # Any accidental live request fails before opening a socket.
        patch.object(bot, "build_opener", side_effect=AssertionError("Offline test attempted a request.")).start()
        patch.object(council, "build_opener", side_effect=AssertionError("Offline test attempted a request.")).start()

    def read_state(self):
        return json.loads(self.state.read_text())

    def fetch(self, raw=None, config=None, now=NOW, **kwargs):
        raw = raw or sources(config)
        def fake_fetch(url, data=None):
            return raw[url.split("?", 1)[0]]
        with patch.object(council, "_fetch", side_effect=fake_fetch) as fetch:
            result = council.fetch_schedule(now, config or property_config(), **kwargs)
        return result, fetch

    def main(self, args, now=NOW):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz) if tz else now.replace(tzinfo=None)
        stream = io.StringIO()
        with patch.object(bot, "datetime", Clock), contextlib.redirect_stdout(stream):
            code = bot.main(args)
        return code, json.loads(stream.getvalue())


class TokenAndTransportTests(RuntimeCase):
    def test_token_comes_only_from_file(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "ignored", "ZEROCLAW_CONFIG_PATH": "never-open"}):
            client = bot.Telegram()
        self.assertEqual(client._token, self.token_path.read_text().strip())
        self.assertEqual(client._bot_id, ACORN_ID)
        self.assertIs(type(client._bot_id), int)
        self.assertNotIn(client._token, repr(client))

    def test_bot_id_follows_mounted_token_prefix(self):
        for prefix, expected in (("123001", 123001), ("987002", 987002), ("000987002", 987002)):
            self.token_path.write_text(f"{prefix}:synthetic_offline_value\n")
            with self.subTest(prefix=prefix):
                self.assertEqual(bot.Telegram()._bot_id, expected)

    def test_default_token_path_without_fallback(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "open", side_effect=OSError) as opened:
            with self.assertRaises(bot.BotError):
                bot.Telegram()
        self.assertEqual(opened.call_args_list[0].args, ("r",))
        # Capture the bound path separately without opening the real mount.
        seen = []
        def read(path, *args, **kwargs):
            seen.append(str(path))
            raise OSError
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "ignored"}, clear=True), patch.object(Path, "open", read):
            with self.assertRaises(bot.BotError):
                bot.Telegram()
        self.assertEqual(seen, ["/var/run/acorn/token"])

    def test_missing_file_does_not_use_environment_or_configuration(self):
        self.token_path.unlink()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "999000:ignored", "ZEROCLAW_CONFIG_PATH": str(self.property_path)}):
            with self.assertRaises(bot.BotError) as error:
                bot.Telegram()
        self.assertNotIn(str(self.token_path), str(error.exception))

    def test_invalid_and_oversize_tokens_are_rejected(self):
        for text in ("", "not a token", "123:abc\nextra", "9:" + "x" * 600, "9:abc" + " " * 600):
            with self.subTest(size=len(text)):
                self.token_path.write_text(text)
                with self.assertRaises(bot.BotError):
                    bot.Telegram()

    def test_exact_method_allowlist(self):
        self.assertEqual(bot.Telegram.METHODS, {"getMe", "getChatMember", "sendMessage", "sendRichMessage",
                                               "pinChatMessage", "unpinChatMessage", "deleteMessage"})
        client = bot.Telegram()
        forbidden = ("getUpdates", "setWebhook", "deleteWebhook", "getWebhookInfo", "setMyCommands",
                     "deleteMyCommands", "unpinAllChatMessages", "unpinAllForumTopicMessages",
                     "createForumTopic", "editForumTopic", "sendPhoto", "forwardMessage", "editMessageText")
        for method in forbidden:
            with self.subTest(method=method), self.assertRaises(bot.BotError):
                client.call(method, {"chat_id": bot.CHAT_ID})
        bot.build_opener.assert_not_called()

    def test_wrong_chat_topic_and_override_fields_fail_before_network(self):
        client = bot.Telegram()
        valid = {"chat_id": bot.CHAT_ID, "message_thread_id": bot.TOPIC_ID, "rich_message": {"html": "<h1>Test</h1>"}}
        for change in ({"chat_id": 1}, {"chat_id": str(bot.CHAT_ID)}, {"message_thread_id": 210},
                       {"message_thread_id": "45"}, {"business_connection_id": "other"},
                       {"reply_parameters": {"message_id": 1}}):
            with self.subTest(fields=list(change)), self.assertRaises(bot.BotError):
                client.call("sendRichMessage", {**valid, **change})
        for method in ("pinChatMessage", "unpinChatMessage", "deleteMessage", "getChatMember"):
            with self.subTest(method=method), self.assertRaises(bot.BotError):
                client.call(method, {"chat_id": 1, "message_id": 1})
        bot.build_opener.assert_not_called()

    def test_cleanup_needs_explicit_positive_id(self):
        client = bot.Telegram()
        for method in ("pinChatMessage", "unpinChatMessage", "deleteMessage"):
            for value in (None, 0, -1, True, "1"):
                with self.subTest(method=method, value=value), self.assertRaises(bot.BotError):
                    client.call(method, {"chat_id": bot.CHAT_ID, "message_id": value})
        bot.build_opener.assert_not_called()

    def test_payloads_use_post_rich_and_html_only(self):
        client = bot.Telegram()
        fake = FakeTelegram()
        bot.send_notice(fake, "<h1>Test</h1>", rich=True)
        payload = fake.calls[0][1]
        envelope = {"ok": True, "result": {"accepted": True}}
        opener = Mock()
        opener.open.return_value = Response(envelope)
        with patch.object(bot, "build_opener", return_value=opener):
            self.assertEqual(client.call("sendRichMessage", payload), {"accepted": True})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(payload["rich_message"], {"html": "<h1>Test</h1>"})
        bot.send_notice(fake, "<b>Alert</b>")
        self.assertEqual(fake.calls[-1][0], "sendMessage")
        self.assertEqual(fake.calls[-1][1]["parse_mode"], "HTML")

    def test_network_failure_is_uncertain_safe_and_not_retried(self):
        client = bot.Telegram()
        opener = Mock()
        opener.open.side_effect = URLError("private-address https://invalid.example/" + client._token)
        with patch.object(bot, "build_opener", return_value=opener):
            with self.assertRaises(bot.TelegramError) as error:
                client.call("getMe", {})
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn("https://", str(error.exception))
        self.assertNotIn(client._token, str(error.exception))
        self.assertNotIn("private-address", str(error.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_api_description_is_never_exposed(self):
        client = bot.Telegram()
        opener = Mock()
        opener.open.return_value = Response({"ok": False, "description": "private-address " + client._token})
        with patch.object(bot, "build_opener", return_value=opener), self.assertRaises(bot.TelegramError) as error:
            client.call("getMe", {})
        self.assertNotIn(client._token, str(error.exception))
        self.assertNotIn("private-address", str(error.exception))
        self.assertFalse(error.exception.uncertain)

    def test_malformed_response_and_http_error_are_uncertain(self):
        for value in ([], {"ok": "yes"}, {"ok": True}, b"not-json"):
            opener = Mock()
            opener.open.return_value = Response(value)
            with self.subTest(kind=type(value).__name__), patch.object(bot, "build_opener", return_value=opener):
                with self.assertRaises(bot.TelegramError) as error:
                    bot.Telegram().call("getMe", {})
                self.assertTrue(error.exception.uncertain)
        opener.open.side_effect = HTTPError("https://invalid.example/private", 502, "private", {}, io.BytesIO(b"private"))
        with patch.object(bot, "build_opener", return_value=opener), self.assertRaises(bot.TelegramError) as error:
            bot.Telegram().call("getMe", {})
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn("private", str(error.exception))

    def test_redirects_are_blocked_for_both_clients(self):
        for module, exception in ((bot, bot.BotError), (council, council.CouncilError)):
            with self.subTest(module=module.__name__), self.assertRaises(exception):
                module._NoRedirect().redirect_request(None, None, 302, "", {}, "https://invalid.example/private")

    def test_verification_checks_identity_and_rights_without_updates(self):
        client = bot.Telegram()
        me = {"id": ACORN_ID, "username": ACORN_USERNAME, "is_bot": True}
        member = {"user": {"id": me["id"]}, "status": "administrator", "can_pin_messages": True,
                  "can_delete_messages": True}
        with patch.object(client, "call", side_effect=[me, member]) as call:
            self.assertEqual(client.verify(), {"username": ACORN_USERNAME, "can_pin_messages": True,
                                               "can_delete_messages": True})
        self.assertEqual([c.args for c in call.call_args_list], [
            ("getMe", {}), ("getChatMember", {"chat_id": bot.CHAT_ID, "user_id": ACORN_ID})])
        for wrong in ({**me, "is_bot": False}, {**me, "is_bot": 1}, {**me, "id": True},
                      {**me, "id": float(ACORN_ID)}, {**me, "id": str(ACORN_ID)}):
            with patch.object(client, "call", return_value=wrong), self.assertRaises(bot.BotError):
                client.verify()
        for wrong in ({**member, "can_pin_messages": False}, {**member, "user": {"id": 1}},
                      {**member, "status": "member"}, None):
            with patch.object(client, "call", side_effect=[me, wrong]), self.assertRaises(bot.BotError):
                client.verify()

    def test_verification_accepts_acorn_username_changes(self):
        client = bot.Telegram()
        member = {"user": {"id": ACORN_ID}, "status": "creator"}
        for username in (ACORN_USERNAME, "renamed_acorn_test_bot"):
            me = {"id": ACORN_ID, "username": username, "is_bot": True}
            with self.subTest(username=username), patch.object(client, "call", side_effect=[me, member]):
                self.assertEqual(client.verify(), {"username": username, "can_pin_messages": True,
                                                   "can_delete_messages": False})

    def test_verification_rejects_mismatched_numeric_id(self):
        client = bot.Telegram.__new__(bot.Telegram)
        client._bot_id = ACORN_ID
        me = {"id": ACORN_ID + 1, "username": ACORN_USERNAME, "is_bot": True}
        with patch.object(client, "call", return_value=me) as call, self.assertRaises(bot.BotError):
            client.verify()
        call.assert_called_once_with("getMe", {})

    def test_verification_omits_unavailable_or_invalid_username(self):
        client = bot.Telegram.__new__(bot.Telegram)
        client._bot_id = ACORN_ID
        member = {"user": {"id": ACORN_ID}, "status": "administrator", "can_pin_messages": True}
        for fields in ({}, {"username": None}, {"username": True}, {"username": ACORN_ID},
                       {"username": []}, {"username": ""}, {"username": "tiny"},
                       {"username": "x" * 33}, {"username": "<unsafe>"},
                       {"username": "invalid username"}, {"username": "999000:synthetic_offline_value"}):
            me = {"id": ACORN_ID, "is_bot": True, **fields}
            with self.subTest(fields=fields), patch.object(client, "call", side_effect=[me, member]):
                self.assertEqual(client.verify(), {"can_pin_messages": True, "can_delete_messages": False})


class PropertyAndCouncilTests(RuntimeCase):
    def test_private_property_file_and_default_path(self):
        self.assertEqual(council.load_property(), property_config())
        seen = []
        def read(path, **kwargs):
            seen.append(str(path))
            raise OSError
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "read_text", read):
            with self.assertRaises(council.CouncilError):
                council.load_property()
        self.assertEqual(seen, ["/etc/bins/property.json"])

    def test_schema_is_closed_and_internally_consistent(self):
        values = [None, {}, {**property_config(), "extra": True}]
        for field in council.PROPERTY_FIELDS:
            value = property_config()
            del value["property"][field]
            values.append(value)
        for field in council.PROPERTY_FIELDS:
            value = property_config()
            value["property"][field] = 9001
            values.append(value)
        value = property_config()
        value["property"]["Postcode"] = "ZZ2 2ZZ"
        values.append(value)
        values.extend([{**property_config(), "address_label": "wrong"},
                       {**property_config(), "address_label": property_config()["address_label"] + "\n"}])
        for value in values:
            with self.subTest(kind=type(value).__name__), self.assertRaises(council.CouncilError):
                council.validate_property(value)

    def test_duplicate_json_keys_fail_closed(self):
        self.property_path.write_text('{"address_label":"one","address_label":"two","property":{}}')
        with self.assertRaises(council.CouncilError):
            council.load_property()
        for text in ('{"key":1,"key":2}', '{"key":NaN}'):
            with self.assertRaises(ValueError):
                storage.strict_json(text)

    def test_synthetic_exact_address_and_property_match(self):
        config = property_config()
        label = config["address_label"]
        self.assertEqual(council._address(json.dumps([label]), config), label)
        spaced = label.lower().replace(" - ", "  -  ")
        self.assertEqual(council._address(json.dumps([spaced]), config), spaced)
        for candidates in ([], [label, label], ["1" + label], [label.replace("9001", "9002")],
                           [label.replace("ZZ1 1ZZ", "ZZ2 2ZZ")]):
            with self.subTest(count=len(candidates)), self.assertRaises(council.CouncilError) as error:
                council._address(json.dumps(candidates), config)
            self.assertNotIn(label, str(error.exception))
        details = {**config["property"], "Day": "Monday", "Frequency": "Week A"}
        for field in council.PROPERTY_FIELDS:
            for replacement in (None, "other"):
                with self.subTest(field=field), self.assertRaises(council.CouncilError):
                    council._property(json.dumps({**details, field: replacement}), config)
        self.assertEqual(council._property(json.dumps(details), config), ("Monday", "Week A"))

    def test_subproperty_requires_explicit_identity_and_exact_label(self):
        config = property_config()
        config["property"]["sao"] = "Test Unit X"
        config["address_label"] = "Test Unit X " + config["address_label"]
        council.validate_property(config)
        result, _ = self.fetch(config=config)
        self.assertEqual(result["property_identity"], council.property_identity(config))
        wrong = sources(config)
        details = json.loads(wrong[council.PROPERTY_URL])
        details["sao"] = "Test Unit Y"
        wrong[council.PROPERTY_URL] = json.dumps(details)
        with self.assertRaises(council.CouncilError):
            self.fetch(wrong, config=config)

    def test_each_fresh_source_failure_keeps_cache_unchanged_without_fallback(self):
        self.fetch(cache_path=self.cache)
        before = self.cache.read_bytes()
        raw = sources()
        for failing_source in council.SOURCES:
            def fail(url, data=None):
                key = url.split("?", 1)[0]
                if key == failing_source:
                    raise council.CouncilError("Synthetic source failure.")
                return raw[key]
            with self.subTest(source_index=council.SOURCES.index(failing_source)), \
                 patch.object(council, "_fetch", side_effect=fail), self.assertRaises(council.CouncilError):
                council.fetch_schedule(NOW, property_config(), self.cache, force=True)
            self.assertEqual(self.cache.read_bytes(), before)

    def test_cache_does_not_cross_london_week_at_utc_boundary(self):
        sunday = datetime(2026, 10, 18, 22, 59, tzinfo=timezone.utc)
        self.fetch(now=sunday, cache_path=self.cache)
        monday = datetime(2026, 10, 18, 23, 1, tzinfo=timezone.utc)
        _, fetch = self.fetch(now=monday, cache_path=self.cache, force=False)
        self.assertEqual(fetch.call_count, 5)

    def test_bad_property_or_address_prevents_cache_replacement(self):
        self.fetch(cache_path=self.cache)
        before = self.cache.read_bytes()
        for key in (council.ADDRESS_URL, council.PROPERTY_URL):
            raw = sources()
            raw[key] = sources(property_config("9002"))[key]
            with self.subTest(source_index=council.SOURCES.index(key)), self.assertRaises(council.CouncilError):
                self.fetch(raw, cache_path=self.cache)
            self.assertEqual(self.cache.read_bytes(), before)

    def test_wrong_collection_day_or_frequency_fails_closed(self):
        details = {**property_config()["property"], "Day": "Monday", "Frequency": "Week A"}
        for change in ({"Day": "Tomorrow"}, {"Frequency": "Fortnightly"}, {"Day": None}):
            with self.assertRaises(council.CouncilError):
                council._property(json.dumps({**details, **change}), property_config())

    def test_fetch_uses_only_configured_lookup_and_five_sources(self):
        raw = sources()
        seen = []
        def fetch(url, data=None):
            seen.append((url, data))
            return raw[url.split("?", 1)[0]]
        with patch.object(council, "_fetch", side_effect=fetch):
            result = council.fetch_schedule(NOW, property_config())
        self.assertEqual(len(seen), 5)
        self.assertEqual(parse_qs(urlsplit(seen[0][0]).query), {"term": ["ZZ1 1ZZ"]})
        self.assertEqual(parse_qs(seen[1][1].decode()), {"address": [property_config()["address_label"]]})
        self.assertEqual(result["target_start"], "2026-10-26")
        self.assertEqual([x["date"] for x in result["collections"]], ["2026-10-26"] * 3)
        self.assertNotIn("address", result)
        self.assertTrue(all("?" not in url for url in result["sources"]))
        self.assertNotIn("ZZ1", json.dumps(result))

    def test_week_a_b_membership_and_next_date_are_published(self):
        result, _ = self.fetch(sources(frequency="Week A"))
        self.assertFalse(result["general_this_week"])
        self.assertEqual(result["general_next_date"], "2026-11-02")
        self.assertEqual([x["kind"] for x in result["collections"]], ["recycling", "food"])
        result, _ = self.fetch(sources(frequency="Weekly"))
        self.assertTrue(result["general_this_week"])

    def test_missing_target_and_future_calendar_never_extrapolate(self):
        raw = sources()
        raw[council.BLACK_URL] = calendar_html(count=1)
        with self.assertRaises(council.CouncilError):
            self.fetch(raw)
        raw[council.BLACK_URL] = calendar_html(count=2)
        result, _ = self.fetch(raw, config=property_config())
        self.assertIsNotNone(result["general_next_date"])
        raw[council.PROPERTY_URL] = sources(frequency="Week A")[council.PROPERTY_URL]
        result, _ = self.fetch(raw)
        self.assertIsNone(result["general_next_date"])
        self.assertTrue(result["warnings"])

    def test_calendar_requires_year_valid_weekdays_and_no_gaps_or_conflicts(self):
        valid = calendar_html()
        bad = [valid.replace("2026</h1>", "</h1>"),
               valid.replace("Monday 19 October", "Tuesday 19 October"),
               calendar_html(count=1) + calendar_html(start=date(2026, 11, 2), count=1),
               valid + valid.replace("Week A", "TEMP").replace("Week B", "Week A").replace("TEMP", "Week B"),
               "<h1>2026</h1><table><tr><td>unknown</td></tr></table>"]
        for text in bad:
            with self.subTest(size=len(text)), self.assertRaises(council.CouncilError):
                council._calendar(text)

    def test_html_parser_inline_markup_optional_end_tags_and_bad_structure(self):
        html = calendar_html(count=2).replace("Monday 19", "<strong>Monday</strong> 19")
        html = html.replace("</td>", "").replace("</th>", "").replace("</tr>", "")
        self.assertEqual(len(council._calendar(html)), 2)
        for text in ("<table><table></table></table>", "<table><tr><td>x", "<table><tr><td colspan='2'>x</td></tr></table>"):
            with self.assertRaises(council.CouncilError):
                council._Tables(text)

    def test_holiday_mapping_is_one_step_not_chained(self):
        mapping, _ = council._holiday_mapping(holiday_html([
            ("Monday 26 October 2026", "Tuesday 27 October 2026"),
            ("Tuesday 27 October 2026", "Wednesday 28 October 2026")]), 2026)
        actual, _ = council._collection_date(date(2026, 10, 26), mapping, set())
        self.assertEqual(actual, "2026-10-27")

    def test_holiday_without_mapping_is_unknown_including_backlog(self):
        for bank in ("2026-10-26", "2026-10-19"):
            raw = sources(banks=["2026-01-01", bank])
            result, _ = self.fetch(raw)
            self.assertTrue(all(x["date"] is None for x in result["collections"]))
            self.assertIsNone(result["general_next_date"])
            self.assertTrue(result["warnings"])

    def test_bank_data_must_cover_collection_period(self):
        with self.assertRaises(council.CouncilError):
            council._collection_date(date(2027, 1, 4), {}, {date(2027, 1, 1)})
        for text in ("{}", '{"england-and-wales":{"division":"other","events":[]}}'):
            with self.assertRaises(council.CouncilError):
                council._bank_holidays(text)

    def test_holiday_rollover_and_stale_yearless_table(self):
        mapping, warnings = council._holiday_mapping(holiday_html([
            ("Friday 25 December", "Monday 28 December"),
            ("Friday 1 January", "Monday 4 January")], year="2026"), 2026)
        self.assertEqual(mapping[date(2027, 1, 1)], date(2027, 1, 4))
        self.assertFalse(warnings)
        mapping, warnings = council._holiday_mapping(holiday_html([
            ("Thursday 25 December", "Saturday 27 December")], year=""), 2026)
        self.assertEqual(mapping, {})
        self.assertTrue(warnings)

    def test_holiday_ambiguous_conflicting_or_wrong_weekdays_fail(self):
        values = [holiday_html([("Monday 26 October 2026", "Monday 27 October 2026")]),
                  holiday_html([("Monday 26 October 2026", "Monday 16 November 2026")]),
                  holiday_html() + holiday_html([("Friday 25 December 2026", "Tuesday 29 December 2026")]),
                  "<h1>2026</h1><table><tr><td>unknown</td></tr></table>"]
        for text in values:
            with self.subTest(size=len(text)), self.assertRaises(council.CouncilError):
                council._holiday_mapping(text, 2026)

    def test_cache_requires_exact_property_identity(self):
        self.fetch(cache_path=self.cache)
        result, fetch = self.fetch(cache_path=self.cache, force=False)
        fetch.assert_not_called()
        self.assertEqual(result["fetched_at"], NOW.isoformat())
        different = property_config("9002")
        result, fetch = self.fetch(config=different, cache_path=self.cache, force=False)
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(result["property_identity"], council.property_identity(different))
        cached = json.loads(self.cache.read_text())
        self.assertEqual(cached["property_identity"], council.property_identity(different))
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)

    def test_cache_identity_includes_label_and_all_fields(self):
        base = property_config()
        value = copy.deepcopy(base)
        value["property"]["streetName"] = "EXAMPLE TEST STREET"
        value["address_label"] = value["address_label"].replace("Example Test Street", "EXAMPLE TEST STREET")
        self.assertNotEqual(council.property_identity(base), council.property_identity(value))

    def test_force_failure_never_falls_back_to_even_fresh_cache(self):
        self.fetch(cache_path=self.cache)
        before = self.cache.read_bytes()
        with patch.object(council, "_fetch", side_effect=council.CouncilError("Synthetic source failure.")) as fetch:
            with self.assertRaises(council.CouncilError):
                council.fetch_schedule(NOW, property_config(), cache_path=self.cache, force=True)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.cache.read_bytes(), before)

    def test_old_future_wrong_version_and_wrong_week_caches_are_not_used(self):
        self.fetch(cache_path=self.cache)
        original = json.loads(self.cache.read_text())
        for change in ({"fetched_at": (NOW - timedelta(days=7)).isoformat()},
                       {"fetched_at": (NOW + timedelta(seconds=1)).isoformat()}, {"version": 1},
                       {"fetched_at": (NOW - timedelta(days=1)).isoformat()}, {"raw": {}},
                       {"fetched_at": NOW.replace(tzinfo=None).isoformat()}):
            self.cache.write_text(json.dumps({**original, **change}))
            _, fetch = self.fetch(cache_path=self.cache, force=False)
            self.assertEqual(fetch.call_count, 5)

    def test_cached_responses_are_revalidated_and_never_used_for_other_property(self):
        self.fetch(cache_path=self.cache)
        cached = json.loads(self.cache.read_text())
        cached["raw"][council.PROPERTY_URL] = sources(property_config("9002"))[council.PROPERTY_URL]
        self.cache.write_text(json.dumps(cached))
        with patch.object(council, "_fetch") as fetch, self.assertRaises(council.CouncilError):
            council.fetch_schedule(NOW, property_config(), cache_path=self.cache, force=False)
        fetch.assert_not_called()

    def test_http_fetch_headers_limits_and_safe_errors(self):
        opener = Mock()
        opener.open.return_value = Response(b"safe body")
        with patch.object(council, "build_opener", return_value=opener):
            self.assertEqual(council._fetch(council.BLACK_URL), "safe body")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Cache-control"), "no-cache")
        self.assertEqual(request.get_header("User-agent"), "Mozilla/5.0")
        for failure in (URLError("https://invalid.example/private-address"), UnicodeError("private-address")):
            opener.open.side_effect = failure
            with patch.object(council, "build_opener", return_value=opener), self.assertRaises(council.CouncilError) as error:
                council._fetch("https://invalid.example/private-address")
            self.assertNotIn("private-address", str(error.exception))
            self.assertNotIn("https://", str(error.exception))
        opener.open.side_effect = None
        opener.open.return_value = Response(b"x" * 4_000_001)
        with patch.object(council, "build_opener", return_value=opener), self.assertRaises(council.CouncilError):
            council._fetch(council.BLACK_URL)

    def test_parser_errors_do_not_include_source_text(self):
        for function, args in ((council._date_parts, ("private-address",)),
                               (council._calendar_range, ("private-address", 2026)),
                               (council._json, ("private-address", "private-address"))):
            with self.assertRaises(council.CouncilError) as error:
                function(*args)
            self.assertNotIn("private-address", str(error.exception))


class PublicationTests(RuntimeCase):
    def test_publish_native_rich_once_and_pin(self):
        client = FakeTelegram()
        self.assertEqual(bot.publish(client, schedule(), NOW, self.state)["status"], "published")
        self.assertEqual(bot.publish(client, schedule(), NOW, self.state)["status"], "already_published")
        self.assertEqual([m for m, _ in client.calls], ["sendRichMessage", "pinChatMessage"])
        self.assertEqual(self.read_state()["current"]["message_id"], 101)
        self.assertEqual(bot.SENDER, "acorn-care-bot")
        self.assertEqual(self.read_state()["sender"], bot.SENDER)
        self.assertEqual(self.read_state()["destination"], bot.DESTINATION)
        self.assertEqual(self.read_state()["property_identity"], schedule()["property_identity"])
        self.assertNotIn("bot_username", self.read_state())
        self.assertNotIn(ACORN_USERNAME, self.state.read_text())
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(str(self.state) + ".lock").stat().st_mode), 0o600)
        self.assertNotIn("address_label", self.state.read_text())

    def test_unknown_send_blocks_same_and_future_weeks_and_resume(self):
        client = FakeTelegram(failure=lambda m, p: bot.TelegramError("Synthetic unknown result.", uncertain=True)
                              if m == "sendRichMessage" else None)
        with self.assertRaises(bot.TelegramError):
            bot.publish(client, schedule(), NOW, self.state)
        self.assertEqual(self.read_state()["publications"]["2026-10-26"]["status"], "needs_review")
        for now in (NOW, NOW + timedelta(days=7)):
            with self.assertRaises(bot.BotError):
                bot.resume(client, now, self.state, schedule()["property_identity"])
            with self.assertRaises(bot.BotError):
                bot.publish(client, schedule(now), now, self.state)
        self.assertEqual(len(client.calls), 1)

    def test_definite_send_rejection_is_also_not_retried(self):
        client = FakeTelegram(failure=lambda m, p: bot.TelegramError("Synthetic rejection.") if m == "sendRichMessage" else None)
        with self.assertRaises(bot.TelegramError):
            bot.publish(client, schedule(), NOW, self.state)
        with self.assertRaises(bot.BotError):
            bot.publish(client, schedule(), NOW, self.state)
        self.assertEqual(len(client.calls), 1)

    def test_crash_claim_without_receipt_blocks_delivery(self):
        class SimulatedCrash(BaseException):
            pass
        client = FakeTelegram(failure=lambda m, p: SimulatedCrash() if m == "sendRichMessage" else None)
        with self.assertRaises(SimulatedCrash):
            bot.publish(client, schedule(), NOW, self.state)
        self.assertEqual(self.read_state()["publications"]["2026-10-26"]["status"], "sending")
        with self.assertRaises(bot.BotError):
            bot.resume(FakeTelegram(), NOW, self.state, schedule()["property_identity"])

    def test_incomplete_pin_resumes_without_send_and_without_fresh_sources(self):
        client = FakeTelegram(failure=lambda m, p: bot.TelegramError("Synthetic pin failure.", uncertain=True)
                              if m == "pinChatMessage" else None)
        with self.assertRaises(bot.TelegramError):
            bot.publish(client, schedule(), NOW, self.state)
        self.assertEqual(self.read_state()["publications"]["2026-10-26"]["status"], "sent")
        client.failure = None
        self.assertEqual(bot.resume(client, NOW, self.state, schedule()["property_identity"])["status"], "published")
        self.assertEqual([m for m, _ in client.calls].count("sendRichMessage"), 1)
        self.assertEqual(bot.resume(client, NOW, self.state, schedule()["property_identity"])["status"], "already_published")

    def test_wrong_receipts_are_uncertain_and_never_pinned_or_resent(self):
        changes = [lambda r: r.update(message_thread_id=210), lambda r: r.update(chat={"id": 1}),
                   lambda r: r.pop("rich_message"), lambda r: r.update(message_id=True),
                   lambda r: r.pop("date"), lambda r: r.update(date="not-an-integer")]
        for i, change in enumerate(changes):
            path = self.directory / f"receipt-{i}.json"
            client = FakeTelegram(receipt_change=change)
            with self.assertRaises(bot.TelegramError) as error:
                bot.publish(client, schedule(), NOW, path)
            self.assertTrue(error.exception.uncertain)
            with self.assertRaises(bot.BotError):
                bot.publish(client, schedule(), NOW, path)
            self.assertEqual([m for m, _ in client.calls], ["sendRichMessage"])

    def test_reused_message_identifier_is_unknown_and_is_not_pinned(self):
        client = FakeTelegram()
        bot.publish(client, schedule(), NOW, self.state)
        later = NOW + timedelta(days=7)
        reused = FakeTelegram()
        with self.assertRaises(bot.TelegramError) as error:
            bot.publish(reused, schedule(later), later, self.state)
        self.assertTrue(error.exception.uncertain)
        self.assertEqual([method for method, _ in reused.calls], ["sendRichMessage"])
        with self.assertRaises(bot.BotError):
            bot.resume(reused, later, self.state, schedule()["property_identity"])

    def test_false_pin_result_resumes_without_a_second_send(self):
        client = FakeTelegram()
        real_call = client.call
        def false_pin(method, payload):
            if method == "pinChatMessage":
                return False
            return real_call(method, payload)
        with patch.object(client, "call", side_effect=false_pin), self.assertRaises(bot.TelegramError):
            bot.publish(client, schedule(), NOW, self.state)
        bot.resume(client, NOW, self.state, schedule()["property_identity"])
        self.assertEqual([method for method, _ in client.calls].count("sendRichMessage"), 1)

    def test_old_incomplete_publication_resumes_before_a_new_week(self):
        client = FakeTelegram(failure=lambda m, p: bot.TelegramError("Pin interrupted.") if m == "pinChatMessage" else None)
        with self.assertRaises(bot.TelegramError):
            bot.publish(client, schedule(), NOW, self.state)
        client.failure = None
        later = NOW + timedelta(days=7)
        resumed = bot.resume(client, later, self.state, schedule()["property_identity"])
        self.assertIsNone(resumed)
        self.assertEqual(self.read_state()["publications"]["2026-10-26"]["status"], "complete")
        self.assertEqual([method for method, _ in client.calls].count("sendRichMessage"), 1)

    def test_definite_rejected_old_pin_does_not_block_later_notices(self):
        client = FakeTelegram(failure=lambda m, p: bot.TelegramError("Pin rejected.") if m == "pinChatMessage" else None)
        with self.assertRaises(bot.TelegramError):
            bot.publish(client, schedule(), NOW, self.state)
        for offset in (7, 14):
            later = NOW + timedelta(days=offset)
            client.failure = lambda m, p: bot.TelegramError("Deleted old notice.") if m == "pinChatMessage" and p["message_id"] == 101 else None
            with patch.object(bot, "Telegram", return_value=client), patch.object(bot, "fetch_schedule", return_value=schedule(later)) as fetch:
                code, result = self.main(["run"], later)
            self.assertEqual((code, result["status"]), (0, "published"))
            fetch.assert_called_once()
        state = self.read_state()
        self.assertEqual(state["publications"]["2026-10-26"]["status"], "superseded")
        self.assertEqual(state["publications"]["2026-10-26"]["message_id"], 101)
        self.assertEqual(state["current"]["target_start"], "2026-11-09")
        self.assertEqual([m for m, _ in client.calls].count("sendRichMessage"), 3)
        self.assertFalse(any(m in {"unpinChatMessage", "deleteMessage"} and p["message_id"] == 101 for m, p in client.calls))
        self.assertEqual(self.main(["status"])[1]["publications"]["2026-10-26"]["status"], "superseded")

    def test_current_target_pin_rejection_is_not_superseded_or_resent(self):
        client = FakeTelegram(failure=lambda m, p: bot.TelegramError("Pin rejected.") if m == "pinChatMessage" else None)
        with self.assertRaises(bot.TelegramError):
            bot.publish(client, schedule(), NOW, self.state)
        with self.assertRaises(bot.TelegramError):
            bot.resume(client, NOW, self.state, schedule()["property_identity"])
        self.assertEqual(self.read_state()["publications"]["2026-10-26"]["status"], "sent")
        self.assertEqual([m for m, _ in client.calls].count("sendRichMessage"), 1)

    def test_unknown_old_pin_is_not_superseded(self):
        client = FakeTelegram(failure=lambda m, p: bot.TelegramError("Unknown pin.", uncertain=True) if m == "pinChatMessage" else None)
        with self.assertRaises(bot.TelegramError):
            bot.publish(client, schedule(), NOW, self.state)
        later = NOW + timedelta(days=7)
        with patch.object(bot, "Telegram", return_value=client), patch.object(bot, "fetch_schedule") as fetch:
            code, result = self.main(["run"], later)
        self.assertEqual(code, 1)
        fetch.assert_not_called()
        self.assertEqual(self.read_state()["publications"]["2026-10-26"]["status"], "sent")
        self.assertEqual([m for m, _ in client.calls].count("sendRichMessage"), 1)

    def test_superseded_send_receipt_still_prevents_duplicate_send(self):
        client = FakeTelegram(failure=lambda m, p: bot.TelegramError("Rejected pin.") if m == "pinChatMessage" else None)
        with self.assertRaises(bot.TelegramError):
            bot.publish(client, schedule(), NOW, self.state)
        bot.resume(client, NOW + timedelta(days=7), self.state, schedule()["property_identity"])
        result = bot.publish(client, schedule(), NOW, self.state)
        self.assertEqual(result["status"], "already_published")
        self.assertFalse(result["pinned"])
        self.assertEqual([m for m, _ in client.calls].count("sendRichMessage"), 1)

    def test_state_is_bound_to_destination_sender_property_and_version(self):
        bot.publish(FakeTelegram(), schedule(), NOW, self.state)
        original = self.read_state()
        for change in ({"destination": f"{bot.CHAT_ID}:210"}, {"sender": "other-test-sender"},
                       {"property_identity": "a" * 64}, {"version": 1}):
            self.state.write_text(json.dumps({**original, **change}))
            client = FakeTelegram()
            with self.assertRaises(bot.BotError):
                bot.publish(client, schedule(), NOW, self.state)
            self.assertEqual(client.calls, [])

    def test_state_binds_numeric_bot_id_before_any_send_or_cleanup(self):
        client = FakeTelegram()
        bot.publish(client, schedule(), NOW, self.state)
        self.assertEqual(self.read_state()["bot_id"], ACORN_ID)
        original = self.state.read_bytes()
        other = FakeTelegram()
        other._bot_id = ACORN_ID + 1
        later = NOW + timedelta(days=7)
        for operation in (lambda: bot.publish(other, schedule(later), later, self.state),
                          lambda: bot.resume(other, later, self.state, schedule()["property_identity"]),
                          lambda: bot.alert(other, later, self.state, schedule()["property_identity"])):
            with self.assertRaises(bot.BotError):
                operation()
        self.assertEqual(other.calls, [])
        self.assertEqual(self.state.read_bytes(), original)

    def test_state_missing_or_invalid_numeric_bot_id_fails_closed(self):
        bot.publish(FakeTelegram(), schedule(), NOW, self.state)
        original = self.read_state()
        values = [{key: value for key, value in original.items() if key != "bot_id"}]
        values += [{**original, "bot_id": value} for value in (None, True, 0, -1, str(ACORN_ID))]
        for value in values:
            self.state.write_text(json.dumps(value))
            client = FakeTelegram()
            with self.assertRaises(bot.BotError):
                bot.resume(client, NOW, self.state, schedule()["property_identity"])
            self.assertEqual(client.calls, [])

    def test_state_requires_sender_instead_of_legacy_username(self):
        bot.publish(FakeTelegram(), schedule(), NOW, self.state)
        value = self.read_state()
        del value["sender"]
        value["bot_username"] = ACORN_USERNAME
        self.state.write_text(json.dumps(value))
        client = FakeTelegram()
        with self.assertRaises(bot.BotError):
            bot.publish(client, schedule(), NOW, self.state)
        self.assertEqual(client.calls, [])

    def test_malformed_state_and_unrecorded_current_fail_closed(self):
        bot.publish(FakeTelegram(), schedule(), NOW, self.state)
        original = self.read_state()
        for value in ([], {}, {**original, "current": {"message_id": 999, "date": int(NOW.timestamp()),
                                                   "target_start": "2026-10-26"}}):
            self.state.write_text(json.dumps(value))
            with self.assertRaises(bot.BotError):
                bot.resume(FakeTelegram(), NOW, self.state, schedule()["property_identity"])
        self.state.write_text("not-json")
        with self.assertRaises(bot.BotError):
            bot.resume(FakeTelegram(), NOW, self.state, schedule()["property_identity"])

    def test_only_own_old_recorded_pin_is_unpinned_and_not_deleted_after_48h(self):
        client = FakeTelegram()
        bot.publish(client, schedule(), NOW, self.state)
        later = NOW + timedelta(days=7)
        bot.publish(client, schedule(later), later, self.state)
        self.assertEqual([m for m, _ in client.calls], ["sendRichMessage", "pinChatMessage",
                                                       "sendRichMessage", "pinChatMessage", "unpinChatMessage"])
        self.assertEqual(client.calls[-1][1]["message_id"], 101)
        self.assertEqual(self.read_state()["current"]["message_id"], 102)

    def test_cleanup_can_delete_only_own_recent_record(self):
        client = FakeTelegram()
        bot.publish(client, schedule(), NOW, self.state)
        later = NOW + timedelta(days=7)
        state = self.read_state()
        recent = int(later.timestamp()) - 3600
        state["publications"]["2026-10-26"]["date"] = recent
        state["current"]["date"] = recent
        storage.atomic_json(self.state, state)
        bot.publish(client, schedule(later), later, self.state)
        self.assertEqual(client.calls[-1], ("deleteMessage", {"chat_id": bot.CHAT_ID, "message_id": 101}))

    def test_unrecorded_previous_pin_is_never_touched(self):
        client = FakeTelegram(failure=lambda m, p: bot.TelegramError("Pin interrupted.") if m == "pinChatMessage" else None)
        with self.assertRaises(bot.TelegramError):
            bot.publish(client, schedule(), NOW, self.state)
        state = self.read_state()
        state["publications"]["2026-10-26"]["previous"] = {"message_id": 999, "date": 1, "target_start": "2026-10-19"}
        storage.atomic_json(self.state, state)
        client.calls.clear()
        with self.assertRaises(bot.BotError):
            bot.resume(client, NOW, self.state, schedule()["property_identity"])
        self.assertEqual(client.calls, [])

    def test_cleanup_failure_resumes_without_resend_or_repin(self):
        client = FakeTelegram()
        bot.publish(client, schedule(), NOW, self.state)
        later = NOW + timedelta(days=7)
        client.failure = lambda m, p: bot.TelegramError("Interrupted cleanup.", uncertain=True) if m == "unpinChatMessage" else None
        result = bot.publish(client, schedule(later), later, self.state)
        self.assertTrue(result["cleanup_pending"])
        self.assertEqual(self.read_state()["publications"]["2026-11-02"]["status"], "complete")
        client.failure = None
        bot.resume(client, later, self.state, schedule()["property_identity"])
        methods = [m for m, _ in client.calls]
        self.assertEqual(methods.count("sendRichMessage"), 2)
        self.assertEqual(methods.count("pinChatMessage"), 2)
        self.assertEqual(methods.count("unpinChatMessage"), 2)

    def test_persistent_unpin_rejection_does_not_block_later_weeks(self):
        client = FakeTelegram()
        bot.publish(client, schedule(), NOW, self.state)
        client.failure = lambda m, p: bot.TelegramError("Rejected cleanup.") if m == "unpinChatMessage" else None
        later = NOW + timedelta(days=7)
        result = bot.publish(client, schedule(later), later, self.state)
        self.assertTrue(result["cleanup_pending"])
        self.assertEqual(self.read_state()["current"]["message_id"], 102)
        following = NOW + timedelta(days=14)
        self.assertIsNone(bot.resume(client, following, self.state, schedule()["property_identity"]))
        result = bot.publish(client, schedule(following), following, self.state)
        self.assertEqual(result["status"], "published")
        self.assertTrue(result["cleanup_pending"])
        state = self.read_state()
        self.assertEqual(state["current"]["message_id"], 103)
        self.assertTrue(all(entry["status"] == "complete" for entry in state["publications"].values()))
        self.assertEqual([m for m, _ in client.calls].count("sendRichMessage"), 3)
        self.assertEqual([m for m, _ in client.calls].count("pinChatMessage"), 3)
        self.assertNotIn("deleteMessage", [m for m, _ in client.calls])

    def test_cleanup_retry_never_reverts_current_pin(self):
        client = FakeTelegram()
        bot.publish(client, schedule(), NOW, self.state)
        client.failure = lambda m, p: bot.TelegramError("Unknown cleanup.", uncertain=True) if m == "unpinChatMessage" else None
        later = NOW + timedelta(days=7)
        bot.publish(client, schedule(later), later, self.state)
        following = NOW + timedelta(days=14)
        bot.publish(client, schedule(following), following, self.state)
        client.failure = None
        result = bot.resume(client, following, self.state, schedule()["property_identity"])
        self.assertEqual(result["status"], "already_published")
        self.assertEqual(self.read_state()["current"]["message_id"], 103)
        self.assertTrue(all(entry.get("cleanup_done") for key, entry in self.read_state()["publications"].items()
                            if key != "2026-10-26"))
        self.assertEqual([m for m, _ in client.calls].count("sendRichMessage"), 3)

    def test_new_pin_is_saved_before_cleanup_failure(self):
        client = FakeTelegram()
        bot.publish(client, schedule(), NOW, self.state)
        later = NOW + timedelta(days=7)
        def inspect(method, payload):
            if method == "unpinChatMessage":
                state = self.read_state()
                self.assertEqual(state["current"]["message_id"], 102)
                self.assertEqual(state["publications"]["2026-11-02"]["status"], "complete")
                return bot.TelegramError("Rejected cleanup.")
        client.failure = inspect
        self.assertTrue(bot.publish(client, schedule(later), later, self.state)["cleanup_pending"])

    def test_atomic_claim_precedes_send(self):
        client = FakeTelegram()
        with patch.object(bot, "atomic_json", side_effect=OSError("Synthetic disk failure.")), self.assertRaises(OSError):
            bot.publish(client, schedule(), NOW, self.state)
        self.assertEqual(client.calls, [])
        def verify_claim(method, payload):
            if method == "sendRichMessage":
                self.assertEqual(self.read_state()["publications"]["2026-10-26"]["status"], "sending")
        client.failure = verify_claim
        bot.publish(client, schedule(), NOW, self.state)

    def test_receipt_save_failure_blocks_automatic_resend(self):
        client = FakeTelegram()
        real = bot.atomic_json
        def fail_receipt(path, state):
            if any(entry["status"] == "sent" for entry in state["publications"].values()):
                raise OSError("Synthetic disk failure.")
            return real(path, state)
        with patch.object(bot, "atomic_json", side_effect=fail_receipt), self.assertRaises(OSError):
            bot.publish(client, schedule(), NOW, self.state)
        with self.assertRaises(bot.BotError):
            bot.resume(client, NOW, self.state, schedule()["property_identity"])
        self.assertEqual([m for m, _ in client.calls], ["sendRichMessage"])

    def test_flock_and_parallel_publish_prevent_duplicate_sends(self):
        with bot.locked_state(self.state, schedule()["property_identity"]):
            with open(str(self.state) + ".lock") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        client = FakeTelegram()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: bot.publish(client, schedule(), NOW, self.state), range(2)))
        self.assertEqual({r["status"] for r in results}, {"published", "already_published"})
        self.assertEqual([m for m, _ in client.calls].count("sendRichMessage"), 1)

    def test_atomic_json_replacement_failure_preserves_old_file_and_removes_temporary(self):
        storage.atomic_json(self.state, {"old": True})
        with patch.object(storage.os, "replace", side_effect=OSError), self.assertRaises(OSError):
            storage.atomic_json(self.state, {"new": True})
        self.assertEqual(json.loads(self.state.read_text()), {"old": True})
        self.assertEqual(list(self.directory.glob("state.json.*")), [])

    def test_wrong_target_week_cannot_create_claim(self):
        value = schedule()
        value["target_start"] = "2026-11-02"
        client = FakeTelegram()
        with self.assertRaises(bot.BotError):
            bot.publish(client, value, NOW, self.state)
        self.assertEqual(client.calls, [])
        self.assertFalse(self.state.exists())

    def test_alert_is_fixed_html_and_never_retried_on_unknown(self):
        client = FakeTelegram(failure=lambda m, p: bot.TelegramError("Unknown.", uncertain=True))
        self.assertFalse(bot.alert(client, NOW, self.state, schedule()["property_identity"]))
        self.assertFalse(bot.alert(client, NOW, self.state, schedule()["property_identity"]))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "sendMessage")
        self.assertIn("@LionZXY", client.calls[0][1]["text"])
        self.assertNotIn(property_config()["address_label"], client.calls[0][1]["text"])


class RichAndCommandTests(RuntimeCase):
    def test_rich_groups_by_date_and_keeps_clickable_sources(self):
        value = schedule()
        value["collections"][0]["date"] = "2026-10-27"
        text = bot.render_rich(value)
        self.assertEqual(text.count("<h1>"), 1)
        self.assertEqual(text.count("<h2>"), 2)
        self.assertEqual(text.count("<ul>"), 2)
        self.assertEqual(text.count("<li>"), 3)
        self.assertLess(text.index("Monday 26 October"), text.index("Tuesday 27 October"))
        self.assertIn("Changed from Monday 26 October 2026", text)
        self.assertIn(f'href="{bot.FINDER_URL}"', text)
        self.assertIn(f'href="{council.HOLIDAY_URL}"', text)
        self.assertNotIn(property_config()["address_label"], text)

    def test_unknown_dates_and_warnings_escape_html_without_guessed_advice(self):
        value = schedule()
        value["collections"][0].update(date=None, note="<unsafe>&")
        value["warnings"] = ["<unsafe>&"]
        value["general_this_week"] = False
        value["general_next_date"] = None
        text = bot.render_rich(value)
        self.assertIn("Collection date not confirmed", text)
        self.assertIn("&lt;unsafe&gt;&amp;", text)
        self.assertNotIn("<unsafe>", text)
        self.assertNotIn("Put your bins out", text)
        self.assertIn("The next black-top date is not confirmed", text)

    def test_rich_period_handles_month_and_year_boundaries(self):
        for first, last, expected in (("2026-12-28", "2027-01-03", "28 December 2026 – 3 January 2027"),
                                      ("2026-11-30", "2026-12-06", "30 November – 6 December 2026")):
            value = schedule()
            value.update(target_start=first, target_end=last)
            self.assertIn(expected, bot.render_rich(value))

    def test_run_schedule_is_monday_noon_london_including_dst(self):
        due = [NOW, datetime(2026, 10, 19, 11, tzinfo=timezone.utc),
               datetime(2026, 10, 26, 12, tzinfo=timezone.utc)]
        not_due = [NOW - timedelta(seconds=1), NOW + timedelta(days=1), NOW - timedelta(days=1),
                   datetime(2026, 10, 26, 11, 59, tzinfo=timezone.utc)]
        with patch.object(bot, "Telegram") as client, patch.object(bot, "load_property") as config:
            for now in not_due:
                code, result = self.main(["run"], now)
                self.assertEqual((code, result["status"]), (0, "not_due"))
            client.assert_not_called()
            config.assert_not_called()
        for now in due:
            with patch.object(bot, "Telegram", return_value=FakeTelegram()), patch.object(bot, "resume", return_value={"status": "already_published"}):
                self.assertEqual(self.main(["run"], now)[1]["status"], "already_published")

    def test_preview_never_reads_token_or_changes_state_or_cache(self):
        self.token_path.unlink()
        with patch.object(bot, "Telegram") as client, patch.object(bot, "fetch_schedule", return_value=schedule()) as fetch:
            code, result = self.main(["preview", "--at", "2026-10-19T12:00:00+01:00"])
        self.assertEqual((code, result["status"]), (0, "preview"))
        client.assert_not_called()
        self.assertEqual(fetch.call_args.args[1], property_config())
        self.assertEqual(fetch.call_args.kwargs, {})
        self.assertFalse(self.state.exists())
        self.assertFalse(self.cache.exists())
        self.assertFalse(Path(str(self.state) + ".lock").exists())

    def test_status_needs_no_token_and_reports_no_private_payload(self):
        bot.publish(FakeTelegram(), schedule(), NOW, self.state)
        self.token_path.unlink()
        self.property_path.unlink()
        code, result = self.main(["status"])
        self.assertEqual((code, result["status"]), (0, "ok"))
        self.assertNotIn("property_identity", json.dumps(result))
        self.assertNotIn("text", json.dumps(result))
        self.assertEqual(result["destination"], bot.DESTINATION)
        self.assertEqual(result["sender"], "acorn-care-bot")
        self.assertNotIn("bot_username", result)
        self.assertNotIn("username", result)

    def test_status_rejects_private_or_malformed_record_keys_without_echoing_them(self):
        bot.publish(FakeTelegram(), schedule(), NOW, self.state)
        value = self.read_state()
        value["alerts"]["https://invalid.example/private-address"] = {"status": "needs_review"}
        storage.atomic_json(self.state, value)
        code, result = self.main(["status"])
        self.assertEqual(code, 1)
        self.assertNotIn("https://", json.dumps(result))
        self.assertNotIn("private-address", json.dumps(result))

    def test_status_works_with_missing_state_without_creating_it(self):
        code, result = self.main(["status"])
        self.assertEqual((code, result["publications"]), (0, {}))
        self.assertFalse(self.state.exists())

    def test_simulated_time_is_preview_only_and_requires_zone(self):
        for command in ("run", "status", "verify"):
            self.assertEqual(self.main([command, "--at", "2026-10-19T12:00:00+01:00"])[0], 1)
        for at in ("2026-10-19T12:00:00", "not-a-date"):
            self.assertEqual(self.main(["preview", "--at", at])[0], 1)

    def test_verify_is_read_only_and_requires_local_configuration(self):
        fake = bot.Telegram.__new__(bot.Telegram)
        fake._bot_id = ACORN_ID
        me = {"id": ACORN_ID, "username": ACORN_USERNAME, "is_bot": True}
        member = {"user": {"id": ACORN_ID}, "status": "administrator", "can_pin_messages": True}
        with patch.object(fake, "call", side_effect=[me, member]), patch.object(bot, "Telegram", return_value=fake):
            code, result = self.main(["verify"])
        self.assertEqual(result, {"status": "verified", "username": ACORN_USERNAME,
                                  "can_pin_messages": True, "can_delete_messages": False})
        self.assertEqual(code, 0)
        self.assertNotIn(self.token_path.read_text().strip(), json.dumps(result))
        self.assertFalse(self.state.exists())
        self.property_path.unlink()
        with patch.object(bot, "Telegram") as client:
            self.assertEqual(self.main(["verify"])[0], 1)
        client.assert_not_called()

    def test_run_resumes_before_fetch_and_uses_fresh_cache_policy(self):
        fake = FakeTelegram()
        with patch.object(bot, "Telegram", return_value=fake), patch.object(bot, "fetch_schedule", return_value=schedule()) as fetch:
            self.assertEqual(self.main(["run"])[1]["status"], "published")
            self.assertEqual(fetch.call_args.kwargs, {"cache_path": self.cache, "force": True})
            fetch.reset_mock()
            self.assertEqual(self.main(["run"])[1]["status"], "already_published")
            fetch.assert_not_called()
        self.assertEqual([m for m, _ in fake.calls].count("sendRichMessage"), 1)

    def test_run_recovers_older_pin_then_publishes_current_week(self):
        fake = FakeTelegram(failure=lambda m, p: bot.TelegramError("Pin interrupted.") if m == "pinChatMessage" else None)
        with self.assertRaises(bot.TelegramError):
            bot.publish(fake, schedule(), NOW, self.state)
        fake.failure = None
        later = NOW + timedelta(days=7)
        with patch.object(bot, "Telegram", return_value=fake), patch.object(bot, "fetch_schedule", return_value=schedule(later)) as fetch:
            code, result = self.main(["run"], later)
        self.assertEqual((code, result["status"], result["target_start"]), (0, "published", "2026-11-02"))
        fetch.assert_called_once()
        self.assertEqual([m for m, _ in fake.calls].count("sendRichMessage"), 2)
        self.assertEqual(self.read_state()["current"]["target_start"], "2026-11-02")

    def test_run_after_cleanup_rejection_still_fetches_new_week(self):
        fake = FakeTelegram()
        bot.publish(fake, schedule(), NOW, self.state)
        later = NOW + timedelta(days=7)
        fake.failure = lambda m, p: bot.TelegramError("Rejected cleanup.") if m == "unpinChatMessage" else None
        bot.publish(fake, schedule(later), later, self.state)
        following = NOW + timedelta(days=14)
        with patch.object(bot, "Telegram", return_value=fake), patch.object(bot, "fetch_schedule", return_value=schedule(following)) as fetch:
            code, result = self.main(["run"], following)
        self.assertEqual((code, result["status"], result["target_start"]), (0, "published", "2026-11-09"))
        fetch.assert_called_once()
        self.assertTrue(result["cleanup_pending"])
        self.assertEqual([m for m, _ in fake.calls].count("sendRichMessage"), 3)

    def test_run_checks_unknown_state_before_loading_token_or_fetching(self):
        fake = FakeTelegram(failure=lambda m, p: bot.TelegramError("Unknown.", uncertain=True))
        with self.assertRaises(bot.TelegramError):
            bot.publish(fake, schedule(), NOW, self.state)
        with patch.object(bot, "Telegram") as client, patch.object(bot, "fetch_schedule") as fetch:
            code, result = self.main(["run"])
        self.assertEqual(code, 1)
        self.assertIn("unknown", result["description"])
        client.assert_not_called()
        fetch.assert_not_called()

    def test_council_failure_sends_only_one_fixed_alert_and_keeps_pin(self):
        fake = FakeTelegram()
        with patch.object(bot, "Telegram", return_value=fake), patch.object(bot, "fetch_schedule", side_effect=council.CouncilError("Synthetic failure.")):
            code, result = self.main(["run"])
            self.assertEqual((code, result["alert_sent"]), (1, True))
            self.main(["run"])
        self.assertEqual([m for m, _ in fake.calls], ["sendMessage"])
        self.assertIsNone(self.read_state()["current"])

    def test_unknown_send_error_does_not_trigger_another_send_as_alert(self):
        fake = FakeTelegram(failure=lambda m, p: URLError("https://invalid.example/private-address"))
        with patch.object(bot, "Telegram", return_value=fake), patch.object(bot, "fetch_schedule", return_value=schedule()):
            code, result = self.main(["run"])
        self.assertEqual(code, 1)
        self.assertNotIn("private-address", json.dumps(result))
        self.assertNotIn("https://", json.dumps(result))
        self.assertEqual([m for m, _ in fake.calls], ["sendRichMessage"])

    def test_initial_force_and_reset_commands_are_not_exposed(self):
        for command in ("initial", "reset", "force", "resend"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                bot.main([command])

    def test_defaults_remain_portable(self):
        value = schedule()
        with patch.dict(os.environ, {}, clear=True), patch.object(bot, "load_property", return_value=property_config()), \
             patch.object(bot, "Telegram", return_value=FakeTelegram()), patch.object(bot, "locked_state") as lock, \
             patch.object(bot, "resume", return_value=None) as resume, patch.object(bot, "fetch_schedule", return_value=value) as fetch, \
             patch.object(bot, "publish", return_value={"status": "published"}) as publish:
            lock.return_value.__enter__.return_value = {"publications": {}}
            self.assertEqual(self.main(["run"])[0], 0)
        self.assertEqual(resume.call_args.args[2], Path("/data/state.json"))
        self.assertEqual(fetch.call_args.kwargs["cache_path"], Path("/data/council-cache.json"))
        self.assertEqual(publish.call_args.args[-1], Path("/data/state.json"))


if __name__ == "__main__":
    unittest.main()

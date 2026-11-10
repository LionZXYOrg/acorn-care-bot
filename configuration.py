"""Validate configuration without reading any credentials or runtime state."""
import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, time
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo


FOOD_TYPES = ('wet', 'wet_new', 'dry', 'dry_new')


def safe_text(value, limit):
    """Product metadata is bounded, plain, single-line Telegram text."""
    return (isinstance(value, str) and bool(value.strip()) and value == value.strip()
            and len(value.encode('utf-16-le', errors='surrogatepass')) // 2 <= limit
            and not any(unicodedata.category(ch).startswith('C') or ch in '<>'
                        or (ch.isspace() and ch != ' ') for ch in value)
            and ' · ' not in value)


def valid_grams(value):
    return (type(value) is int or (type(value) is float and math.isfinite(value))) and value >= 0


def load_care_config(path):
    value = json.loads(Path(path).read_text())
    validate_care_config(value)
    return value


def validate_care_config(c):
    ZoneInfo(c['timezone'])
    start = date.fromisoformat(c['start_date'])
    for name in ('quiet_start', 'quiet_end'):
        if not re.fullmatch(r'\d{2}:\d{2}', c[name]):
            raise ValueError('Invalid quiet hour')
        time.fromisoformat(c[name])
    if c['quiet_start'] <= c['quiet_end']:
        raise ValueError('Quiet hours must cross midnight')
    if type(c.get('automatic_repeats', True)) is not bool:
        raise ValueError('Invalid automatic reminder flag')
    if type(c.get('bowls_morning_only', False)) is not bool:
        raise ValueError('Invalid morning-only bowls flag')
    if type(c.get('water_morning_only', False)) is not bool:
        raise ValueError('Invalid morning-only water flag')
    if type(c['repeat_minutes']) is not int or not 1 <= c['repeat_minutes'] <= 1440:
        raise ValueError('Invalid repeat interval')
    if type(c['wet_can_grams']) not in (int, float) or not math.isfinite(c['wet_can_grams']) or c['wet_can_grams'] <= 0:
        raise ValueError('Invalid can weight')
    if not isinstance(c['mention'], str) or not c['mention'] or len(c['mention']) > 256:
        raise ValueError('Invalid caregiver mention')
    products = c.get('food_products', {})
    if not isinstance(products, dict) or any(kind not in FOOD_TYPES for kind in products):
        raise ValueError('Invalid food products')
    for product in products.values():
        if (not isinstance(product, dict) or not safe_text(product.get('name'), 256)
                or not safe_text(product.get('label'), 64)
                or not safe_text(product.get('button_label'), 64)):
            raise ValueError('Invalid food product metadata')
        if 'pack_grams' in product and (not valid_grams(product['pack_grams']) or product['pack_grams'] <= 0):
            raise ValueError('Invalid food pack weight')
    if 'nutrition_plan_id' in c and not safe_text(c['nutrition_plan_id'], 256):
        raise ValueError('Invalid nutrition plan identity')
    if 'nutrition_notice_time' in c:
        if not isinstance(c['nutrition_notice_time'], str) or not re.fullmatch(r'\d{2}:\d{2}', c['nutrition_notice_time']):
            raise ValueError('Invalid nutrition notice time')
        time.fromisoformat(c['nutrition_notice_time'])
    slots = c['slots']
    if len(slots) != 3 or [s['key'] for s in slots] != ['breakfast', 'lunch', 'dinner']:
        raise ValueError('The portion plan requires three ordered meals')
    times = [s['time'] for s in slots]
    if times != sorted(set(times)):
        raise ValueError('Meal times must be distinct and ordered')
    for slot in slots:
        if not re.fullmatch(r'\d{2}:\d{2}', slot['time']):
            raise ValueError('Invalid meal time')
        time.fromisoformat(slot['time'])
        if not c['quiet_end'] <= slot['time'] < c['quiet_start']:
            raise ValueError('Meals must be outside quiet hours')
        if not isinstance(slot['label'], str) or not slot['label'] or len(slot['label']) > 128:
            raise ValueError('Invalid meal label')
        if type(slot['cleaning']) is not bool:
            raise ValueError('Invalid cleaning flag')
        if any(k not in {'litter', 'bowls', 'water'} for k in slot.get('cleaning_tasks', [])):
            raise ValueError('Invalid cleaning task')
        if 'cleaning_labels' in slot:
            labels = slot['cleaning_labels']
            effective = slot.get('cleaning_tasks', ('litter', 'bowls', 'water')) if slot['cleaning'] else ()
            if (not isinstance(labels, dict)
                    or any(kind not in {'litter', 'bowls', 'water'} or kind not in effective
                           or not safe_text(label, 256) for kind, label in labels.items())):
                raise ValueError('Invalid cleaning labels')
    if 'treatments' in c:
        from treatments import validate
        validate(c['treatments'], {slot['key'] for slot in slots}, safe_text)
    stages = c['stages']
    starts = [date.fromisoformat(s['start']) for s in stages]
    if not starts or starts != sorted(set(starts)) or starts[0] != start:
        raise ValueError('Invalid stage dates')
    if len({s['id'] for s in stages}) != len(stages):
        raise ValueError('Duplicate stage identity')
    for stage in stages:
        if 'morning_notice' in stage and type(stage['morning_notice']) is not bool:
            raise ValueError('Invalid morning nutrition notice flag')
        if 'advance_notice' in stage and not safe_text(stage['advance_notice'], 2048):
            raise ValueError('Invalid advance nutrition notice')
        for food in FOOD_TYPES:
            grams = stage[food] if food in ('wet', 'dry') else stage.get(food, [0, 0, 0])
            if not isinstance(grams, (list, tuple)) or len(grams) != len(slots) or any(not valid_grams(g) for g in grams):
                raise ValueError('Invalid portions')
            daily_key = 'daily_' + food
            daily = stage[daily_key] if food in ('wet', 'dry') else stage.get(daily_key, 0)
            if not valid_grams(daily) or sum((Fraction(str(g)) for g in grams), Fraction(0)) != Fraction(str(daily)):
                raise ValueError('Daily portion does not match meal portions')
            if food in ('wet_new', 'dry_new') and any(grams) and food not in products:
                raise ValueError('New food requires product metadata')


def integer(text, *, positive=False):
    if not isinstance(text, str) or not re.fullmatch(r'-?[0-9]+', text):
        raise ValueError('A numeric Telegram ID is required')
    result = int(text)
    if not result or (positive and result < 1):
        raise ValueError('Invalid Telegram ID')
    return result


@dataclass(frozen=True)
class Runtime:
    chat_id: int
    topic_id: int
    allowed_ids: frozenset
    state_path: Path
    http_host: str = '0.0.0.0'
    http_port: int = 8080
    fresh_start: bool = False

    @classmethod
    def from_env(cls):
        ids = os.environ.get('TELEGRAM_ALLOWED_USER_IDS', '').split(',')
        allowed = frozenset(integer(s.strip(), positive=True) for s in ids)
        port = integer(os.environ.get('ACORN_HTTP_PORT', '8080'), positive=True)
        if port > 65535:
            raise ValueError('Invalid HTTP port')
        return cls(integer(os.environ.get('TELEGRAM_CHAT_ID')),
                   integer(os.environ.get('TELEGRAM_TOPIC_ID'), positive=True), allowed,
                   Path(os.environ.get('ACORN_STATE_PATH', '/data/state.json')),
                   os.environ.get('ACORN_HTTP_HOST', '0.0.0.0'), port,
                   fresh_start=os.environ.get('ACORN_FRESH_START', '0') == '1')


def read_token():
    """A configured token file takes precedence. Never fall back on a missing file."""
    path = os.environ.get('TELEGRAM_BOT_TOKEN_FILE')
    try:
        token = Path(path).read_text().strip() if path else os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    except OSError:
        raise ValueError('New bot token file is unavailable') from None
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{16,}', token):
        raise ValueError('A new Telegram bot token is required')
    return token

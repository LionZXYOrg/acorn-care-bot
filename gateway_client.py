"""Token-free, one-request-per-connection Telegram transport over a Unix socket.

UnixClient.call has the same result/TelegramError interface as telegram_api.Client.
There are deliberately no connection retries, including after ambiguous sends.
"""
import http.client
import json
import math
import socket

from telegram_api import TelegramError

MAX_BODY = 65_536
MAX_RESPONSE = 2_100_000


def strict_loads(raw):
    """Reject duplicate keys, invalid UTF-8, NaN, Infinity and overflowing floats."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Invalid JSON')
            value[key] = item
        return value

    def constant(_):
        raise ValueError('Invalid JSON')

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('Invalid JSON')
        return value

    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='strict')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant,
                      parse_float=number)


def encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode('ascii')


class _Connection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
        except Exception:
            self.sock.close()
            self.sock = None
            raise


class UnixClient:
    def __init__(self, socket_path, *, timeout=40):
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def call(self, method, payload=None):
        try:
            if not isinstance(method, str) or not isinstance(payload if payload is not None else {}, dict):
                raise ValueError()
            body = encode({'method': method, 'payload': {} if payload is None else payload})
            if len(body) > MAX_BODY:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise TelegramError(400) from None
        connection = _Connection(self.socket_path, self.timeout)
        try:
            connection.request('POST', '/call', body=body,
                               headers={'Content-Type': 'application/json', 'Connection': 'close'})
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError()
            value = strict_loads(raw)
            if not isinstance(value, dict) or type(value.get('ok')) is not bool:
                raise ValueError()
            if value['ok']:
                if response.status != 200 or set(value) != {'ok', 'result'}:
                    raise ValueError()
                return value['result']
            if set(value) != {'ok', 'error'} or not isinstance(value['error'], dict):
                raise ValueError()
            error = value['error']
            if (set(error) != {'code', 'retry_after', 'uncertain', 'not_modified'}
                    or type(error['code']) is not int or not 0 <= error['code'] <= 599
                    or type(error['retry_after']) is not int or not 0 <= error['retry_after'] <= 3600
                    or type(error['uncertain']) is not bool or type(error['not_modified']) is not bool):
                raise ValueError()
            raise TelegramError(**error)
        except TelegramError:
            raise
        except Exception:
            # A partial write, lost reply or malformed envelope can follow a successful send.
            raise TelegramError(uncertain=True) from None
        finally:
            connection.close()

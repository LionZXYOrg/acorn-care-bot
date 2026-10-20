"""Minimal Telegram API transport. Exception messages never contain URLs or bodies."""
import json
import urllib.error
import urllib.request


class TelegramError(Exception):
    def __init__(self, code=0, *, retry_after=0, uncertain=False, not_modified=False):
        super().__init__('Telegram request failed (code %d)' % code)
        self.code = code
        self.retry_after = min(max(retry_after, 1), 3600) if retry_after else 0
        self.uncertain = uncertain
        self.not_modified = not_modified


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, token, opener=None):
        self._token = token
        self._opener = opener or urllib.request.build_opener(NoRedirect())

    def call(self, method, payload=None):
        if method not in {'getMe', 'getWebhookInfo', 'getChatMember', 'getUpdates', 'sendMessage',
                          'editMessageText', 'answerCallbackQuery'}:
            raise ValueError('Unsupported Telegram operation')
        request = urllib.request.Request(
            'https://api.telegram.org/bot' + self._token + '/' + method,
            data=json.dumps(payload or {}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        status = 200
        try:
            with self._opener.open(request, timeout=45) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(2_000_001)
            except Exception:
                raw = b''
            finally:
                exc.close()
        except Exception:
            raise TelegramError(uncertain=True) from None
        try:
            if len(raw) > 2_000_000:
                raise ValueError()
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise TelegramError(status, uncertain=True) from None
        if status != 200 or data.get('ok') is not True:
            code = data.get('error_code', status)
            if type(code) is not int:
                code = status
            parameters = data.get('parameters')
            retry_after = parameters.get('retry_after', 0) if isinstance(parameters, dict) else 0
            if type(retry_after) is not int:
                retry_after = 0
            raise TelegramError(code, retry_after=retry_after if code == 429 else 0,
                                uncertain=code >= 500 or 300 <= code < 400,
                                not_modified=code == 400 and 'message is not modified' in str(data.get('description', '')).lower())
        if 'result' not in data:
            raise TelegramError(uncertain=True)
        return data['result']

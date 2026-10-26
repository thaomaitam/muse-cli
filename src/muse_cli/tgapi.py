"""Minimal Telegram Bot API client and message formatting for the bridge.

Talks to https://api.telegram.org directly with curl_cffi (already a
dependency): no bot framework. The token is part of every URL, so URLs and
raw transport errors are never logged or raised as-is; `_redact` strips it.
"""
import html
import json
import os
import re
import time
import uuid

API = "https://api.telegram.org"
TEXT_LIMIT = 4096       # Telegram's hard limit per message
CHUNK = 3800            # what we put in one message (room for markup)
CAPTION_LIMIT = 1024
UPLOAD_LIMIT = 49 * 1024 * 1024    # bots may send up to 50 MB
DOWNLOAD_LIMIT = 20 * 1024 * 1024  # bots may fetch up to 20 MB


class TelegramError(RuntimeError):
    def __init__(self, method, code, description, retry_after=None):
        super().__init__(f"telegram {method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description or ""
        self.retry_after = retry_after

    @property
    def parse_error(self):
        d = self.description.lower()
        return self.code == 400 and ("can't parse" in d or "parse entities" in d
                                     or "unsupported start tag" in d)

    @property
    def not_modified(self):
        return "message is not modified" in self.description.lower()


class TelegramAPI:
    """Bot API over HTTPS. `call(method, **params)` returns the `result`."""

    def __init__(self, token, base=API, session=None):
        if not token or not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token.strip()):
            raise ValueError("that does not look like a bot token (expected 123456:ABC...)")
        self.token = token.strip()
        self.base = base
        self._session = session

    def __repr__(self):
        return "TelegramAPI(token=<redacted>)"

    def _redact(self, text):
        return str(text).replace(self.token, "<token>")

    def _http(self):
        if self._session is None:
            from curl_cffi import requests as rq
            self._session = rq.Session()
        return self._session

    def _post(self, method, timeout, **kw):
        url = f"{self.base}/bot{self.token}/{method}"
        try:
            r = self._http().post(url, timeout=timeout, **kw)
        except Exception as e:  # noqa: BLE001 - network; never leak the URL
            raise ConnectionError(f"telegram {method}: {self._redact(e)}") from None
        try:
            d = r.json()
        except ValueError:
            raise TelegramError(method, r.status_code, self._redact(r.text[:200])) from None
        if not d.get("ok"):
            params = d.get("parameters") or {}
            raise TelegramError(method, d.get("error_code") or r.status_code,
                                self._redact(d.get("description") or ""), params.get("retry_after"))
        return d.get("result")

    def call(self, method, _http_timeout=40, _retries=2, **params):
        """POST one Bot API method; retries 429 (flood wait) up to _retries times."""
        timeout, retries = _http_timeout, _retries
        params = {k: v for k, v in params.items() if v is not None}
        for k, v in list(params.items()):
            if isinstance(v, (dict, list)):
                params[k] = json.dumps(v, ensure_ascii=False)
        for attempt in range(retries + 1):
            try:
                return self._post(method, timeout, data=params)
            except TelegramError as e:
                if e.code == 429 and e.retry_after and attempt < retries and e.retry_after <= 60:
                    time.sleep(e.retry_after + 0.5)
                    continue
                raise

    def send_file(self, chat_id, filename, data, caption=None, kind="document", **params):
        """sendDocument / sendPhoto with an in-memory file (multipart)."""
        method = {"document": "sendDocument", "photo": "sendPhoto"}[kind]
        fields = {"chat_id": chat_id, "caption": (caption or "")[:CAPTION_LIMIT] or None, **params}
        body, ctype = multipart({k: v for k, v in fields.items() if v is not None},
                                kind, filename, data)
        return self._post(method, 120, data=body, headers={"Content-Type": ctype})

    def download(self, file_id, limit=DOWNLOAD_LIMIT):
        """Bytes of a file the user sent (getFile, then the file URL)."""
        f = self.call("getFile", file_id=file_id)
        if (f.get("file_size") or 0) > limit:
            raise ValueError(f"file is {f['file_size']} bytes; bots can only fetch {limit}")
        url = f"{self.base}/file/bot{self.token}/{f['file_path']}"
        try:
            r = self._http().get(url, timeout=120)
        except Exception as e:  # noqa: BLE001
            raise ConnectionError(f"telegram file download: {self._redact(e)}") from None
        if r.status_code != 200:
            raise TelegramError("file", r.status_code, "download failed")
        return r.content, f.get("file_path")


def multipart(fields, file_field, filename, data):
    boundary = "----muse" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'
                      f"{v}\r\n").encode())
    safe = re.sub(r'[\r\n"]', "_", filename) or "file"
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                  f'filename="{safe}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
                 + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# -- formatting -----------------------------------------------------------------
_FENCE_RE = re.compile(r"```([A-Za-z0-9_+.-]*)\n?(.*?)```", re.S)
_INLINE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")


def esc(text):
    return html.escape(str(text), quote=False)


def md_to_html(text):
    """Muse's Markdown -> Telegram HTML: code fences, `code`, **bold**, links.

    Anything else is escaped as plain text, so the result always parses
    unless a fence is cut in half (callers fall back to plain text then).
    """
    out, pos = [], 0
    for m in _FENCE_RE.finditer(text):
        out.append(_inline(text[pos:m.start()]))
        lang, code = m.group(1), m.group(2)
        cls = f' class="language-{esc(lang)}"' if lang else ""
        out.append(f"<pre><code{cls}>{esc(code.rstrip())}</code></pre>")
        pos = m.end()
    out.append(_inline(text[pos:]))
    return "".join(out)


def _inline(text):
    stash = []

    def keep(s):
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"
    t = _INLINE_RE.sub(lambda m: keep(f"<code>{esc(m.group(1))}</code>"), text)
    t = _LINK_RE.sub(lambda m: keep(f'<a href="{html.escape(m.group(2))}">{esc(m.group(1))}</a>'), t)
    t = esc(t)
    t = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", t)
    t = re.sub(r"^#{1,6} +(.+)$", r"<b>\1</b>", t, flags=re.M)
    return re.sub("\x00(\\d+)\x00", lambda m: stash[int(m.group(1))], t)


def split_text(text, limit=CHUNK):
    """Split into pieces <= limit, preferring paragraph, then line, then word breaks.
    A ``` fence cut by a split is closed and reopened so each piece renders."""
    text = text or ""
    pieces = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 3:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 3:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 3:
            cut = limit
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text.strip() or not pieces:
        pieces.append(text)
    fixed, carry = [], None
    for p in pieces:
        if carry is not None:
            p = f"```{carry}\n" + p
            carry = None
        fences = re.findall(r"```([A-Za-z0-9_+.-]*)", p)
        if len(fences) % 2:
            carry = fences[-1]
            p = p + "\n```"
        fixed.append(p)
    return fixed


def longest_code_block(text):
    return max((len(m.group(2)) for m in _FENCE_RE.finditer(text or "")), default=0)


def needs_document(text, max_messages=3, max_code=3000):
    """Very long replies or big code blocks read better as an attached file."""
    return len(text or "") > max_messages * CHUNK or longest_code_block(text) > max_code


def button(text, data):
    return {"text": text[:64], "callback_data": data}


def keyboard(rows):
    return {"inline_keyboard": [r for r in rows if r]}


def doc_name(stem, text):
    ext = ".md" if ("```" in text or re.search(r"^#{1,6} |\*\*", text, re.M)) else ".txt"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-")[:40] + ext


def file_kind(name):
    ext = os.path.splitext(name.lower())[1]
    return "photo" if ext in (".jpg", ".jpeg", ".png", ".webp") else "document"

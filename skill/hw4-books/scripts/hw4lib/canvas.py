"""Minimal Canvas REST client: only the discussion endpoints this homework needs.

Reads are retried with exponential backoff. Writes (POST) are NEVER retried blindly:
if a write's outcome is unknown (timeout, dropped connection, 5xx), we raise AmbiguousWrite
and the cycle reconciles by re-reading the forum before doing anything else.
"""
import html
import random
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests


class CanvasError(Exception):
    """Permanent error (bad request, forbidden, not found)."""


class TransientError(CanvasError):
    """Retryable error that persisted after all retries."""


class AmbiguousWrite(CanvasError):
    """A write was sent but we don't know whether Canvas saved it."""


class CanvasClient:
    def __init__(self, base_url, token, *, timeout=20.0, max_retries=3, backoff_base=2.0,
                 session=None, sleep=time.sleep, log=None):
        self.base_url = base_url.rstrip("/")
        self._host = urlparse(self.base_url).netloc
        self._session = session or requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._log = log or (lambda *a, **k: None)

    # ---------- low level ----------
    def _url(self, path):
        return path if path.startswith("http") else f"{self.base_url}{path}"

    def _backoff(self, attempt, retry_after=None):
        delay = float(retry_after) if retry_after else self.backoff_base * (2 ** attempt)
        delay = min(delay, 60.0) + random.uniform(0, 0.5)
        self._log("backoff", attempt=attempt + 1, delay_s=round(delay, 2))
        self._sleep(delay)

    def _get(self, path, params=None):
        """GET with retries on timeout / connection error / 429 / 5xx / malformed JSON."""
        url = self._url(path)
        if urlparse(url).netloc != self._host:  # never send the token to another host
            raise CanvasError("refusing to follow a URL off the Canvas host")
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                r = self._session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                elif r.status_code >= 400:
                    raise CanvasError(f"GET {path} -> HTTP {r.status_code}")
                else:
                    try:
                        return r.json(), r
                    except ValueError:
                        last = "malformed JSON"
            except (requests.Timeout, requests.ConnectionError) as e:
                last = type(e).__name__
                r = None
            self._log("read_retry", path=path, reason=last)
            if attempt < self.max_retries:
                self._backoff(attempt, r.headers.get("Retry-After") if r is not None else None)
        raise TransientError(f"GET {path} failed after retries: {last}")

    def _get_all(self, path, params=None):
        """Follow Canvas Link-header pagination (same host only)."""
        items, url, p = [], path, dict(params or {}, per_page=100)
        while url:
            data, r = self._get(url, p)
            if not isinstance(data, list):
                raise TransientError(f"expected a list from {path}")
            items.extend(data)
            url, p = r.links.get("next", {}).get("url"), None
        return items

    def _post(self, path, data):
        """Single-attempt POST. Unknown outcome -> AmbiguousWrite; caller must reconcile."""
        try:
            r = self._session.post(self._url(path), data=data, timeout=self.timeout)
        except requests.Timeout:
            raise AmbiguousWrite(f"POST {path} timed out")
        except requests.ConnectionError:
            raise AmbiguousWrite(f"POST {path} connection error")
        if r.status_code == 429:
            raise TransientError("POST rate limited by Canvas (not applied)")
        if r.status_code >= 500:
            raise AmbiguousWrite(f"POST {path} -> HTTP {r.status_code}")
        if r.status_code >= 400:
            raise CanvasError(f"POST {path} -> HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise AmbiguousWrite(f"POST {path} returned malformed JSON")
        if not isinstance(body, dict) or "id" not in body:
            raise AmbiguousWrite(f"POST {path} response missing id")
        return body

    # ---------- endpoints ----------
    def get_self(self):
        data, _ = self._get("/api/v1/users/self")
        return data

    def find_topic(self, course_id, title):
        topics = self._get_all(f"/api/v1/courses/{course_id}/discussion_topics", {"search_term": title})
        exact = [t for t in topics if t.get("title", "").strip() == title]
        if len(exact) != 1:
            raise CanvasError(f"expected exactly one topic titled {title!r}, found {len(exact)}")
        return exact[0]

    def get_topic(self, course_id, topic_id):
        data, _ = self._get(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}")
        if not isinstance(data, dict) or "message" not in data:
            raise TransientError("malformed topic response")
        return data

    def get_topic_view(self, course_id, topic_id):
        data, _ = self._get(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/view",
                            {"include_new_entries": 1})
        if not isinstance(data, dict) or not isinstance(data.get("view", []), list):
            raise TransientError("malformed topic view")
        return data

    def post_entry(self, course_id, topic_id, message_html):
        return self._post(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/entries",
                          {"message": message_html})

    def post_reply(self, course_id, topic_id, entry_id, message_html):
        return self._post(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/entries/{entry_id}/replies",
                          {"message": message_html})


# ---------- helpers ----------
class _Text(HTMLParser):
    BREAKS = {"br", "p", "div", "li", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, d):
        self.parts.append(d)


def html_to_text(s):
    p = _Text()
    p.feed(s or "")
    text = html.unescape("".join(p.parts)).replace("\xa0", " ")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


CONTROL_RE = re.compile(r"^COURSE-TEAM CONTROL:\s*(RUNNING|PAUSED)\s*$")


def read_control(topic):
    """Return 'RUNNING', 'PAUSED', or 'UNKNOWN' from the first non-empty line of the forum description.
    Anything other than an exact RUNNING line is treated as 'do not post' (fail closed)."""
    for line in html_to_text(topic.get("message", "")).splitlines():
        line = line.strip()
        if line:
            m = CONTROL_RE.match(line)
            return m.group(1) if m else "UNKNOWN"
    return "UNKNOWN"


def text_to_html(text):
    paras = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)


def flatten_view(view_json):
    """Flatten Canvas' nested /view tree into a list of entries with parent_id set."""
    out = []

    def walk(entries, parent):
        for e in entries or []:
            out.append({
                "id": e.get("id"),
                "parent_id": e.get("parent_id", parent),
                "user_id": e.get("user_id"),
                "created_at": e.get("created_at"),
                "updated_at": e.get("updated_at"),
                "deleted": bool(e.get("deleted")),
                "text": html_to_text(e.get("message", "")),
            })
            walk(e.get("replies"), e.get("id"))

    walk(view_json.get("view"), None)
    for e in view_json.get("new_entries") or []:
        if not any(x["id"] == e.get("id") for x in out):
            walk([e], e.get("parent_id"))
    names = {p.get("id"): p.get("display_name", "") for p in view_json.get("participants", [])}
    for e in out:
        e["author"] = names.get(e["user_id"], "unknown")
    return [e for e in out if e["id"] is not None]

"""Fetching and 'on sale' detection.

Two detection modes per item:
  * keyword  - look for configurable available/unavailable phrases in visible text
  * diff     - hash the visible text and flag any change (user confirms meaning)

Fetching strategy:
  1. Plain HTTP (requests) + BeautifulSoup text extraction.
  2. If the extracted text looks empty/suspicious (too short), fall back to a
     headless browser (Playwright) that renders JavaScript before extraction.
     Playwright is optional; if it is not installed we simply skip the fallback.
"""
import hashlib
import re

import requests
from bs4 import BeautifulSoup

import config
from models import (
    MODE_DIFF,
    MODE_KEYWORD,
    STATE_AVAILABLE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)

FETCH_HTTP = "http"
FETCH_BROWSER = "browser"

SNIPPET_MAX = 600


class FetchError(Exception):
    """Raised when a page could not be fetched by any available method."""


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------
def extract_visible_text(html):
    """Return collapsed visible text from an HTML string."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    # Collapse whitespace runs.
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------
def fetch_http(url):
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(
        url, headers=headers, timeout=config.FETCH_TIMEOUT, allow_redirects=True
    )
    resp.raise_for_status()
    return resp.text


def fetch_browser(url):
    """Render the page with a headless Chromium via Playwright.

    Imported lazily so the app works even when Playwright isn't installed.
    Raises FetchError with a helpful message if it isn't available.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise FetchError(
            "Playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium"
        ) from exc

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=config.USER_AGENT)
                page.goto(
                    url,
                    timeout=config.FETCH_TIMEOUT * 1000,
                    wait_until="networkidle",
                )
                html = page.content()
            finally:
                browser.close()
        return html
    except FetchError:
        raise
    except Exception as exc:  # pragma: no cover - runtime/browser errors
        raise FetchError(f"Headless browser fetch failed: {exc}") from exc


def fetch_text(url):
    """Fetch visible text, preferring plain HTTP and falling back to a browser.

    Returns (text, fetch_method). Raises FetchError if everything fails.
    """
    http_error = None
    try:
        html = fetch_http(url)
        text = extract_visible_text(html)
        if len(text) >= config.MIN_TEXT_LENGTH:
            return text, FETCH_HTTP
        # Text is suspiciously short -> likely JS-rendered. Try the browser.
    except requests.RequestException as exc:
        http_error = exc
        text = ""

    try:
        html = fetch_browser(url)
        text = extract_visible_text(html)
        return text, FETCH_BROWSER
    except FetchError as browser_exc:
        # Browser unavailable/failed. If HTTP at least returned *something*,
        # use it rather than failing outright.
        if http_error is None and text:
            return text, FETCH_HTTP
        raise FetchError(
            f"HTTP fetch: {http_error or 'text too short'}. "
            f"Browser fallback: {browser_exc}"
        )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def _find_phrase(text, phrases):
    """Return the first phrase found in text (case-insensitive), else None."""
    lowered = text.lower()
    for phrase in phrases:
        p = (phrase or "").strip().lower()
        if p and p in lowered:
            return phrase
    return None


def _snippet_around(text, phrase, width=SNIPPET_MAX):
    """Return a snippet of text centered on the matched phrase."""
    if not phrase:
        return text[:width]
    idx = text.lower().find(phrase.lower())
    if idx == -1:
        return text[:width]
    start = max(0, idx - width // 2)
    end = min(len(text), idx + len(phrase) + width // 2)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def detect_keyword(text, available_phrases, unavailable_phrases):
    """Keyword-mode detection.

    AVAILABLE when an available phrase appears and no unavailable phrase does.
    UNAVAILABLE when an unavailable phrase appears.
    UNKNOWN when neither list matches.
    Returns (state, snippet).
    """
    avail = _find_phrase(text, available_phrases)
    unavail = _find_phrase(text, unavailable_phrases)

    if avail and not unavail:
        return STATE_AVAILABLE, f'Matched available phrase "{avail}": ' + _snippet_around(text, avail)
    if unavail:
        return STATE_UNAVAILABLE, f'Matched unavailable phrase "{unavail}": ' + _snippet_around(text, unavail)
    return STATE_UNKNOWN, text[:SNIPPET_MAX]


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def detect_diff(text, previous_hash):
    """Diff-mode detection.

    First check (no previous hash) records UNKNOWN as a baseline. After that,
    any change flips to AVAILABLE (meaning "something changed — go look"),
    while an unchanged page stays UNAVAILABLE.
    Returns (state, snippet, new_hash).
    """
    new_hash = hash_text(text)
    snippet = text[:SNIPPET_MAX]
    if not previous_hash:
        return STATE_UNKNOWN, "Baseline captured — will alert on any change. " + snippet, new_hash
    if new_hash != previous_hash:
        return STATE_AVAILABLE, "Page content CHANGED since last check. " + snippet, new_hash
    return STATE_UNAVAILABLE, "No change since last check. " + snippet, new_hash


# ---------------------------------------------------------------------------
# Top-level check for a single item
# ---------------------------------------------------------------------------
def run_detection(item):
    """Run the configured detection for one item dict (from models.get_item).

    Returns a dict:
        {state, snippet, fetch_method, error, snapshot_hash}
    Never raises — fetch/detection failures are returned as error with
    state=UNKNOWN so the caller can log them.
    """
    result = {
        "state": STATE_UNKNOWN,
        "snippet": None,
        "fetch_method": None,
        "error": None,
        "snapshot_hash": item.get("last_snapshot_hash"),
    }
    try:
        text, method = fetch_text(item["url"])
        result["fetch_method"] = method
    except FetchError as exc:
        result["error"] = str(exc)
        return result

    if not text:
        result["error"] = "Fetched page but extracted no visible text."
        return result

    if item["detection_mode"] == MODE_DIFF:
        state, snippet, new_hash = detect_diff(text, item.get("last_snapshot_hash"))
        result["snapshot_hash"] = new_hash
    else:
        available = item.get("available_phrases") or config.DEFAULT_AVAILABLE_PHRASES
        unavailable = item.get("unavailable_phrases") or config.DEFAULT_UNAVAILABLE_PHRASES
        state, snippet = detect_keyword(text, available, unavailable)

    result["state"] = state
    result["snippet"] = snippet
    return result

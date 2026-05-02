#!/usr/bin/env python3
"""
Safari Bookmark Processor

Watches Safari's Bookmarks.plist for new entries, matches each URL against
groups defined in a YAML config, then uses OpenAI to generate summaries/tags
and saves the result as a Markdown note in an Obsidian vault.

Copyright (C) 2026 Андрій Петренко
Розповсюджується на умовах Ukrainian Restricted Jurisdictions Public License (URJPL) v1.0.
УВАГА: Використання на території РФ, КНР та Ісламської Республіки Іран СУВОРО ЗАБОРОНЕНО.

https://github.com/aplsms/BookMarkProcessor/LicenseUA.md
"""

import argparse
import hashlib
import json
import logging
import os
import plistlib
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from html.parser import HTMLParser

import yaml
from openai import OpenAI
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

SAFARI_BOOKMARKS = Path.home() / "Library/Safari/Bookmarks.plist"
DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"
DEFAULT_STATE = Path(__file__).parent / ".state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config & state helpers
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_state(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"seen": {}}


def save_state(state: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Safari bookmark extraction
# ---------------------------------------------------------------------------

def _extract(node, result: list) -> None:
    if isinstance(node, dict):
        if node.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
            url = node.get("URLString", "")
            title = node.get("URIDictionary", {}).get("title", "")
            if url and not url.startswith("javascript:"):
                result.append({"url": url, "title": title})
        for child in node.get("Children", []):
            _extract(child, result)
    elif isinstance(node, list):
        for item in node:
            _extract(item, result)


def load_safari_bookmarks(path: Path) -> list[dict]:
    with open(path, "rb") as f:
        plist = plistlib.load(f)
    bookmarks: list[dict] = []
    _extract(plist, bookmarks)
    return bookmarks


# ---------------------------------------------------------------------------
# Group matching
# ---------------------------------------------------------------------------

def match_group(url: str, groups: list[dict]) -> Optional[dict]:
    for group in groups:
        for pattern in group.get("pattern", []):
            if re.search(pattern, url, re.IGNORECASE):
                return group
    return None


# ---------------------------------------------------------------------------
# Web content fetching
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self.chunks.append(text)


def fetch_text(url: str, max_chars: int = 8000) -> str:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; BookmarkProcessor/1.0)"})
        with urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        parser = _TextExtractor()
        parser.feed(html)
        return " ".join(parser.chunks)[:max_chars]
    except (URLError, Exception) as exc:
        log.warning("Could not fetch %s: %s", url, exc)
        return ""


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def validate_url(url: str) -> tuple[bool, str]:
    """Check URL format and reachability. Returns (ok, error_message)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"unsupported scheme: {parsed.scheme!r}"
    if not parsed.netloc:
        return False, "missing host"

    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; BookmarkProcessor/1.0)"},
                      method="HEAD")
        with urlopen(req, timeout=10) as resp:
            status = resp.status
        if status >= 400:
            return False, f"HTTP {status}"
        return True, ""
    except HTTPError as exc:
        # HEAD not allowed — server is reachable, content may still be fetchable
        if exc.code in (405, 403):
            return True, ""
        return False, f"HTTP {exc.code}"
    except URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# OpenAI processing
# ---------------------------------------------------------------------------

def build_prompt(url: str, title: str, content: str, action: str, language: str = "Ukrainian") -> str:
    want_summary = "summary" in action
    want_tags = "tags" in action

    parts = [f"URL: {url}", f"Title: {title}"]
    if content:
        parts.append(f"\nPage content (excerpt):\n{content}")

    instructions = [
        f'- "title": a clean, concise title for the page, written in {language}',
    ]
    schema_fields = ['"title": "..."']
    if want_summary:
        instructions.append(f'- "summary": a concise 2-4 sentence summary of the content, written in {language}')
        schema_fields.append('"summary": "..."')
    if want_tags:
        instructions.append('- "tags": 5-10 relevant lowercase tags (single words or short phrases, no #, in English)')
        schema_fields.append('"tags": ["tag1", "tag2", ...]')

    parts.append("\nProvide the following JSON fields:")
    parts.extend(instructions)
    parts.append(f'\nRespond ONLY with valid JSON: {{{", ".join(schema_fields)}}}')
    return "\n".join(parts)


def call_openai(client: OpenAI, url: str, title: str, action: str, model: str, language: str = "Ukrainian") -> dict:
    content = fetch_text(url)
    prompt = build_prompt(url, title, content, action, language)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise assistant that analyzes web pages and returns "
                        "structured JSON with summaries and tags. Return only valid JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as exc:
        log.error("OpenAI error for %s: %s", url, exc)
        return {}


# ---------------------------------------------------------------------------
# Obsidian note writing
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_filename(title: str, max_len: int = 80) -> str:
    return _UNSAFE_CHARS.sub("_", title).strip()[:max_len]


def save_to_obsidian(vault: Path, folder: str, bookmark: dict, ai: dict) -> Path:
    url = bookmark["url"]
    title = ai.get("title") or bookmark.get("title") or url
    tags = ai.get("tags", [])
    summary = ai.get("summary", "")

    now = datetime.now()
    filename = f"{_safe_filename(title)}_{now.strftime('%Y%m%d_%H%M%S')}.md"
    target_dir = vault / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    note_path = target_dir / filename

    lines = ["---"]
    if tags:
        lines.append(f"tags: [{', '.join(tags)}]")
    lines += [
        f"url: \"{url}\"",
        f"added: {now.strftime('%Y-%m-%d')}",
        "---",
        "",
        f"# {title}",
        "",
        f"> {url}",
        "",
    ]
    if summary:
        lines += ["## Summary", "", summary, ""]

    note_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Saved → %s", note_path)
    return note_path


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Notification senders
# ---------------------------------------------------------------------------

def _http_post(url: str, payload: dict, headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    req = Request(url, data=data, headers=h, method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc


def _http_put(url: str, payload: dict, headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    req = Request(url, data=data, headers=h, method="PUT")
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc


def format_notify_message(bookmark: dict, ai_result: dict) -> str:
    url = bookmark["url"]
    title = ai_result.get("title") or bookmark.get("title") or url
    summary = ai_result.get("summary", "")
    tags = ai_result.get("tags", [])

    parts = [f"<b>{title}</b>", url]
    if summary:
        parts += ["", summary]
    if tags:
        parts += ["", " ".join(f"#{t.replace('-', '_').replace(' ', '_')}" for t in tags)]
    return "\n".join(parts)


def send_telegram(cfg: dict, message: str) -> None:
    token = cfg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = cfg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise ValueError("telegram: bot_token and chat_id are required")
    _http_post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat_id, "text": message, "parse_mode": "HTML",
         "disable_web_page_preview": False},
    )
    log.info("Telegram: sent")


def send_mastodon(cfg: dict, message: str) -> None:
    instance = cfg.get("instance_url", "").rstrip("/")
    token = cfg.get("access_token") or os.environ.get("MASTODON_ACCESS_TOKEN", "")
    if not instance or not token:
        raise ValueError("mastodon: instance_url and access_token are required")
    # Mastodon has a 500-char limit; strip HTML tags for plain text
    plain = re.sub(r"<[^>]+>", "", message)[:500]
    _http_post(
        f"{instance}/api/v1/statuses",
        {"status": plain, "visibility": cfg.get("visibility", "public")},
        headers={"Authorization": f"Bearer {token}"},
    )
    log.info("Mastodon: posted")


def send_signal(cfg: dict, message: str) -> None:
    # Requires signal-cli REST API: https://github.com/bbernhard/signal-rest-api
    api_url = cfg.get("api_url", "http://localhost:8080").rstrip("/")
    number = cfg.get("number") or os.environ.get("SIGNAL_NUMBER", "")
    recipients = cfg.get("recipients", [])
    if not number or not recipients:
        raise ValueError("signal: number and recipients are required")
    plain = re.sub(r"<[^>]+>", "", message)
    _http_post(
        f"{api_url}/v2/send",
        {"message": plain, "number": number, "recipients": recipients},
    )
    log.info("Signal: sent to %s", recipients)


def send_matrix(cfg: dict, message: str) -> None:
    homeserver = cfg.get("homeserver", "").rstrip("/")
    token = cfg.get("access_token") or os.environ.get("MATRIX_ACCESS_TOKEN", "")
    room_id = cfg.get("room_id") or os.environ.get("MATRIX_ROOM_ID", "")
    if not homeserver or not token or not room_id:
        raise ValueError("matrix: homeserver, access_token, and room_id are required")
    from urllib.parse import quote
    txn_id = uuid.uuid4().hex
    plain = re.sub(r"<[^>]+>", "", message)
    _http_put(
        f"{homeserver}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/send/m.room.message/{txn_id}",
        {"msgtype": "m.text", "body": plain, "format": "org.matrix.custom.html",
         "formatted_body": message},
        headers={"Authorization": f"Bearer {token}"},
    )
    log.info("Matrix: sent to %s", room_id)


_SENDERS = {
    "telegram": send_telegram,
    "mastodon": send_mastodon,
    "signal":   send_signal,
    "matrix":   send_matrix,
}


def notify_platforms(platforms: list[str], config: dict, bookmark: dict, ai_result: dict) -> None:
    message = format_notify_message(bookmark, ai_result)
    for platform in platforms:
        sender = _SENDERS.get(platform)
        if sender is None:
            log.warning("Unknown notify platform: %r (valid: %s)", platform, list(_SENDERS))
            continue
        cfg = config.get(platform, {})
        try:
            sender(cfg, message)
        except Exception as exc:
            log.error("%s: failed to send — %s", platform, exc)


# ---------------------------------------------------------------------------
# URL tag extraction
# ---------------------------------------------------------------------------

_GITHUB_RE = re.compile(r"github\.com/([^/?#]+)/([^/?#]+)", re.IGNORECASE)


def extract_url_tags(url: str) -> list[str]:
    """Return structured tags derived from the URL itself (not AI-generated)."""
    m = _GITHUB_RE.search(url)
    if m:
        owner, repo = m.group(1), m.group(2).removesuffix(".git")
        return [owner.lower(), repo.lower()]
    return []


def process_bookmark(bookmark: dict, group: dict, config: dict) -> None:
    url = bookmark["url"]
    title = bookmark.get("title", "")
    action = group.get("action", "")
    target = group.get("target", "Inbox")

    ok, err = validate_url(url)
    if not ok:
        log.error("Skipping — invalid or unreachable URL (%s): %s", err, url)
        return

    log.info("Processing [%s] → %s: %s", action, target, url)

    ai_result: dict = {}
    if action in ("summary", "tags", "tags+summary"):
        oa_cfg = config.get("openai", {})
        api_key = oa_cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            log.error("No OpenAI API key. Set openai.api_key in config or OPENAI_API_KEY env var.")
        else:
            client = OpenAI(api_key=api_key)
            model = oa_cfg.get("model", "gpt-4o-mini")
            language = group.get("language") or oa_cfg.get("language", "Ukrainian")
            ai_result = call_openai(client, url, title, action, model, language)

    url_tags = extract_url_tags(url)
    if url_tags:
        existing = ai_result.get("tags", [])
        # url_tags first so owner/repo appear at the top of the tag list
        ai_result["tags"] = url_tags + [t for t in existing if t not in url_tags]

    obs_cfg = config.get("obsidian", {})
    vault_path = Path(obs_cfg.get("vault_path", "~/Documents/Obsidian")).expanduser()
    save_to_obsidian(vault_path, target, bookmark, ai_result)

    notify = group.get("notify", [])
    if notify:
        notify_platforms(notify, config, bookmark, ai_result)


def snapshot_state(bookmarks_file: Path, state: dict, state_file: Path) -> int:
    """Mark all current bookmarks as seen without processing them."""
    try:
        bookmarks = load_safari_bookmarks(bookmarks_file)
    except Exception as exc:
        log.error("Failed to read bookmarks: %s", exc)
        return 0

    seen: dict = state.setdefault("seen", {})
    added = 0
    for bm in bookmarks:
        key = hashlib.sha1(bm["url"].encode()).hexdigest()
        if key not in seen:
            seen[key] = {"url": bm["url"], "first_seen": datetime.now().isoformat()}
            added += 1

    save_state(state, state_file)
    log.info("Init complete — %d bookmark(s) recorded as baseline (none processed)", added)
    return added


def scan_and_process(bookmarks_file: Path, config: dict, state: dict, state_file: Path) -> int:
    try:
        bookmarks = load_safari_bookmarks(bookmarks_file)
    except Exception as exc:
        log.error("Failed to read bookmarks: %s", exc)
        return 0

    seen: dict = state.setdefault("seen", {})
    groups: list = config.get("groups", [])
    processed = 0

    for bm in bookmarks:
        url = bm["url"]
        key = hashlib.sha1(url.encode()).hexdigest()
        if key in seen:
            continue

        seen[key] = {"url": url, "first_seen": datetime.now().isoformat()}
        processed += 1

        group = match_group(url, groups)
        if group:
            try:
                process_bookmark(bm, group, config)
            except Exception as exc:
                log.error("Error processing %s: %s", url, exc)
        else:
            log.debug("No group matched: %s", url)

    if processed:
        save_state(state, state_file)
        log.info("Done — processed %d new bookmark(s)", processed)
    return processed


# ---------------------------------------------------------------------------
# File watcher
# ---------------------------------------------------------------------------

class _BookmarksHandler(FileSystemEventHandler):
    def __init__(self, bookmarks_file: Path, config: dict, state: dict, state_file: Path):
        self._file = bookmarks_file
        self._config = config
        self._state = state
        self._state_file = state_file
        self._last_run: float = 0

    def on_modified(self, event):
        if Path(event.src_path) != self._file:
            return
        now = time.time()
        if now - self._last_run < 3:  # debounce
            return
        self._last_run = now
        log.info("Bookmarks file changed — scanning…")
        time.sleep(0.5)  # wait for Safari to finish writing
        scan_and_process(self._file, self._config, self._state, self._state_file)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def process_url(url: str, config: dict, group_override: Optional[str] = None) -> None:
    """Process a single URL from the command line."""
    groups: list = config.get("groups", [])

    if group_override:
        group = next((g for g in groups if g.get("name") == group_override), None)
        if group is None:
            log.error("Group %r not found in config. Available: %s",
                      group_override, [g.get("name") for g in groups])
            sys.exit(1)
    else:
        group = match_group(url, groups)
        if group is None:
            log.error("No group matched for: %s\nUse --group to specify one explicitly.", url)
            sys.exit(1)

    log.info("Matched group: %s", group.get("name", "(unnamed)"))
    process_bookmark({"url": url, "title": ""}, group, config)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Watch Safari bookmarks and save new links to Obsidian via OpenAI."
    )
    parser.add_argument("url", nargs="?", metavar="URL",
                        help="Process a single URL and exit")
    parser.add_argument("--group", metavar="NAME",
                        help="Force a specific group for the given URL (skips pattern matching)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, metavar="FILE")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, metavar="FILE")
    parser.add_argument("--bookmarks", type=Path, default=SAFARI_BOOKMARKS, metavar="FILE")
    parser.add_argument("--once", action="store_true", help="Scan once and exit")
    parser.add_argument(
        "--init", action="store_true",
        help="Snapshot current bookmarks as baseline (mark all seen, process none). "
             "Run once before the first watch so only future additions are processed.",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Clear seen-state so all current bookmarks are (re)processed",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.config.exists():
        log.error("Config not found: %s\nCopy config.example.yaml → config.yaml and edit it.", args.config)
        sys.exit(1)

    config = load_config(args.config)

    if args.url:
        process_url(args.url, config, group_override=args.group)
        return

    state: dict = {} if args.reset else load_state(args.state)

    if args.init:
        snapshot_state(args.bookmarks, state, args.state)
        return

    if args.once or args.reset:
        scan_and_process(args.bookmarks, config, state, args.state)
        return

    # Initial scan on startup — only delta since last run
    scan_and_process(args.bookmarks, config, state, args.state)

    # Watch for changes
    handler = _BookmarksHandler(args.bookmarks, config, state, args.state)
    observer = Observer()
    observer.schedule(handler, str(args.bookmarks.parent), recursive=False)
    observer.start()
    log.info("Watching %s  (Ctrl-C to stop)", args.bookmarks)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()

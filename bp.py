# bot.py
import os
import re
import json
import asyncio
from urllib.parse import urlparse, parse_qs, urljoin

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

# ---------- HTTP config ----------
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0.0.0 Safari/537.36")
}
TIMEOUT = 30

# ---------- URL patterns ----------
LINKSHORTIFY_RE = re.compile(r"https?://(?:www\.)?linkshortify\.com/full\?[^ \n]+", re.I)
TEJTIME_RE      = re.compile(r"https?://(?:www\.)?info\.tejtime24\.com/[^ \n]+", re.I)
LKSFY_RE        = re.compile(r"https?://(?:www\.)?lksfy\.com/[^ \n]+", re.I)

# Episode label detector: "Episode 01-06", "Ep 7–12", "E03", optionally with "Season x"
EP_LABEL_RE = re.compile(
    r"(?:season\s*\d+\s*)?(?:episodes?|ep|e)\s*\d+(?:\s*[-–—]\s*\d+)?",
    re.I
)

INVISIBLE = "\u2063"  # zero-width char so the message has no visible text

# ---------- Core helpers ----------
def uni(url: str) -> str:
    """Bypass lksfy shortlink -> return final page URL using your API."""
    res = requests.post(
        "https://freeseptemberapi.vercel.app/bypass",
        json={"url": url},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    data = res.text
    # If API returns an error JSON/Text containing "message", pass it back
    if "message" in data:
        return data
    return json.loads(data)["url"]

def extract_id_from_url(u: str, body: str = "") -> str | None:
    qs = parse_qs(urlparse(u).query)
    if "id" in qs and qs["id"]:
        return qs["id"][0]

    m = re.search(r"[?&]id=([A-Za-z0-9]+)", u)
    if m:
        return m.group(1)

    m = re.search(r"id=([A-Za-z0-9]+)", body)
    if m:
        return m.group(1)

    return None

def linkshortify_to_lksfy(start_url: str) -> str:
    """Follow linkshortify -> tejtime24 and build lksfy URL."""
    r = requests.get(start_url, headers=HEADERS, allow_redirects=True, timeout=TIMEOUT)
    final_url = r.url
    lid = extract_id_from_url(final_url, r.text)
    if not lid:
        raise ValueError("Could not find ?id=… in redirected page.")
    return f"https://lksfy.com/{lid}"

def _clean_tg_url(u: str) -> str | None:
    """
    Keep only the URL part, strip trailing quotes/tags/spaces.
    Accepts https://t.me/... or tg://...
    """
    m = re.match(r'^\s*(https://t\.me/[^\s"\'<>]+|tg://[^\s"\'<>]+)', u, re.I)
    return m.group(1) if m else None

def _collapse_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def _guess_label_for_anchor(a_tag) -> str:
    """
    Find a human label (e.g., "Episode 01-06") close to the anchor:
      - anchor text
      - previous siblings
      - parents (up 4 levels)
      - previous siblings of those parents
    """
    # 1) anchor text
    txt = _collapse_spaces(a_tag.get_text(" ", strip=True))
    m = EP_LABEL_RE.search(txt)
    if m:
        return _collapse_spaces(m.group(0)).title()

    # 2) walk previous siblings (few hops)
    prev = a_tag
    for _ in range(8):
        prev = getattr(prev, "previous_sibling", None)
        if not prev:
            break
        if hasattr(prev, "get_text"):
            t2 = _collapse_spaces(prev.get_text(" ", strip=True))
            m2 = EP_LABEL_RE.search(t2)
            if m2:
                return _collapse_spaces(m2.group(0)).title()

    # 3) climb parents and check their text + nearby previous siblings
    node = a_tag
    for _ in range(4):
        node = getattr(node, "parent", None)
        if not node:
            break
        if hasattr(node, "get_text"):
            t3 = _collapse_spaces(node.get_text(" ", strip=True))
            m3 = EP_LABEL_RE.search(t3)
            if m3:
                return _collapse_spaces(m3.group(0)).title()
        # prev siblings of this parent
        ps = getattr(node, "previous_sibling", None)
        hops = 0
        while ps and hops < 6:
            if hasattr(ps, "get_text"):
                t4 = _collapse_spaces(ps.get_text(" ", strip=True))
                m4 = EP_LABEL_RE.search(t4)
                if m4:
                    return _collapse_spaces(m4.group(0)).title()
            ps = getattr(ps, "previous_sibling", None)
            hops += 1

    return "Episode ?"

def extract_labeled_tg_links(page_url: str) -> dict[str, list[str]]:
    """
    Parse the final page and return {label: [tg links]}.
    Uses only real <a href="..."> to avoid messy tails like '">TG'.
    """
    r = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    grouped: dict[str, list[str]] = {}
    seen_global = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        clean = _clean_tg_url(href)
        if not clean:
            continue
        if clean in seen_global:
            continue
        seen_global.add(clean)

        label = _guess_label_for_anchor(a)
        grouped.setdefault(label, []).append(clean)

    return grouped

def _label_sort_key(label: str):
    # sort by first number in label; unknown at the end
    nums = re.findall(r"\d+", label)
    return int(nums[0]) if nums else 10**9

def extract_labeled_tg_from_any_input(url: str) -> tuple[str, dict[str, list[str]]]:
    """
    Resolve linkshortify/tejtime/lksfy -> final page URL, then extract grouped TG links.
    Returns: (final_page_url_or_error_text, {label: [links]})
    """
    url = url.strip()
    try:
        if LINKSHORTIFY_RE.match(url):
            lksfy = linkshortify_to_lksfy(url)
            final_page = uni(lksfy)
        elif TEJTIME_RE.match(url):
            lid = extract_id_from_url(url, "")
            if not lid:
                raise ValueError("Could not extract id from tejtime24 URL.")
            final_page = uni(f"https://lksfy.com/{lid}")
        elif LKSFY_RE.match(url):
            final_page = uni(url)
        else:
            raise ValueError("Please send a linkshortify / tejtime24 / lksfy URL.")
    except Exception as e:
        return (f"[ERROR] {e}", {})

    # If bypass API returned an error message string
    if isinstance(final_page, str) and "message" in final_page.lower():
        return (f"[BYPASS API] {final_page}", {})

    try:
        grouped = extract_labeled_tg_links(final_page)
        return (final_page, grouped)
    except Exception as e:
        return (f"[ERROR extracting TG links] {e}", {})

# ---------- Bot handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a linkshortify / tejtime24 / lksfy link and I’ll return Telegram links, "
        "grouped by episode, as buttons only.\n\nExample:\n"
        "https://linkshortify.com/full?api=...&enc=1"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()

    # pick one supported URL from the message
    url = None
    for rx in (LINKSHORTIFY_RE, TEJTIME_RE, LKSFY_RE):
        m = rx.search(text)
        if m:
            url = m.group(0)
            break
    if not url:
        await update.message.reply_text("Please send a valid linkshortify / tejtime24 / lksfy URL.")
        return

    await update.message.chat.send_action("typing")

    final_page, grouped = await asyncio.to_thread(extract_labeled_tg_from_any_input, url)

    # error surfaced
    if not grouped:
        await update.message.reply_text(final_page)
        return

    # Send one message per label with inline buttons (2 per row), caption = label only
    for label in sorted(grouped.keys(), key=_label_sort_key):
        urls = grouped[label]
        buttons = [InlineKeyboardButton(f"TG {i}", url=u) for i, u in enumerate(urls, 1)]
        rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]

        # Telegram keyboards can get big; if too many rows, split across messages
        MAX_ROWS = 25
        for i in range(0, len(rows), MAX_ROWS):
            kb = InlineKeyboardMarkup(rows[i:i+MAX_ROWS])
            cap = f"<b>{label}</b>" if i == 0 else INVISIBLE  # label only on first block
            await update.message.reply_text(cap, parse_mode=ParseMode.HTML, reply_markup=kb)

# ---------- Entrypoint ----------
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN environment variable first.")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()

if __name__ == "__main__":
    main()

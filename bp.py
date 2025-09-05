# bot.py
import os
import re
import json
import asyncio
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ---------- HTTP config ----------
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0.0.0 Safari/537.36")
}
TIMEOUT = 30
INVISIBLE = "\u2063"  # zero-width char (lets us send messages with no visible caption)

# ---------- URL patterns ----------
LINKSHORTIFY_RE = re.compile(r"https?://(?:www\.)?linkshortify\.com/full\?[^ \n]+", re.I)
TEJTIME_RE      = re.compile(r"https?://(?:www\.)?info\.tejtime24\.com/[^ \n]+", re.I)
LKSFY_RE        = re.compile(r"https?://(?:www\.)?lksfy\.com/[^ \n]+", re.I)

# Episode label detector, e.g., "Episode 01-06", "Ep 7–12", "E03", optional "Season x"
EP_LABEL_RE = re.compile(
    r"(?:season\s*\d+\s*)?(?:episodes?|ep|e)\s*\d+(?:\s*[-–—]\s*\d+)?",
    re.I
)

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

def _collapse_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def _guess_label_for_anchor(a_tag) -> str:
    """
    Find a human label (e.g., "Episode 01-06") close to the anchor:
      - anchor text
      - previous siblings
      - parents (up 4 levels) and their previous siblings
    """
    txt = _collapse_spaces(a_tag.get_text(" ", strip=True))
    m = EP_LABEL_RE.search(txt)
    if m:
        return _collapse_spaces(m.group(0)).title()

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

def _clean_tg_url(u: str) -> str | None:
    m = re.match(r'^\s*(https://t\.me/[^\s"\'<>]+|tg://[^\s"\'<>]+)', u, re.I)
    return m.group(1) if m else None

def _clean_drive_url(u: str) -> str | None:
    # Accept common Google Drive/doc variants
    m = re.match(
        r'^\s*(https://(?:drive\.google\.com|docs\.google\.com|drive\.usercontent\.google\.com)/[^\s"\'<>]+)',
        u, re.I
    )
    return m.group(1) if m else None

def extract_title_and_labeled_links(page_url: str) -> tuple[str, dict[str, dict[str, list[str]]]]:
    """
    Return (page_title, { episode_label: { "tg":[...], "drive":[...] } }).
    Only uses <a href="...">; clean + dedupe.
    """
    r = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    title = _collapse_spaces(getattr(getattr(soup, "title", None), "string", "") or "") or "All Links"

    grouped: dict[str, dict[str, list[str]]] = {}
    seen_tg, seen_drive = set(), set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        tg = _clean_tg_url(href)
        gd = _clean_drive_url(href)
        if not tg and not gd:
            continue

        label = _guess_label_for_anchor(a)
        slot = grouped.setdefault(label, {"tg": [], "drive": []})

        if tg and tg not in seen_tg:
            seen_tg.add(tg)
            slot["tg"].append(tg)
        if gd and gd not in seen_drive:
            seen_drive.add(gd)
            slot["drive"].append(gd)

    return (title, grouped)

def _label_sort_key(label: str):
    nums = re.findall(r"\d+", label)
    return int(nums[0]) if nums else 10**9

def extract_everything_from_any_input(url: str) -> tuple[str, str, dict[str, dict[str, list[str]]]]:
    """
    Resolve linkshortify/tejtime/lksfy -> final page URL,
    then extract page title + grouped TG/Drive links.
    Returns: (final_page_url_or_error_text, page_title_or_default, grouped)
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
        return (f"[ERROR] {e}", "All Links", {})

    if isinstance(final_page, str) and "message" in final_page.lower():
        return (f"[BYPASS API] {final_page}", "All Links", {})

    try:
        title, grouped = extract_title_and_labeled_links(final_page)
        return (final_page, title, grouped)
    except Exception as e:
        return (f"[ERROR extracting links] {e}", "All Links", {})

def _abbr_label(label: str) -> str:
    """Turn 'Episode 01-06' -> 'E01-06', 'Episode 37' -> 'E37', etc."""
    m = re.search(r'(?:episode|ep|e)\s*(\d+)(?:\s*[-–—]\s*(\d+))?', label, re.I)
    if not m:
        return label
    a = int(m.group(1))
    b = m.group(2)
    return f"E{a:02d}-{int(b):02d}" if b else f"E{a:02d}"

# ---------- Bot handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a linkshortify / tejtime24 / lksfy link.\n"
        "I’ll return ALL Telegram + Drive links as buttons under ONE caption."
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()

    # pick supported URL
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

    final_page, title, grouped = await asyncio.to_thread(extract_everything_from_any_input, url)

    if not grouped:
        await update.message.reply_text(final_page)
        return

    # Build ONE keyboard with all links (TG first per episode, then Drive),
    # button text includes episode tag to keep context (e.g., "E37 • TG 1")
    buttons = []
    for label in sorted(grouped.keys(), key=_label_sort_key):
        slot = grouped[label]
        tag = _abbr_label(label)

        # TG first
        for i, u in enumerate(slot.get("tg", []), 1):
            buttons.append(InlineKeyboardButton(f"{tag} • TG {i}", url=u))
        # then Drive
        for i, u in enumerate(slot.get("drive", []), 1):
            buttons.append(InlineKeyboardButton(f"{tag} • Drive {i}", url=u))

    if not buttons:
        await update.message.reply_text("No Telegram/Drive links found.")
        return

    # Split into multiple messages if too many rows (2 buttons/row)
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    MAX_ROWS = 25
    for i in range(0, len(rows), MAX_ROWS):
        kb = InlineKeyboardMarkup(rows[i:i+MAX_ROWS])
        cap = f"<b>{title}</b>" if i == 0 else INVISIBLE  # ONE caption only
        await update.message.reply_text(cap, parse_mode=ParseMode.HTML, reply_markup=kb)

# ---------- Error/webhook handling ----------
async def on_startup(app):
    # Ensure polling isn't killed by an old webhook
    await app.bot.delete_webhook(drop_pending_updates=True)

async def error_handler(update, context):
    try:
        raise context.error
    except TelegramError as e:
        print(f"[TelegramError] {e}")
    except Exception as e:
        print(f"[Unhandled] {e}")

# ---------- Entrypoint ----------
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN environment variable first.")

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(on_startup)
        .build()
    )
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

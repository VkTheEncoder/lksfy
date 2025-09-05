import os
import re
import json
import asyncio
from urllib.parse import urlparse, parse_qs, urljoin

import requests
from bs4 import BeautifulSoup
from telegram.constants import ParseMode
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
import html

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)

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

TG_RE = re.compile(r"(https://t\.me/[^\s]+|tg://[^\s]+)", re.I)

# ---------- Core helpers (your logic) ----------
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

def _clean_tg_url(u: str) -> str | None:
    """
    Keep only the URL part, strip trailing quotes/tags/spaces.
    Accepts https://t.me/... or tg://...
    """
    m = re.match(r'^\s*(https://t\.me/[^\s"\'<>]+|tg://[^\s"\'<>]+)', u, re.I)
    return m.group(1) if m else None

def linkshortify_to_lksfy(start_url: str) -> str:
    """Follow linkshortify -> tejtime24 and build lksfy URL."""
    r = requests.get(start_url, headers=HEADERS, allow_redirects=True, timeout=TIMEOUT)
    final_url = r.url
    lid = extract_id_from_url(final_url, r.text)
    if not lid:
        raise ValueError("Could not find ?id=… in redirected page.")
    return f"https://lksfy.com/{lid}"

def _render_links_html(urls: list[str]) -> str:
    """
    Pretty HTML list like:
    <b>Telegram Links (N)</b>
    1. <a href="...">TG 1</a>
    2. <a href="...">TG 2</a>
    """
    lines = [f"<b>Telegram Links ({len(urls)})</b>"]
    for i, u in enumerate(urls, 1):
        # escape only text, keep URL raw for <a href="">
        lines.append(f'{i}. <a href="{u}">TG {i}</a>')
    return "\n".join(lines)


def extract_tg_links(page_url: str) -> list[str]:
    """
    Extract ONLY Telegram links from real <a href="..."> tags.
    Returns a de-duplicated, cleaned list preserving order.
    """
    r = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        clean = _clean_tg_url(href)
        if not clean:
            continue
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out

def _chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i+size]

def extract_tg_from_any_input(url: str) -> list[str]:
    """
    Accepts:
      - linkshortify URL   -> linkshortify_to_lksfy -> uni -> final page -> TG links
      - tejtime24 URL      -> lksfy(id from URL)     -> uni -> final page -> TG links
      - lksfy URL          -> uni -> final page -> TG links
    Returns list of TG links (may be empty).
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
            # Not a supported starting URL
            raise ValueError("Please send a linkshortify / tejtime24 / lksfy URL.")
    except Exception as e:
        # Surface the error in the bot response
        return [f"[ERROR] {e}"]

    # If bypass API returned an error message string
    if isinstance(final_page, str) and "message" in final_page.lower():
        return [f"[BYPASS API] {final_page}"]

    try:
        tg_links = extract_tg_links(final_page)
        return tg_links
    except Exception as e:
        return [f"[ERROR extracting TG links] {e}"]

# ---------- Bot handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a linkshortify / tejtime24 / lksfy link and I’ll return only the Telegram links.\n"
        "Example:\n"
        "https://linkshortify.com/full?api=...&enc=1"
    )

def _pick_first_url(text: str) -> str | None:
    # Prefer one of the supported hosts; fall back to any URL-like thing
    for rx in (LINKSHORTIFY_RE, TEJTIME_RE, LKSFY_RE):
        m = rx.search(text)
        if m:
            return m.group(0)
    # Generic fallback
    m = re.search(r"https?://[^\s]+", text)
    return m.group(0) if m else None

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
        await update.message.reply_text(
            "Please send a valid linkshortify / tejtime24 / lksfy URL."
        )
        return

    await update.message.chat.send_action("typing")

    # run resolution off the event loop
    tg_links = await asyncio.to_thread(extract_tg_from_any_input, url)

    # Surface errors nicely
    if not tg_links:
        await update.message.reply_text("No Telegram links found.")
        return
    if len(tg_links) == 1 and tg_links[0].startswith("["):
        await update.message.reply_text(tg_links[0])
        return

    # 1) Send a clean HTML list
    html_msg = _render_links_html(tg_links)
    # Telegram hard limit is ~4096 chars for text
    if len(html_msg) <= 4096:
        await update.message.reply_text(html_msg, parse_mode=ParseMode.HTML)
    else:
        # split into multiple messages if needed
        chunks = []
        cur = []
        cur_len = 0
        for i, u in enumerate(tg_links, 1):
            line = f'{i}. <a href="{u}">TG {i}</a>'
            if (cur_len + len(line) + 1) > 3800:  # keep some buffer
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            cur.append(line)
            cur_len += len(line) + 1
        if cur:
            chunks.append("\n".join(cur))

        await update.message.reply_text(
            f"<b>Telegram Links ({len(tg_links)})</b>",
            parse_mode=ParseMode.HTML,
        )
        for c in chunks:
            await update.message.reply_text(c, parse_mode=ParseMode.HTML)

    # 2) Also send Inline Keyboard buttons (clean + professional)
    #    Telegram allows up to 8 buttons per row; we’ll do 2 per row.
    rows = []
    for pair in _chunk(tg_links, 2):
        rows.append([InlineKeyboardButton(f"TG {i+1+len(rows)*2}", url=u)
                     for i, u in enumerate(pair)])
    kb = InlineKeyboardMarkup(rows)
    await update.message.reply_text("Quick access:", reply_markup=kb)
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

import json
import re
from urllib.parse import urlparse, parse_qs, urljoin
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def uni(url: str) -> str:
    """Bypass lksfy shortlink -> return final URL."""
    res = requests.post(
        "https://freeseptemberapi.vercel.app/bypass",
        json={"url": url},
        headers=HEADERS,
        timeout=30,
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
    r = requests.get(start_url, headers=HEADERS, allow_redirects=True, timeout=30)
    final_url = r.url
    lid = extract_id_from_url(final_url, r.text)
    if not lid:
        raise ValueError("Could not find ?id=… in redirected page.")
    return f"https://lksfy.com/{lid}"

def extract_tg_links(page_url: str):
    res = requests.get(page_url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    tg_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("https://t.me/") or href.startswith("tg://"):
            tg_links.append(href)

    return tg_links

if __name__ == "__main__":
    # Step 1: Your starting linkshortify link
    start_url = "https://linkshortify.com/full?api=8308c89feff85b167a3804f3e9c3cc716c235f49&url=031bb8c7ccabff5a70d2d8dfcd0afccdd2840a541c7c7bb958628089f6494e9f269187e8f5a3847a13ea6bd4563671c6&type=2&enc=1"

    # Step 2: linkshortify -> lksfy
    lksfy_url = linkshortify_to_lksfy(start_url)
    print("lksfy URL:", lksfy_url)

    # Step 3: bypass lksfy -> final page
    final_url = uni(lksfy_url)
    print("Final page URL:", final_url)

    # Step 4: extract only Telegram links
    tg_links = extract_tg_links(final_url)
    print("\nTelegram Links Found:")
    for link in tg_links:
        print(link)

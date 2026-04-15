"""
Sources — fetches deals from multiple feeds.
v3: adds X/Twitter (price error accounts), Camelcamelcamel watchlist,
and Brickseek clearance scraper.
"""
import os
import re
import html
import json
import time
import requests
import feedparser
from pathlib import Path

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
SCRAPER_API_BASE = "https://api.scraperapi.com/"


def _strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _scraper_get(target_url, timeout=25):
    """Route a request through ScraperAPI to dodge IP blocks."""
    if not SCRAPER_API_KEY:
        return None
    try:
        r = requests.get(SCRAPER_API_BASE, params={
            "api_key": SCRAPER_API_KEY,
            "url": target_url,
            "country_code": "us",
        }, timeout=timeout)
        return r.text if r.status_code == 200 else None
    except Exception as e:
        print(f"  ScraperAPI error for {target_url[:60]}: {e}")
        return None


# =============================================================
# SLICKDEALS
# =============================================================

def fetch_slickdeals_frontpage():
    url = ("https://slickdeals.net/newsearch.php"
           "?mode=frontpage&searcharea=deals&searchin=first&rss=1")
    return _parse_feed(url, "Slickdeals-FP")


def fetch_slickdeals_popular():
    url = ("https://slickdeals.net/newsearch.php"
           "?mode=popdeals&searcharea=deals&searchin=first&rss=1")
    return _parse_feed(url, "Slickdeals-Pop")


def _parse_feed(url, source_name):
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
    except Exception as e:
        print(f"{source_name} failed: {e}")
        return []
    deals = []
    for entry in feed.entries[:40]:
        deals.append({
            "source": source_name,
            "title": entry.title,
            "url": entry.link,
            "description": _strip_html(entry.get("summary", ""))[:300],
        })
    return deals


# =============================================================
# DEALNEWS / WOOT
# =============================================================

def fetch_dealnews():
    url = "https://www.dealnews.com/rss/all-deals.rss"
    return _parse_feed(url, "DealNews")


def fetch_woot():
    url = "https://www.woot.com/feed/rss"
    return _parse_feed(url, "Woot")


# =============================================================
# X / TWITTER (via RSS feeds — configured in x_feeds.txt)
# =============================================================

def fetch_x_accounts():
    """
    Reads RSS feed URLs from x_feeds.txt (one per line, # for comments).
    These come from rss.app or any other X-to-RSS service.
    """
    feeds_file = Path("x_feeds.txt")
    if not feeds_file.exists():
        return []

    feeds = []
    for line in feeds_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Each line: name|url  OR just url
        if "|" in line:
            name, url = line.split("|", 1)
            feeds.append((name.strip(), url.strip()))
        else:
            feeds.append(("X", line))

    deals = []
    for name, url in feeds:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
            for entry in feed.entries[:20]:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                # Skip retweets and replies (they usually start with "RT" or "@")
                if title.startswith("RT @") or title.startswith("@"):
                    continue
                deals.append({
                    "source": f"X-{name}",
                    "title": title[:280],
                    "url": entry.get("link", url),
                    "description": _strip_html(entry.get("summary", ""))[:300],
                })
        except Exception as e:
            print(f"X feed {name} failed: {e}")

    return deals


# =============================================================
# CAMELCAMELCAMEL (price-drop watchlist via RSS)
# =============================================================

def fetch_camelcamelcamel():
    """
    Reads CCC RSS feed URLs from ccc_feeds.txt.
    Each line: ASIN|target_price|RSS_URL  (target_price is informational)
    """
    feeds_file = Path("ccc_feeds.txt")
    if not feeds_file.exists():
        return []

    deals = []
    for line in feeds_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        asin, target_price, url = parts[0].strip(), parts[1].strip(), parts[2].strip()

        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
            for entry in feed.entries[:10]:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                deals.append({
                    "source": "CCC-Watchlist",
                    "title": f"[{asin}] {title}",
                    "url": entry.get("link") or f"https://www.amazon.com/dp/{asin}",
                    "description": (f"Watchlist target: ${target_price}. "
                                    + _strip_html(entry.get("summary", "")))[:300],
                    "asin": asin,
                    "target_price": float(target_price) if target_price else None,
                })
        except Exception as e:
            print(f"CCC feed for {asin} failed: {e}")

    return deals


# =============================================================
# BRICKSEEK (Walmart/Target/Best Buy clearance scraper)
# =============================================================

def fetch_brickseek():
    """
    Scrape Brickseek's public clearance feeds via ScraperAPI.
    These are aggregated price-drop alerts from major retailers.
    """
    deals = []
    targets = [
        ("Brickseek-Walmart", "https://brickseek.com/walmart-inventory-checker/"),
        ("Brickseek-Target", "https://brickseek.com/target-inventory-checker/"),
    ]

    for source, url in targets:
        html_text = _scraper_get(url)
        if not html_text:
            continue

        # Brickseek lists deals in cards with title + price; structure varies.
        # Extract anything that looks like "<a href="...">Product Name</a>" near a "$X.XX"
        items = re.findall(
            r'<a[^>]+href="([^"]+)"[^>]*>([^<]{10,120})</a>[\s\S]{0,500}?\$(\d{1,4}\.\d{2})',
            html_text
        )
        seen_urls = set()
        for href, title, price in items[:20]:
            if href in seen_urls:
                continue
            seen_urls.add(href)
            full_url = href if href.startswith("http") else f"https://brickseek.com{href}"
            title = _strip_html(title).strip()
            if len(title) < 5:
                continue
            deals.append({
                "source": source,
                "title": f"{title} - ${price}",
                "url": full_url,
                "description": f"Clearance price: ${price}",
            })

    return deals


# =============================================================
# REDDIT (kept for if you ever get OAuth approved)
# =============================================================

_reddit_token_cache = {"token": None, "expires": 0}


def _reddit_token():
    if _reddit_token_cache["token"] and _reddit_token_cache["expires"] > time.time():
        return _reddit_token_cache["token"]

    cid = os.environ.get("REDDIT_CLIENT_ID")
    cs = os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not cs:
        return None

    try:
        r = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(cid, cs),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        token = data["access_token"]
        _reddit_token_cache["token"] = token
        _reddit_token_cache["expires"] = time.time() + data.get("expires_in", 3600) - 600
        return token
    except Exception as e:
        print(f"Reddit auth failed: {e}")
        return None


def fetch_reddit(subreddit):
    token = _reddit_token()
    if not token:
        return []  # silently skip if no creds
    url = f"https://oauth.reddit.com/r/{subreddit}/new?limit=25"
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}

    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        posts = r.json()["data"]["children"]
    except Exception as e:
        print(f"Reddit r/{subreddit} failed: {e}")
        return []

    deals = []
    for p in posts:
        d = p["data"]
        if d.get("stickied") or d.get("over_18"):
            continue
        deals.append({
            "source": f"r/{subreddit}",
            "title": d["title"],
            "url": d.get("url_overridden_by_dest") or f"https://reddit.com{d['permalink']}",
            "description": (d.get("selftext") or "")[:300],
            "upvotes": d.get("ups", 0),
            "comments": d.get("num_comments", 0),
        })
    return deals


# =============================================================
# AGGREGATOR
# =============================================================

def fetch_all():
    deals = []

    # Core RSS sources (always work, GitHub IPs allowed)
    deals += fetch_slickdeals_frontpage()
    deals += fetch_slickdeals_popular()
    deals += fetch_dealnews()
    deals += fetch_woot()

    # New: X/Twitter accounts (price errors first!)
    deals += fetch_x_accounts()

    # New: CCC watchlist (zero false positives — you set the prices)
    deals += fetch_camelcamelcamel()

    # New: Brickseek clearance (uses ScraperAPI quota)
    deals += fetch_brickseek()

    # Reddit (silently skipped if no creds)
    for sub in ["Flipping", "deals", "GameDeals", "buildapcsales", "MUAontheCheap"]:
        deals += fetch_reddit(sub)

    return deals

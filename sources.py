"""
Sources v4 — parallel feed fetching for speed.
All feed fetches run concurrently using ThreadPoolExecutor.
"""
import os
import re
import html
import time
import requests
import feedparser
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
SCRAPER_API_BASE = "https://api.scraperapi.com/"


def _strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _scraper_get(target_url, timeout=25):
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
# RSS-based sources (all same pattern)
# =============================================================

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
            "published": entry.get("published_parsed") or entry.get("updated_parsed"),
        })
    return deals


def fetch_slickdeals_frontpage():
    return _parse_feed(
        "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
        "Slickdeals-FP")


def fetch_slickdeals_popular():
    return _parse_feed(
        "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1",
        "Slickdeals-Pop")


def fetch_dealnews():
    return _parse_feed("https://www.dealnews.com/rss/all-deals.rss", "DealNews")


def fetch_woot():
    return _parse_feed("https://www.woot.com/feed/rss", "Woot")


# =============================================================
# X / Twitter via rss.app
# =============================================================

def fetch_x_accounts():
    feeds_file = Path("x_feeds.txt")
    if not feeds_file.exists():
        return []

    feeds = []
    for line in feeds_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            name, url = line.split("|", 1)
            feeds.append((name.strip(), url.strip()))
        else:
            feeds.append(("X", line.strip()))

    if not feeds:
        return []

    # Parallel fetch across X accounts
    deals = []
    with ThreadPoolExecutor(max_workers=min(5, len(feeds))) as ex:
        futures = {ex.submit(_parse_x_feed, name, url): name for name, url in feeds}
        for fut in as_completed(futures):
            try:
                deals.extend(fut.result())
            except Exception as e:
                print(f"X feed {futures[fut]} crashed: {e}")
    return deals


def _parse_x_feed(name, url):
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
    except Exception as e:
        print(f"X feed {name} failed: {e}")
        return []

    deals = []
    for entry in feed.entries[:20]:
        title = entry.get("title", "").strip()
        if not title:
            continue
        if title.startswith("RT @") or title.startswith("@"):
            continue
        deals.append({
            "source": f"X-{name}",
            "title": title[:280],
            "url": entry.get("link", url),
            "description": _strip_html(entry.get("summary", ""))[:300],
            "published": entry.get("published_parsed") or entry.get("updated_parsed"),
        })
    return deals


# =============================================================
# Camelcamelcamel watchlist
# =============================================================

def fetch_camelcamelcamel():
    feeds_file = Path("ccc_feeds.txt")
    if not feeds_file.exists():
        return []

    entries = []
    for line in feeds_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        entries.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))

    if not entries:
        return []

    deals = []
    with ThreadPoolExecutor(max_workers=min(8, len(entries))) as ex:
        futures = {ex.submit(_parse_ccc_feed, asin, tp, url): asin
                   for asin, tp, url in entries}
        for fut in as_completed(futures):
            try:
                deals.extend(fut.result())
            except Exception as e:
                print(f"CCC feed {futures[fut]} crashed: {e}")
    return deals


def _parse_ccc_feed(asin, target_price, url):
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
    except Exception as e:
        print(f"CCC feed {asin} failed: {e}")
        return []

    deals = []
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
            "published": entry.get("published_parsed") or entry.get("updated_parsed"),
        })
    return deals


# =============================================================
# Brickseek (via ScraperAPI)
# =============================================================

def fetch_brickseek():
    targets = [
        ("Brickseek-Walmart", "https://brickseek.com/walmart-inventory-checker/"),
        ("Brickseek-Target", "https://brickseek.com/target-inventory-checker/"),
    ]

    deals = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(_parse_brickseek, s, u): s for s, u in targets}
        for fut in as_completed(futures):
            try:
                deals.extend(fut.result())
            except Exception as e:
                print(f"Brickseek {futures[fut]} crashed: {e}")
    return deals


def _parse_brickseek(source, url):
    html_text = _scraper_get(url)
    if not html_text:
        return []

    deals = []
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
# Reddit (skipped if no OAuth)
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
        return []
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
            "published": d.get("created_utc"),
        })
    return deals


# =============================================================
# PARALLEL AGGREGATOR — the speed win
# =============================================================

def fetch_all():
    """
    Fetch every source in parallel. ~8 sources completing in the time of 1.
    Typical: 6-8 sec vs 20-30 sec serial.
    """
    tasks = [
        fetch_slickdeals_frontpage,
        fetch_slickdeals_popular,
        fetch_dealnews,
        fetch_woot,
        fetch_x_accounts,
        fetch_camelcamelcamel,
        fetch_brickseek,
    ]
    # Reddit subs each as separate tasks for more parallelism
    for sub in ["Flipping", "deals", "GameDeals", "buildapcsales", "MUAontheCheap"]:
        tasks.append(lambda s=sub: fetch_reddit(s))

    all_deals = []
    with ThreadPoolExecutor(max_workers=min(12, len(tasks))) as ex:
        futures = {ex.submit(task): task.__name__ if hasattr(task, "__name__") else "task"
                   for task in tasks}
        for fut in as_completed(futures):
            try:
                all_deals.extend(fut.result())
            except Exception as e:
                print(f"Source task {futures[fut]} crashed: {e}")
    return all_deals

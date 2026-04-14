"""
Sources — fetches deals from multiple feeds.
Each fetcher returns a list of dicts: {source, title, url, description, [upvotes, comments]}
"""
import os
import re
import html
import time
import requests
import feedparser

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) deal-hunter/2.0"


def _strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


# ---- Slickdeals ----

def fetch_slickdeals_frontpage():
    """Slickdeals Frontpage — community-curated, highest signal."""
    url = ("https://slickdeals.net/newsearch.php"
           "?mode=frontpage&searcharea=deals&searchin=first&rss=1")
    return _parse_feed(url, "Slickdeals-FP")


def fetch_slickdeals_popular():
    """Slickdeals Popular Deals — broader, includes hot non-frontpage stuff."""
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


# ---- DealNews RSS ----

def fetch_dealnews():
    """DealNews — editor-vetted deals, fewer affiliate spam."""
    url = "https://www.dealnews.com/rss/all-deals.rss"
    return _parse_feed(url, "DealNews")


# ---- Woot ----

def fetch_woot():
    """Woot — Amazon's daily deals site, often sourced for arbitrage."""
    url = "https://www.woot.com/feed/rss"
    return _parse_feed(url, "Woot")


# ---- Reddit (OAuth-authenticated to dodge 403 blocks) ----

_reddit_token_cache = {"token": None, "expires": 0}


def _reddit_token():
    """Get an OAuth bearer token. Cached ~50 min."""
    if _reddit_token_cache["token"] and _reddit_token_cache["expires"] > time.time():
        return _reddit_token_cache["token"]

    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None

    try:
        r = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(client_id, client_secret),
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
    """Fetch /new from a subreddit using OAuth if configured."""
    token = _reddit_token()
    if token:
        url = f"https://oauth.reddit.com/r/{subreddit}/new?limit=25"
        headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    else:
        url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=25"
        headers = {"User-Agent": USER_AGENT}

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


# ---- Aggregator ----

def fetch_all():
    deals = []
    deals += fetch_slickdeals_frontpage()
    deals += fetch_slickdeals_popular()
    deals += fetch_dealnews()
    deals += fetch_woot()
    for sub in ["Flipping", "deals", "GameDeals", "buildapcsales", "MUAontheCheap"]:
        deals += fetch_reddit(sub)
    return deals

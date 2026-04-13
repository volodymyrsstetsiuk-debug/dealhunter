"""
Deal Hunter — real-time monitor.
Runs every 10 minutes via GitHub Actions.
Tracks seen deals in seen.json (committed back to repo).
Sends instant Telegram alerts when new high-scoring deals appear.
"""

import os
import re
import html
import json
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

# ---- Config ----
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = Path("seen.json")
SEEN_TTL_DAYS = 7            # drop IDs older than this to keep file small
MIN_SCORE = 25               # minimum score to fire an alert
MAX_ALERTS_PER_RUN = 10      # spam guard (unlikely to hit)

# Brands/products to boost. Customize these for YOUR flip niches.
KEYWORDS_BOOST = [
    "lego", "pokemon", "pokémon", "nintendo", "switch", "playstation", "ps5",
    "xbox", "funko", "trading card",
    "apple", "iphone", "ipad", "macbook", "airpods", "apple watch",
    "dyson", "kitchenaid", "vitamix", "ninja", "instant pot", "breville",
    "milwaukee", "dewalt", "makita", "ryobi",
    "bose", "sony", "samsung",
    "clearance", "price error", "pricing error", "glitch",
    "75% off", "80% off", "85% off", "90% off",
]

# Drop deals matching any of these outright.
KEYWORDS_SKIP = [
    "credit card", "auto insurance", "car insurance", "mortgage",
    "mattress", "subscription", "streaming service", "web hosting",
    "vpn", "mint mobile", "phone plan",
]


# ---- State tracking ----

def load_seen():
    """Load seen deal IDs, pruning any older than SEEN_TTL_DAYS."""
    if not SEEN_FILE.exists():
        return {}
    try:
        data = json.loads(SEEN_FILE.read_text())
    except Exception:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_TTL_DAYS)).isoformat()
    return {k: v for k, v in data.items() if v > cutoff}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))


def deal_id(deal):
    """Stable unique ID for dedupe across runs."""
    return f"{deal['source']}::{deal['url']}"


# ---- Fetchers ----

def fetch_slickdeals():
    url = ("https://slickdeals.net/newsearch.php"
           "?mode=frontpage&searcharea=deals&searchin=first&rss=1")
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"Slickdeals fetch failed: {e}")
        return []
    deals = []
    for entry in feed.entries[:40]:
        summary = html.unescape(re.sub(r"<[^>]+>", "", entry.get("summary", "")))
        deals.append({
            "source": "Slickdeals",
            "title": entry.title,
            "url": entry.link,
            "description": summary[:300],
        })
    return deals


def fetch_reddit(subreddit):
    # /new.json surfaces brand-new posts — better for real-time than /top
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=25"
    headers = {"User-Agent": "DealHunter/1.0 (personal use)"}
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


# ---- Scoring ----

def score_deal(deal):
    text = (deal["title"] + " " + deal.get("description", "")).lower()

    if any(skip in text for skip in KEYWORDS_SKIP):
        return -1

    score = 0

    for kw in KEYWORDS_BOOST:
        if kw in text:
            score += 10

    pct_match = re.search(r"(\d{2,3})\s*%\s*off", text)
    if pct_match:
        pct = int(pct_match.group(1))
        if pct >= 50:
            score += pct // 5

    if "upvotes" in deal:
        score += min(deal["upvotes"] // 10, 30)
        score += min(deal.get("comments", 0) // 5, 15)

    if deal["source"] == "Slickdeals":
        score += 15

    if any(x in text for x in ["price error", "pricing error", "glitch"]):
        score += 30

    return score


def dedupe(deals):
    seen = set()
    result = []
    for d in deals:
        domain = urlparse(d["url"]).netloc.replace("www.", "")
        first_words = " ".join(d["title"].lower().split()[:4])
        key = (domain, first_words)
        if key in seen:
            continue
        seen.add(key)
        result.append(d)
    return result


# ---- Telegram ----

def escape_md(text):
    return (text.replace("*", "").replace("_", "")
                .replace("[", "(").replace("]", ")"))


def format_alert(deal):
    """One rich message per deal — lands as a normal Telegram notification."""
    title = escape_md(deal["title"][:200])
    lines = [
        f"🚨 *NEW DEAL* — score {deal['_score']}",
        "",
        f"[{title}]({deal['url']})",
        "",
        f"📍 {deal['source']}",
    ]
    if "upvotes" in deal:
        lines.append(f"📊 {deal['upvotes']}↑ · {deal['comments']}💬")
    if deal.get("description"):
        desc = escape_md(deal["description"][:220])
        lines.append(f"\n_{desc}_")
    lines.append("\n⚡ _Verify on Keepa + 3× rule before buying_")
    return "\n".join(lines)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    r = requests.post(url, data=data, timeout=15)
    if not r.ok:
        print(f"Telegram error {r.status_code}: {r.text}")


# ---- Main ----

def main():
    seen = load_seen()
    print(f"Loaded {len(seen)} previously-seen deals")

    all_deals = []
    all_deals += fetch_slickdeals()
    for sub in ["deals", "Flipping", "GameDeals", "buildapcsales"]:
        all_deals += fetch_reddit(sub)

    # Keep only NEW ones we haven't processed before
    new_deals = [d for d in all_deals if deal_id(d) not in seen]
    print(f"Fetched {len(all_deals)} total; {len(new_deals)} new since last run")

    for d in new_deals:
        d["_score"] = score_deal(d)

    # Pick alerts: score above threshold, deduped, top N by score
    alerts = [d for d in new_deals if d["_score"] >= MIN_SCORE]
    alerts = dedupe(alerts)
    alerts.sort(key=lambda x: x["_score"], reverse=True)
    alerts = alerts[:MAX_ALERTS_PER_RUN]

    now_iso = datetime.now(timezone.utc).isoformat()

    for d in alerts:
        print(f"  ALERT [{d['_score']}] {d['title'][:80]}")
        send_telegram(format_alert(d))

    # Mark every fetched deal as seen so we don't re-score them next run
    for d in all_deals:
        seen[deal_id(d)] = now_iso

    save_seen(seen)
    print(f"Sent {len(alerts)} alerts; tracking {len(seen)} seen IDs")


if __name__ == "__main__":
    main()

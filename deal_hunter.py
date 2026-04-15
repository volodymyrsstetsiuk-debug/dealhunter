"""
Deal Hunter v4 — parallelized + concise alerts + recency prioritized.

- All source fetches run concurrently
- All marketplace lookups per deal run concurrently
- Candidates sorted by newness so freshest deals alert first
- Telegram messages are tight — 5-7 lines, scanable in 3 seconds
- Raised MIN_SCORE for 5-min cron = higher signal, fewer false alarms
"""

import os
import re
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import sources
import enrich

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = Path("seen.json")
SEEN_TTL_DAYS = 7

# TUNED for 5-min cron + want-only-best-deals
MIN_SCORE_FOR_ENRICHMENT = 30   # was 20 — raises quality bar
MAX_ENRICHMENTS_PER_RUN = 5     # was 8 — keeps ScraperAPI burn manageable
MAX_ALERTS_PER_RUN = 10

KEYWORDS_BOOST = [
    "lego", "pokemon", "pokémon", "nintendo", "switch", "playstation", "ps5",
    "xbox", "funko", "trading card", "magic the gathering",
    "apple", "iphone", "ipad", "macbook", "airpods", "apple watch",
    "dyson", "kitchenaid", "vitamix", "ninja", "instant pot", "breville",
    "milwaukee", "dewalt", "makita", "ryobi", "ridgid",
    "bose", "sony", "samsung", "lg",
    "stanley", "yeti", "hydro flask",
    "nike", "adidas", "lululemon",
    "clearance", "price error", "pricing error", "glitch", "underpriced",
    "75% off", "80% off", "85% off", "90% off",
]

KEYWORDS_SKIP = [
    "credit card", "auto insurance", "car insurance", "mortgage",
    "mattress", "subscription", "streaming service", "web hosting",
    "vpn", "mint mobile", "phone plan", "ebook", "kindle book",
    "course", "masterclass", "udemy",
]


# ---- State ----

def load_seen():
    if not SEEN_FILE.exists():
        return {}
    try:
        data = json.loads(SEEN_FILE.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_TTL_DAYS)).isoformat()
    return {k: v for k, v in data.items() if v > cutoff}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))


def deal_id(deal):
    return f"{deal['source']}::{deal['url']}"


# ---- Scoring ----

def score_deal(deal):
    text = (deal["title"] + " " + deal.get("description", "")).lower()

    if any(skip in text for skip in KEYWORDS_SKIP):
        return -1

    score = 0
    for kw in KEYWORDS_BOOST:
        if kw in text:
            score += 10

    pct = re.search(r"(\d{2,3})\s*%\s*off", text)
    if pct:
        p = int(pct.group(1))
        if p >= 50:
            score += p // 5

    if "upvotes" in deal:
        score += min(deal["upvotes"] // 10, 30)
        score += min(deal.get("comments", 0) // 5, 15)

    src = deal["source"]
    if src == "CCC-Watchlist":
        score += 50
    elif src.startswith("X-"):
        score += 30
    elif src == "Slickdeals-FP":
        score += 20
    elif src.startswith("Brickseek-"):
        score += 18
    elif src == "DealNews":
        score += 12
    elif src == "Slickdeals-Pop":
        score += 10
    elif src == "Woot":
        score += 8

    if any(x in text for x in ["price error", "pricing error", "glitch", "underpriced"]):
        score += 35

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


# ---- Recency scoring — freshness as a signal ----

def recency_key(deal):
    """Sort key: newest deals first. struct_time from feedparser, float from reddit."""
    pub = deal.get("published")
    if pub is None:
        return 0
    try:
        if isinstance(pub, (int, float)):
            return float(pub)
        if hasattr(pub, "__iter__"):
            return time.mktime(pub)
    except Exception:
        pass
    return 0


# ---- Telegram (concise format) ----

def escape_md(s):
    return (s or "").replace("*", "").replace("_", "").replace("[", "(").replace("]", ")")


def format_alert(deal):
    """Tight 5-7 line format. Scanable in 3 seconds."""
    verdict = deal.get("verdict", "WATCH")
    emoji_map = {"BUY": "🟢", "WATCH": "🟡", "SKIP": "⚪"}
    emoji = emoji_map.get(verdict, "🟡")

    src = deal["source"]
    prefix = ""
    if src == "CCC-Watchlist":
        prefix = "🎯 "
    elif src.startswith("X-"):
        prefix = "⚡ "

    # Trim title hard — max 120 chars for scanability
    title = escape_md(deal["title"][:120])

    # Top line: verdict + profit (if known) + score
    header_parts = [f"{prefix}{emoji} *{verdict}*"]
    if deal.get("profit") is not None and deal["profit"] > 0:
        header_parts.append(f"+${deal['profit']:.0f}")
    header_parts.append(f"· {src}")
    header = " ".join(header_parts)

    lines = [header, "", f"*{title}*"]

    # One-line price summary
    price_bits = []
    if deal.get("deal_price"):
        price_bits.append(f"💵 ${deal['deal_price']:.0f}")
    if deal.get("amazon") and deal["amazon"].get("found"):
        price_bits.append(f"🟧 ${deal['amazon']['price']:.0f}")
    if deal.get("ebay") and deal["ebay"].get("found"):
        price_bits.append(f"🔵 ${deal['ebay']['median_price']:.0f} ({deal['ebay']['sold_count']} sold)")
    if deal.get("mercari") and deal["mercari"].get("found"):
        price_bits.append(f"🟣 ${deal['mercari']['median_price']:.0f}")
    if deal.get("google") and deal["google"].get("found"):
        price_bits.append(f"🔎 ${deal['google']['median_price']:.0f}")

    if price_bits:
        lines.append(" | ".join(price_bits))

    lines.append(f"[👉 Buy here]({deal['url']})")

    # Short reasoning line if present
    if deal.get("verdict_reason"):
        reason = escape_md(deal["verdict_reason"][:120])
        lines.append(f"_{reason}_")

    return "\n".join(lines)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        }, timeout=15)
        if not r.ok:
            print(f"Telegram error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"Telegram send failed: {e}")


# ---- Main ----

def main():
    run_start = time.time()
    seen = load_seen()
    print(f"Loaded {len(seen)} previously-seen deals")

    # ── Fetch all sources in parallel (v4 win #1)
    fetch_start = time.time()
    all_deals = sources.fetch_all()
    fetch_time = time.time() - fetch_start

    sc = {}
    for d in all_deals:
        sc[d["source"]] = sc.get(d["source"], 0) + 1
    print(f"Fetched {len(all_deals)} deals in {fetch_time:.1f}s: {sc}")

    # Filter to new
    new_deals = [d for d in all_deals if deal_id(d) not in seen]
    print(f"  {len(new_deals)} new since last run")

    # Score
    for d in new_deals:
        d["_score"] = score_deal(d)

    # Pick candidates — sort by (score desc, recency desc) so best AND freshest float up
    candidates = [d for d in new_deals if d["_score"] >= MIN_SCORE_FOR_ENRICHMENT]
    candidates = dedupe(candidates)
    candidates.sort(key=lambda x: (x["_score"], recency_key(x)), reverse=True)
    candidates = candidates[:MAX_ENRICHMENTS_PER_RUN]

    print(f"  {len(candidates)} candidates above score {MIN_SCORE_FOR_ENRICHMENT}; enriching in parallel...")

    # ── Enrich candidates in parallel (v4 win #2)
    # Each enrich_deal internally parallelizes its 4 marketplace lookups too (v4 win #3)
    enriched = []
    enrich_start = time.time()
    if candidates:
        with ThreadPoolExecutor(max_workers=min(5, len(candidates))) as ex:
            futures = {ex.submit(enrich.enrich_deal, d): d for d in candidates}
            for fut in as_completed(futures):
                try:
                    enriched.append(fut.result())
                except Exception as e:
                    d = futures[fut]
                    print(f"  Enrich failed '{d['title'][:60]}': {e}")
                    enriched.append({**d, "verdict": "WATCH",
                                     "verdict_reason": f"Error: {e}"})
    enrich_time = time.time() - enrich_start

    # Alert decisions — freshest first so you see newest before scrolling
    enriched.sort(key=recency_key, reverse=True)

    alerts = []
    for d in enriched:
        v = d.get("verdict")
        src = d["source"]
        if src == "CCC-Watchlist" or src.startswith("X-"):
            alerts.append(d)
        elif v == "BUY":
            alerts.append(d)
        elif v == "WATCH" and d["_score"] >= 45:  # stricter WATCH bar for 5-min cron
            alerts.append(d)

    alerts = alerts[:MAX_ALERTS_PER_RUN]

    now_iso = datetime.now(timezone.utc).isoformat()

    for d in alerts:
        print(f"  ALERT [{d['verdict']}] [{d['_score']}] {d['title'][:80]}")
        send_telegram(format_alert(d))

    for d in all_deals:
        seen[deal_id(d)] = now_iso

    save_seen(seen)
    total = time.time() - run_start
    print(f"Done in {total:.1f}s (fetch {fetch_time:.1f}s, enrich {enrich_time:.1f}s); "
          f"sent {len(alerts)} alerts; tracking {len(seen)} IDs")


if __name__ == "__main__":
    main()

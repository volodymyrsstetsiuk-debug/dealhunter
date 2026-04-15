"""
Deal Hunter v3 — multi-source + ScraperAPI Amazon/eBay verdict.
New in v3: X/Twitter price-error monitoring, CCC watchlist, Brickseek clearance.
"""

import os
import re
import json
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import sources
import enrich

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = Path("seen.json")
SEEN_TTL_DAYS = 7
MIN_SCORE_FOR_ENRICHMENT = 20
MAX_ENRICHMENTS_PER_RUN = 8
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
    # Source quality boost — higher = trusted/curated
    if src == "CCC-Watchlist":
        score += 50  # YOU set the trigger price = always relevant
    elif src.startswith("X-"):
        score += 30  # X price-error accounts = real-time edge
    elif src == "Slickdeals-FP":
        score += 20
    elif src.startswith("Brickseek-"):
        score += 18  # Real clearance with retailer pricing
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


# ---- Telegram ----

def escape_md(s):
    return (s or "").replace("*", "").replace("_", "").replace("[", "(").replace("]", ")")


def format_alert(deal):
    verdict = deal.get("verdict", "WATCH")
    emoji = {"BUY": "🟢 BUY", "WATCH": "🟡 WATCH", "SKIP": "⚪ SKIP"}.get(verdict, "🟡")

    # Source-specific prefix for the most exciting sources
    src = deal["source"]
    if src == "CCC-Watchlist":
        emoji = "🎯 WATCHLIST HIT — " + emoji
    elif src.startswith("X-"):
        emoji = "⚡ X ALERT — " + emoji

    title = escape_md(deal["title"][:200])
    lines = [
        f"{emoji} — score {deal['_score']}",
        "",
        f"*{title}*",
        f"[Open deal]({deal['url']})",
        "",
    ]

    if deal.get("deal_price"):
        lines.append(f"💵 Deal price: *${deal['deal_price']:.2f}*")

    amazon = deal.get("amazon")
    if amazon and amazon.get("found"):
        a_line = f"🟧 Amazon: *${amazon['price']:.2f}*"
        if amazon.get("url"):
            a_line += f" — [view]({amazon['url']})"
        lines.append(a_line)

    ebay = deal.get("ebay")
    if ebay and ebay.get("found"):
        lines.append(f"🔵 eBay sold (90d): median *${ebay['median_price']:.0f}* "
                     f"(range ${ebay['min_price']:.0f}–${ebay['max_price']:.0f}, "
                     f"{ebay['sold_count']} sold)")

    if deal.get("profit") is not None:
        lines.append(f"📊 Est. profit: *${deal['profit']:.2f}/unit*")

    if deal.get("verdict_reason"):
        lines.append(f"\n_{escape_md(deal['verdict_reason'])}_")

    lines.append(f"\n📍 {deal['source']}")
    return "\n".join(lines)


def send_telegram(message, silent=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
        "disable_notification": silent,
    }, timeout=15)
    if not r.ok:
        print(f"Telegram error {r.status_code}: {r.text[:200]}")


# ---- Main ----

def main():
    seen = load_seen()
    print(f"Loaded {len(seen)} previously-seen deals")

    all_deals = sources.fetch_all()
    sc = {}
    for d in all_deals:
        sc[d["source"]] = sc.get(d["source"], 0) + 1
    print(f"Fetched {len(all_deals)} deals from sources: {sc}")

    new_deals = [d for d in all_deals if deal_id(d) not in seen]
    print(f"  {len(new_deals)} new since last run")

    for d in new_deals:
        d["_score"] = score_deal(d)

    candidates = [d for d in new_deals if d["_score"] >= MIN_SCORE_FOR_ENRICHMENT]
    candidates = dedupe(candidates)
    candidates.sort(key=lambda x: x["_score"], reverse=True)
    candidates = candidates[:MAX_ENRICHMENTS_PER_RUN]

    print(f"  {len(candidates)} candidates above score {MIN_SCORE_FOR_ENRICHMENT}; enriching...")

    enriched = []
    for d in candidates:
        try:
            enriched.append(enrich.enrich_deal(d))
        except Exception as e:
            print(f"  Enrich failed for '{d['title'][:60]}': {e}")
            enriched.append({**d, "verdict": "WATCH",
                            "verdict_reason": f"Enrichment error: {e}"})

    # Alert decisions:
    #   - CCC watchlist hits: ALWAYS alert (you set the trigger)
    #   - X price-error alerts: ALWAYS alert (time-sensitive)
    #   - BUY verdict: alert
    #   - WATCH verdict: only if score very high (>= 40)
    alerts = []
    for d in enriched:
        v = d.get("verdict")
        src = d["source"]
        if src == "CCC-Watchlist" or src.startswith("X-"):
            alerts.append(d)
        elif v == "BUY":
            alerts.append(d)
        elif v == "WATCH" and d["_score"] >= 40:
            alerts.append(d)

    alerts = alerts[:MAX_ALERTS_PER_RUN]

    now_iso = datetime.now(timezone.utc).isoformat()

    for d in alerts:
        print(f"  ALERT [{d['verdict']}] [{d['_score']}] {d['title'][:80]}")
        send_telegram(format_alert(d))

    for d in all_deals:
        seen[deal_id(d)] = now_iso

    save_seen(seen)
    print(f"Sent {len(alerts)} alerts; tracking {len(seen)} seen IDs")


if __name__ == "__main__":
    main()

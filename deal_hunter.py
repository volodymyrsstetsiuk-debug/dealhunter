"""
Deal Hunter v5 — LLM extraction + stock check + tiered urgency.

Tiers (all alert immediately, just with different styling):
  🚨 EMERGENCY — profit ≥ $50, ROI ≥ 75%, demand confirmed (loud notification)
  🟢 STRONG    — profit ≥ $20, ROI ≥ 50%, demand confirmed (normal alert)
  🟡 NORMAL    — profit ≥ $8, ROI ≥ 30% OR passes 3× rule (silent notification)
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
MIN_SCORE_FOR_ENRICHMENT = 30
MAX_ENRICHMENTS_PER_RUN = 5
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


# ---- Scoring (unchanged from v4) ----

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


def recency_key(deal):
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


# ---- Telegram (tiered formatting) ----

def escape_md(s):
    return (s or "").replace("*", "").replace("_", "").replace("[", "(").replace("]", ")")


def format_alert(deal):
    """Format varies by tier. EMERGENCY gets the most prominence."""
    tier = deal.get("tier", "NORMAL")
    src = deal["source"]

    # Header by tier
    if tier == "EMERGENCY":
        header = "🚨🚨🚨 *PRICE ERROR / EMERGENCY BUY* 🚨🚨🚨"
    elif tier == "STRONG":
        header = "🟢 *STRONG BUY*"
    else:
        header = "🟡 *BUY*"

    # Source prefix
    src_prefix = ""
    if src == "CCC-Watchlist":
        src_prefix = "🎯 WATCHLIST · "
    elif src.startswith("X-"):
        src_prefix = "⚡ X · "

    title = escape_md(deal["title"][:120])
    profit = deal.get("profit", 0) or 0

    lines = [
        f"{header}",
        f"{src_prefix}*+${profit:.0f} profit*",
        "",
        f"*{title}*",
    ]

    # Single-line price summary
    price_bits = []
    if deal.get("deal_price"):
        price_bits.append(f"💵 ${deal['deal_price']:.0f}")
    if deal.get("amazon") and deal["amazon"].get("found"):
        price_bits.append(f"🟧 ${deal['amazon']['price']:.0f}")
    if deal.get("ebay") and deal["ebay"].get("found"):
        e = deal["ebay"]
        price_bits.append(f"🔵 ${e['median_price']:.0f} ({e['sold_count']} sold)")
    if deal.get("mercari") and deal["mercari"].get("found"):
        price_bits.append(f"🟣 ${deal['mercari']['median_price']:.0f}")
    if deal.get("google") and deal["google"].get("found"):
        price_bits.append(f"🔎 ${deal['google']['median_price']:.0f}")

    if price_bits:
        lines.append(" | ".join(price_bits))

    # Stock status
    if deal.get("in_stock") is True:
        lines.append("✅ In stock")

    # Buy link — always prominent
    lines.append(f"\n[👉 BUY HERE]({deal['url']})")

    # Quick reason
    if deal.get("verdict_reason"):
        lines.append(f"_{escape_md(deal['verdict_reason'][:120])}_")

    return "\n".join(lines)


def send_telegram(message, silent=False):
    """silent=True sends without sound — used for NORMAL tier."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
            "disable_notification": silent,
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

    fetch_start = time.time()
    all_deals = sources.fetch_all()
    fetch_time = time.time() - fetch_start

    sc = {}
    for d in all_deals:
        sc[d["source"]] = sc.get(d["source"], 0) + 1
    print(f"Fetched {len(all_deals)} deals in {fetch_time:.1f}s: {sc}")

    new_deals = [d for d in all_deals if deal_id(d) not in seen]
    print(f"  {len(new_deals)} new since last run")

    for d in new_deals:
        d["_score"] = score_deal(d)

    candidates = [d for d in new_deals if d["_score"] >= MIN_SCORE_FOR_ENRICHMENT]
    candidates = dedupe(candidates)
    candidates.sort(key=lambda x: (x["_score"], recency_key(x)), reverse=True)
    candidates = candidates[:MAX_ENRICHMENTS_PER_RUN]

    print(f"  {len(candidates)} candidates above score {MIN_SCORE_FOR_ENRICHMENT}; enriching...")

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
                    enriched.append({**d, "verdict": "WATCH", "tier": None,
                                     "verdict_reason": f"Error: {e}"})
    enrich_time = time.time() - enrich_start

    # Sort: EMERGENCY first, then STRONG, then NORMAL, then by recency within tier
    tier_order = {"EMERGENCY": 0, "STRONG": 1, "NORMAL": 2}
    enriched.sort(key=lambda d: (
        tier_order.get(d.get("tier"), 99),
        -recency_key(d),
    ))

    # Pick what to alert
    alerts = []
    for d in enriched:
        tier = d.get("tier")
        src = d["source"]
        # CCC + X always alert (you set the trigger / time-sensitive)
        if src == "CCC-Watchlist" or src.startswith("X-"):
            if d.get("verdict") != "SKIP":  # respect OOS
                alerts.append(d)
        elif tier in ("EMERGENCY", "STRONG", "NORMAL"):
            alerts.append(d)

    alerts = alerts[:MAX_ALERTS_PER_RUN]

    now_iso = datetime.now(timezone.utc).isoformat()

    for d in alerts:
        tier = d.get("tier", "?")
        print(f"  ALERT [{tier}] [{d.get('verdict')}] [{d['_score']}] {d['title'][:80]}")
        # NORMAL tier sends silently (no sound), others ping
        silent = (tier == "NORMAL")
        send_telegram(format_alert(d), silent=silent)

    for d in all_deals:
        seen[deal_id(d)] = now_iso

    save_seen(seen)
    total = time.time() - run_start
    print(f"Done in {total:.1f}s (fetch {fetch_time:.1f}s, enrich {enrich_time:.1f}s); "
          f"sent {len(alerts)} alerts; tracking {len(seen)} IDs")


if __name__ == "__main__":
    main()

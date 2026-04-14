"""
deal_hunter.py  v3 — multi-source deal scanner with buy/watch/pass recommender
Sources: Slickdeals RSS · Reddit (via ScraperAPI) · Nitter/Twitter RSS ·
         Amazon Warehouse (ScraperAPI) · CamelCamelCamel watchlist · Brickseek clearance
"""

import os, json, re, time, hashlib, logging
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
import feedparser

logging.basicConfig(level=logging.INFO, format="%(lineno)d %(message)s")

# ── secrets ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SCRAPER_API_KEY  = os.environ.get("SCRAPER_API_KEY", "")
SEEN_FILE        = "seen.json"
MIN_SCORE        = 15

# ── keyword config ─────────────────────────────────────────────────────────────
KEYWORDS_BOOST = [
    "lego","pokemon","pokémon","nintendo","switch","ps5","xbox","playstation",
    "apple","ipad","airpods","macbook","iphone","dyson","keurig","instant pot",
    "kitchenaid","dewalt","milwaukee","makita","stanley","ryobi","bosch",
    "yeti","hydro flask","funko","hot wheels","nerf","barbie",
    "magic the gathering","mtg","yugioh","yu-gi-oh","disney","star wars",
    "price error","pricing error","glitch","mistake","clearance",
    "75% off","80% off","85% off","90% off","gift card","visa gift",
    "amazon warehouse","open box","returned",
]
KEYWORDS_SKIP = [
    "porn","adult","casino","gambling","cbd","vape","tobacco",
    "crypto","nft","insurance","mortgage","loan","forex",
]

# ── Nitter accounts (price errors + deal scouts) ───────────────────────────────
NITTER_MIRRORS = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.net",
]
TWITTER_ACCOUNTS = [
    "Pricerrors", "DealsPlus", "lootbot_deals", "GottaDEAL",
    "dealsource_mel", "SlickdealsNet", "1saleaday", "TechDealBlog",
    "DealNews", "MaximumDeals",
]

# ── CamelCamelCamel watchlist ─────────────────────────────────────────────────
# Add tuples: (ASIN, max_price_you_will_pay, label)
CCC_WATCHLIST = [
    # ("B08N5WRWNW", 80,  "LEGO Millennium Falcon"),
    # ("B07XJ8C8F5", 40,  "Switch Pro Controller"),
]

# ── helpers ────────────────────────────────────────────────────────────────────

def scraper_get(url, retries=2, **params):
    """Route through ScraperAPI if key is set, else direct with UA header."""
    for attempt in range(retries + 1):
        try:
            if SCRAPER_API_KEY:
                p = {"api_key": SCRAPER_API_KEY, "url": url}
                p.update(params)
                r = requests.get("https://api.scraperapi.com/", params=p, timeout=45)
            else:
                headers = {"User-Agent": "Mozilla/5.0 (compatible; DealHunterBot/3.0)"}
                r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            return r
        except Exception as ex:
            if attempt == retries:
                raise
            time.sleep(2 ** attempt)

def load_seen():
    try:
        return set(json.loads(open(SEEN_FILE).read()))
    except Exception:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

def deal_id(title, url=""):
    return hashlib.md5((title + url).encode()).hexdigest()[:12]

def score_deal(title, body="", source=""):
    t = (title + " " + body + " " + source).lower()
    if any(k in t for k in KEYWORDS_SKIP):
        return -99
    s = 0
    for kw in KEYWORDS_BOOST:
        if kw in t:
            s += 8
    m = re.search(r'(\d+)\s*%\s*off', t)
    if m:
        p = int(m.group(1))
        s += 25 if p >= 70 else 18 if p >= 55 else 10 if p >= 40 else 4 if p >= 25 else 0
    if any(x in t for x in ["price error", "pricing error", "glitch", "mistake price"]):
        s += 35
    if "amazon warehouse" in t or "open box" in t:
        s += 15
    if "clearance" in t:
        s += 10
    return s


# ── sources ────────────────────────────────────────────────────────────────────

def fetch_slickdeals():
    deals = []
    feeds = [
        "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
        "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1",
    ]
    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for e in feed.entries[:30]:
                deals.append({
                    "id":     deal_id(e.get("title",""), e.get("link","")),
                    "title":  e.get("title","").strip(),
                    "url":    e.get("link",""),
                    "source": "Slickdeals",
                    "score":  score_deal(e.get("title",""), e.get("summary","")),
                    "price_hint": "",
                })
        except Exception as ex:
            logging.warning(f"Slickdeals failed: {ex}")
    return deals


def fetch_reddit(subreddit):
    """Fetch subreddit new posts via ScraperAPI to avoid 403s."""
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=25"
    try:
        r = scraper_get(url)
        posts = r.json()["data"]["children"]
        out = []
        for p in posts:
            d = p["data"]
            upvote_bonus = min(d.get("score", 0) // 20, 15)
            out.append({
                "id":     deal_id(d["title"], d.get("url","")),
                "title":  d["title"].strip(),
                "url":    f"https://reddit.com{d['permalink']}",
                "source": f"r/{subreddit}",
                "score":  score_deal(d["title"], d.get("selftext",""), subreddit) + upvote_bonus,
                "price_hint": "",
            })
        return out
    except Exception as ex:
        logging.warning(f"Reddit r/{subreddit} failed: {ex}")
        return []


def fetch_nitter():
    """Pull recent deal tweets via Nitter RSS (free X mirrors)."""
    deals = []
    for account in TWITTER_ACCOUNTS:
        feed_url = None
        for mirror in NITTER_MIRRORS:
            try:
                test = f"{mirror}/{account}/rss"
                r = requests.get(test, timeout=8, headers={"User-Agent": "feedparser/6.0"})
                if r.status_code == 200 and "<rss" in r.text[:500]:
                    feed_url = test
                    break
            except Exception:
                continue
        if not feed_url:
            logging.warning(f"Nitter: no working mirror for @{account}")
            continue
        try:
            feed = feedparser.parse(feed_url)
            for e in feed.entries[:12]:
                title = e.get("title","").strip()
                link  = e.get("link","")
                s = score_deal(title, source=f"@{account}")
                if s < 5:
                    continue
                deals.append({
                    "id":     deal_id(title, link),
                    "title":  f"{title[:130]}",
                    "url":    link,
                    "source": f"Twitter/@{account}",
                    "score":  s + 8,  # bonus: real-time, often before Slickdeals
                    "price_hint": "",
                })
        except Exception as ex:
            logging.warning(f"Nitter @{account} failed: {ex}")
    return deals


def fetch_amazon_warehouse():
    """Scrape Amazon Warehouse deals categories via ScraperAPI."""
    if not SCRAPER_API_KEY:
        logging.info("Skipping Amazon Warehouse (no SCRAPER_API_KEY)")
        return []
    deals = []
    categories = [
        ("Electronics",  "https://www.amazon.com/s?i=warehouse-deals&rh=n%3A172282&s=date-desc-rank"),
        ("Toys",         "https://www.amazon.com/s?i=warehouse-deals&rh=n%3A165793011&s=date-desc-rank"),
        ("Tools",        "https://www.amazon.com/s?i=warehouse-deals&rh=n%3A228013&s=date-desc-rank"),
        ("Video Games",  "https://www.amazon.com/s?i=warehouse-deals&rh=n%3A468642&s=date-desc-rank"),
    ]
    for cat_name, url in categories:
        try:
            r = scraper_get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            for item in soup.select("[data-component-type='s-search-result']")[:8]:
                title_el = item.select_one("h2 a span")
                price_el = item.select_one(".a-price .a-offscreen")
                link_el  = item.select_one("h2 a")
                cond_el  = item.select_one(".a-color-secondary")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                price = price_el.get_text(strip=True) if price_el else ""
                link  = "https://amazon.com" + link_el["href"] if link_el and link_el.get("href") else url
                cond  = cond_el.get_text(strip=True)[:60] if cond_el else ""
                full  = f"{title} amazon warehouse {cat_name} {cond}"
                deals.append({
                    "id":         deal_id(title, link),
                    "title":      f"[WH-{cat_name}] {title[:90]} — {price}",
                    "url":        link,
                    "source":     "Amazon Warehouse",
                    "score":      score_deal(full) + 15,
                    "price_hint": price,
                })
        except Exception as ex:
            logging.warning(f"Amazon Warehouse ({cat_name}) failed: {ex}")
    return deals


def fetch_ccc_watchlist():
    """Check CamelCamelCamel for watchlist ASIN price targets."""
    deals = []
    for asin, max_price, label in CCC_WATCHLIST:
        try:
            r = requests.get(
                f"https://camelcamelcamel.com/product/{asin}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            soup = BeautifulSoup(r.text, "html.parser")
            price_el = soup.select_one(".amazon .price")
            if not price_el:
                continue
            raw = re.sub(r"[^\d.]", "", price_el.get_text())
            price_num = float(raw) if raw else 9999
            if price_num <= max_price:
                deals.append({
                    "id":         deal_id(label, asin),
                    "title":      f"⚡ WATCHLIST HIT: {label} — now ${price_num:.2f} (target ≤${max_price})",
                    "url":        f"https://www.amazon.com/dp/{asin}",
                    "source":     "CamelCamelCamel",
                    "score":      90,
                    "price_hint": f"${price_num:.2f}",
                })
        except Exception as ex:
            logging.warning(f"CCC {asin} ({label}) failed: {ex}")
    return deals


def fetch_brickseek():
    """Scan Brickseek for Target/Walmart clearance finds."""
    if not SCRAPER_API_KEY:
        logging.info("Skipping Brickseek (no SCRAPER_API_KEY)")
        return []
    deals = []
    targets = [
        ("Target",  "https://brickseek.com/target-inventory-checker/?sku=&type=clearance"),
        ("Walmart", "https://brickseek.com/walmart-inventory-checker/?sku=&type=clearance"),
    ]
    for store, url in targets:
        try:
            r = scraper_get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            selectors = [".item-results__item", ".product-card", ".clearance-item"]
            rows = []
            for sel in selectors:
                rows = soup.select(sel)
                if rows:
                    break
            for row in rows[:12]:
                title_el = row.select_one(
                    ".item-results__name, .product-title, .product-name, h3, h2"
                )
                price_el = row.select_one(".price, .item-results__price, .sale-price")
                pct_el   = row.select_one(".item-results__discount, .discount, .savings")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                price = price_el.get_text(strip=True) if price_el else ""
                pct   = pct_el.get_text(strip=True) if pct_el else ""
                s = score_deal(f"{title} clearance {pct}")
                if s < 10:
                    continue
                deals.append({
                    "id":         deal_id(title + store),
                    "title":      f"[{store} Clearance] {title[:90]} {price} {pct}".strip(),
                    "url":        url,
                    "source":     f"Brickseek/{store}",
                    "score":      s + 10,
                    "price_hint": price,
                })
        except Exception as ex:
            logging.warning(f"Brickseek {store} failed: {ex}")
    return deals


# ── buy/watch/pass recommender ─────────────────────────────────────────────────

def recommend(deal):
    """
    Returns (verdict_str, reason_str).
    Verdict: BUY 🟢 | WATCH 🟡 | PASS 🔴

    Rules (in priority order):
    1. CCC watchlist hit → always BUY (you set the price, trust yourself)
    2. "Price error / glitch" signal → BUY (act immediately, expires fast)
    3. Amazon Warehouse + relevant category + score ≥ 30 → BUY
    4. ≥ 60% off + score ≥ 30 → BUY (run Keepa before purchasing)
    5. ≥ 40% off OR score ≥ 35 → WATCH
    6. score ≥ 20 → WATCH (soft signal, manual glance)
    7. else → PASS
    """
    title  = deal["title"].lower()
    score  = deal["score"]
    source = deal["source"]

    if source == "CamelCamelCamel":
        return "BUY 🟢", "Your watchlist target was hit — check condition & rank"

    if any(x in title for x in ["price error","pricing error","glitch","mistake price","pricing mistake"]):
        return "BUY 🟢", "Price error detected — act now, these expire in minutes"

    if source == "Amazon Warehouse" and score >= 30:
        return "BUY 🟢", "Warehouse deal on tracked category — verify condition grade"

    pct_m = re.search(r'(\d+)\s*%\s*off', title)
    pct   = int(pct_m.group(1)) if pct_m else 0

    if pct >= 60 and score >= 30:
        return "BUY 🟢", f"{pct}% off + keyword match — confirm with Keepa before buying"
    if pct >= 40 and score >= 20:
        return "WATCH 🟡", f"{pct}% off — check Amazon price history and sales rank"
    if score >= 35:
        return "WATCH 🟡", "Strong keyword match — worth a manual look"
    if score >= 20:
        return "WATCH 🟡", "Soft signal — quick glance recommended"

    return "PASS 🔴", "Low relevance score"


# ── telegram ───────────────────────────────────────────────────────────────────

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        try:
            requests.post(url, json={
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     chunk,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            }, timeout=15)
        except Exception as ex:
            logging.error(f"Telegram send failed: {ex}")


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    seen = load_seen()
    logging.info(f"Loaded {len(seen)} previously-seen deals")

    all_deals = []
    all_deals += fetch_slickdeals()
    for sub in ["deals","Flipping","GameDeals","buildapcsales","MUAontheCheap","flipping"]:
        all_deals += fetch_reddit(sub)
    all_deals += fetch_nitter()
    all_deals += fetch_amazon_warehouse()
    all_deals += fetch_ccc_watchlist()
    all_deals += fetch_brickseek()

    source_counts = {}
    for d in all_deals:
        source_counts[d["source"]] = source_counts.get(d["source"], 0) + 1
    logging.info(f"Fetched {len(all_deals)} deals from: {source_counts}")

    new_deals = [d for d in all_deals if d["id"] not in seen]
    logging.info(f"  {len(new_deals)} new since last run")

    candidates = sorted(
        [d for d in new_deals if d["score"] >= MIN_SCORE],
        key=lambda x: x["score"],
        reverse=True,
    )[:12]
    logging.info(f"  {len(candidates)} candidates above score {MIN_SCORE}")

    new_seen = {d["id"] for d in new_deals}

    if not candidates:
        logging.info("No candidates — skipping Telegram message")
        save_seen(seen | new_seen)
        return

    ts = datetime.now(timezone.utc).strftime("%-I:%M %p UTC · %b %-d")
    lines = [f"<b>🔎 Deal Alert — {ts}</b>\n"]

    for d in candidates:
        verdict, reason = recommend(d)
        lines.append(
            f"{verdict}  <b>{d['title'][:105]}</b>\n"
            f"   📌 {d['source']}  ·  score {d['score']}\n"
            f"   💡 {reason}\n"
            f"   🔗 {d['url']}\n"
        )

    send_telegram("\n".join(lines))
    logging.info(f"Sent {len(candidates)} alerts; tracking {len(seen | new_seen)} seen IDs")
    save_seen(seen | new_seen)


if __name__ == "__main__":
    main()

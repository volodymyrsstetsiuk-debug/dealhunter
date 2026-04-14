"""
deal_hunter.py  v4
Fixes: seen.json write perms (workflow), Nitter replaced with reliable RSS,
       Amazon + eBay sold price comparison restored, one Telegram message per deal.
"""

import os, json, re, time, hashlib, logging
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
import feedparser

logging.basicConfig(level=logging.INFO, format="%(lineno)d %(message)s")

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SCRAPER_API_KEY  = os.environ.get("SCRAPER_API_KEY", "")
SEEN_FILE        = "seen.json"
MIN_SCORE        = 15

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

# CamelCamelCamel watchlist: (ASIN, max_price, label)
CCC_WATCHLIST = [
    # ("B08N5WRWNW", 80, "LEGO Millennium Falcon"),
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})


# ── helpers ────────────────────────────────────────────────────────────────────

def scraper_get(url, retries=2, **params):
    for attempt in range(retries + 1):
        try:
            if SCRAPER_API_KEY:
                p = {"api_key": SCRAPER_API_KEY, "url": url}
                p.update(params)
                r = SESSION.get("https://api.scraperapi.com/", params=p, timeout=45)
            else:
                r = SESSION.get(url, timeout=20)
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
    if any(x in t for x in ["price error","pricing error","glitch","mistake price"]):
        s += 35
    if "amazon warehouse" in t or "open box" in t:
        s += 15
    if "clearance" in t:
        s += 10
    return s


# ── sources ────────────────────────────────────────────────────────────────────

def fetch_rss(feed_url, source_name):
    deals = []
    try:
        feed = feedparser.parse(feed_url)
        for e in feed.entries[:30]:
            title = e.get("title","").strip()
            url   = e.get("link","")
            deals.append({
                "id":     deal_id(title, url),
                "title":  title,
                "url":    url,
                "source": source_name,
                "score":  score_deal(title, e.get("summary","")),
            })
    except Exception as ex:
        logging.warning(f"{source_name} RSS failed: {ex}")
    return deals

def fetch_all_rss():
    feeds = [
        ("https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1", "Slickdeals-FP"),
        ("https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1",  "Slickdeals-Pop"),
        ("https://9to5toys.com/feed/",           "9to5Toys"),
        ("https://9to5mac.com/deals/feed/",      "9to5Mac-Deals"),
        ("https://www.bensbargains.com/feed/",   "BensBargains"),
        ("https://dealnews.com/rss.html",        "DealNews"),
        ("https://www.gottadeal.com/feed",       "GottaDEAL"),
        ("https://www.dealsplus.com/feed",       "DealsPlus"),
        ("https://hip2save.com/feed/",           "Hip2Save"),
        ("https://www.techbargains.com/feed/rss2/", "TechBargains"),
    ]
    all_deals = []
    for url, name in feeds:
        all_deals += fetch_rss(url, name)
    return all_deals

def fetch_reddit(subreddit):
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
            })
        return out
    except Exception as ex:
        logging.warning(f"Reddit r/{subreddit} failed: {ex}")
        return []

def fetch_amazon_warehouse():
    if not SCRAPER_API_KEY:
        return []
    deals = []
    categories = [
        ("Electronics", "https://www.amazon.com/s?i=warehouse-deals&rh=n%3A172282&s=date-desc-rank"),
        ("Toys",        "https://www.amazon.com/s?i=warehouse-deals&rh=n%3A165793011&s=date-desc-rank"),
        ("Tools",       "https://www.amazon.com/s?i=warehouse-deals&rh=n%3A228013&s=date-desc-rank"),
        ("Games",       "https://www.amazon.com/s?i=warehouse-deals&rh=n%3A468642&s=date-desc-rank"),
    ]
    for cat_name, url in categories:
        try:
            r = scraper_get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            for item in soup.select("[data-component-type='s-search-result']")[:8]:
                title_el = item.select_one("h2 a span")
                price_el = item.select_one(".a-price .a-offscreen")
                link_el  = item.select_one("h2 a")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                price = price_el.get_text(strip=True) if price_el else ""
                link  = "https://amazon.com" + link_el["href"] if link_el and link_el.get("href") else url
                deals.append({
                    "id":     deal_id(title, link),
                    "title":  f"[WH-{cat_name}] {title[:90]} — {price}",
                    "url":    link,
                    "source": "Amazon Warehouse",
                    "score":  score_deal(f"{title} amazon warehouse {cat_name}") + 15,
                })
        except Exception as ex:
            logging.warning(f"Amazon Warehouse ({cat_name}) failed: {ex}")
    return deals

def fetch_ccc_watchlist():
    deals = []
    for asin, max_price, label in CCC_WATCHLIST:
        try:
            r = SESSION.get(f"https://camelcamelcamel.com/product/{asin}", timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            price_el = soup.select_one(".amazon .price")
            if not price_el:
                continue
            raw = re.sub(r"[^\d.]", "", price_el.get_text())
            price_num = float(raw) if raw else 9999
            if price_num <= max_price:
                deals.append({
                    "id":     deal_id(label, asin),
                    "title":  f"WATCHLIST HIT: {label} — now ${price_num:.2f} (target ≤${max_price})",
                    "url":    f"https://www.amazon.com/dp/{asin}",
                    "source": "CamelCamelCamel",
                    "score":  90,
                })
        except Exception as ex:
            logging.warning(f"CCC {asin} failed: {ex}")
    return deals


# ── price enrichment ───────────────────────────────────────────────────────────

def get_amazon_price(query):
    """Returns (price_float, product_url) or (None, None)."""
    if not SCRAPER_API_KEY:
        return None, None
    try:
        url = f"https://www.amazon.com/s?k={requests.utils.quote(query[:60])}"
        r = scraper_get(url)
        soup = BeautifulSoup(r.text, "html.parser")
        result = soup.select_one("[data-component-type='s-search-result']")
        if not result:
            return None, None
        price_el = result.select_one(".a-price .a-offscreen")
        link_el  = result.select_one("h2 a")
        if not price_el:
            return None, None
        price = float(re.sub(r"[^\d.]", "", price_el.get_text()) or "0")
        link  = "https://amazon.com" + link_el["href"] if link_el and link_el.get("href") else ""
        return price, link
    except Exception:
        return None, None

def get_ebay_sold_avg(query):
    """Returns average sold price from eBay completed listings, or None."""
    try:
        search_url = (
            f"https://www.ebay.com/sch/i.html?_nkw={requests.utils.quote(query[:60])}"
            "&LH_Complete=1&LH_Sold=1&_sop=13"
        )
        r = scraper_get(search_url) if SCRAPER_API_KEY else SESSION.get(search_url, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        prices = []
        for el in soup.select(".s-item__price")[:12]:
            raw = re.sub(r"[^\d.]", "", el.get_text().split("to")[0])
            try:
                prices.append(float(raw))
            except Exception:
                pass
        if not prices:
            return None
        prices.sort()
        trim = max(1, len(prices) // 7)
        trimmed = prices[trim:-trim] if len(prices) > 4 else prices
        return round(sum(trimmed) / len(trimmed), 2)
    except Exception:
        return None

def enrich(deal):
    query = re.sub(r'^\[.*?\]\s*', '', deal["title"])
    query = re.sub(r'\s*[-—]\s*\$[\d.,]+.*$', '', query).strip()[:70]
    amazon_price, amazon_url = get_amazon_price(query)
    ebay_sold = get_ebay_sold_avg(query)
    deal["amazon_price"] = amazon_price
    deal["amazon_url"]   = amazon_url or ""
    deal["ebay_sold"]    = ebay_sold
    return deal


# ── recommender ────────────────────────────────────────────────────────────────

def recommend(deal):
    """Returns (verdict, reason, roi_lines)."""
    title  = deal["title"].lower()
    score  = deal["score"]
    source = deal["source"]
    ap     = deal.get("amazon_price")
    es     = deal.get("ebay_sold")

    price_m    = re.search(r'\$([\d,.]+)', deal["title"])
    deal_price = float(price_m.group(1).replace(",","")) if price_m else None

    roi_lines = []
    if deal_price and ap:
        margin  = ap - deal_price
        roi_pct = (margin / deal_price * 100) if deal_price else 0
        roi_lines.append(f"Deal ${deal_price:.0f} → Amazon ${ap:.0f}  (+${margin:.0f} / {roi_pct:.0f}% ROI)")
    if es:
        if deal_price:
            roi_lines.append(f"eBay sold avg ${es:.0f}  (+${es - deal_price:.0f} vs deal price)")
        else:
            roi_lines.append(f"eBay sold avg ${es:.0f}")

    if source == "CamelCamelCamel":
        return "BUY 🟢", "Watchlist target hit", roi_lines

    if any(x in title for x in ["price error","pricing error","glitch","mistake price"]):
        return "BUY 🟢", "Price error — act NOW, expires in minutes", roi_lines

    if deal_price and ap and ap / deal_price >= 2.5:
        return "BUY 🟢", f"3× rule: Amazon is {ap/deal_price:.1f}× the deal price", roi_lines

    if deal_price and es and es / deal_price >= 2.0:
        return "BUY 🟢", f"eBay sold avg is {es/deal_price:.1f}× the deal price", roi_lines

    if source == "Amazon Warehouse" and score >= 30:
        return "BUY 🟢", "Warehouse deal — check condition grade", roi_lines

    pct_m = re.search(r'(\d+)\s*%\s*off', title)
    pct   = int(pct_m.group(1)) if pct_m else 0

    if pct >= 60 and score >= 30:
        return "BUY 🟢", f"{pct}% off + keyword match — verify with Keepa", roi_lines
    if pct >= 40 and score >= 20:
        return "WATCH 🟡", f"{pct}% off — check price history", roi_lines
    if score >= 35:
        return "WATCH 🟡", "Strong keyword match — manual check", roi_lines
    if score >= 20:
        return "WATCH 🟡", "Soft signal — worth a glance", roi_lines

    return "PASS 🔴", "Low relevance", roi_lines


# ── telegram ───────────────────────────────────────────────────────────────────

def send_message(text):
    SESSION.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=15,
    )
    time.sleep(0.4)

def send_summary_header(count, source_summary):
    ts = datetime.now(timezone.utc).strftime("%-I:%M %p UTC · %b %-d")
    send_message(
        f"🔎 <b>Deal Hunt — {ts}</b>\n"
        f"{count} candidates found\n"
        f"<i>{source_summary}</i>"
    )

def send_deal_card(deal, verdict, reason, roi_lines, index, total):
    lines = [
        f"{verdict}  <b>[{index}/{total}] {deal['title'][:110]}</b>",
        f"",
        f"📌 {deal['source']}  ·  score {deal['score']}",
        f"💡 {reason}",
    ]
    for rl in roi_lines:
        lines.append(f"💰 {rl}")
    if deal.get("amazon_url"):
        lines.append(f"🛒 <a href=\"{deal['amazon_url']}\">Amazon</a>")
    lines.append(f"🔗 <a href=\"{deal['url']}\">Source</a>")
    send_message("\n".join(lines))


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    seen = load_seen()
    logging.info(f"Loaded {len(seen)} previously-seen deals")

    all_deals = []
    all_deals += fetch_all_rss()
    for sub in ["deals","Flipping","GameDeals","buildapcsales","MUAontheCheap","flipping"]:
        all_deals += fetch_reddit(sub)
    all_deals += fetch_amazon_warehouse()
    all_deals += fetch_ccc_watchlist()

    source_counts = {}
    for d in all_deals:
        source_counts[d["source"]] = source_counts.get(d["source"], 0) + 1
    logging.info(f"Fetched {len(all_deals)} from: {source_counts}")

    new_deals = [d for d in all_deals if d["id"] not in seen]
    logging.info(f"  {len(new_deals)} new since last run")

    candidates = sorted(
        [d for d in new_deals if d["score"] >= MIN_SCORE],
        key=lambda x: x["score"], reverse=True,
    )[:10]
    logging.info(f"  {len(candidates)} candidates above score {MIN_SCORE}; enriching...")

    new_seen = {d["id"] for d in new_deals}

    if not candidates:
        logging.info("No candidates")
        save_seen(seen | new_seen)
        return

    for d in candidates:
        enrich(d)

    rated = [(d, *recommend(d)) for d in candidates]
    buys  = [(d,v,r,roi) for d,v,r,roi in rated if "BUY"   in v]
    watch = [(d,v,r,roi) for d,v,r,roi in rated if "WATCH" in v]
    passes= [(d,v,r,roi) for d,v,r,roi in rated if "PASS"  in v]

    final = buys + watch
    if len(final) < 5:
        final += passes[:5 - len(final)]

    top_sources = ", ".join(list(source_counts.keys())[:5])
    send_summary_header(len(final), top_sources)

    for i, (deal, verdict, reason, roi_lines) in enumerate(final, 1):
        send_deal_card(deal, verdict, reason, roi_lines, i, len(final))

    logging.info(f"Sent {len(final)} alerts; tracking {len(seen | new_seen)} seen IDs")
    save_seen(seen | new_seen)


if __name__ == "__main__":
    main()

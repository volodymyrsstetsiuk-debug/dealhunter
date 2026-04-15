"""
Enrichment v5 — LLM-powered product extraction + stock check + 4 marketplaces parallel.
"""
import os
import re
import json
import requests
from urllib.parse import quote_plus, urlparse
from concurrent.futures import ThreadPoolExecutor

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
SCRAPER_API_BASE = "https://api.scraperapi.com/"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

FBA_FEE_RATE = 0.25
FIXED_FBA_FEE = 3.50
EBAY_FEE_RATE = 0.13


def _scraper_get(target_url, timeout=25, render_js=False):
    """Route a request through ScraperAPI."""
    if not SCRAPER_API_KEY:
        print("  WARNING: SCRAPER_API_KEY not set")
        return None
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "country_code": "us",
    }
    if render_js:
        params["render"] = "true"
    try:
        r = requests.get(SCRAPER_API_BASE, params=params, timeout=timeout)
        if r.status_code != 200:
            print(f"  ScraperAPI {r.status_code} for {target_url[:80]}")
            return None
        return r.text
    except Exception as e:
        print(f"  ScraperAPI error: {e}")
        return None


def extract_price(text):
    if not text:
        return None
    m = re.search(r"\$\s?(\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


# =============================================================
# LLM PRODUCT EXTRACTION (Claude Haiku)
# =============================================================

def llm_extract_product(title, description=""):
    """
    Use Claude Haiku to turn a messy deal title into a precise marketplace search query.
    Returns dict: {"query": "...", "price": float|None, "category": "..."}
    Falls back to regex if Anthropic API not configured.
    """
    if not ANTHROPIC_API_KEY:
        return _fallback_extract(title)

    text = (title + "\n" + (description or ""))[:600]
    prompt = (
        "Extract product info from this deal text. Output STRICT JSON only.\n"
        "Format: {\"query\": \"brand model key-spec\", \"price\": NUMBER_OR_NULL, "
        "\"category\": \"electronics|toys|tools|beauty|home|fashion|other\", "
        "\"flippable\": true_or_false}\n\n"
        "Rules:\n"
        "- query: 3-7 words, brand + model + key spec (color/size/capacity)\n"
        "- price: deal price as a number, or null if unclear\n"
        "- flippable: false for: software, subscriptions, ebooks, food, "
        "small consumables under $10, services\n"
        "- flippable: true for: branded physical goods with resale potential\n\n"
        f"DEAL TEXT:\n{text}\n\n"
        "JSON:"
    )

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        if not r.ok:
            print(f"  LLM error {r.status_code}: {r.text[:150]}")
            return _fallback_extract(title)
        data = r.json()
        content = data["content"][0]["text"].strip()
        # Strip code fences if present
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
        parsed = json.loads(content)
        return {
            "query": parsed.get("query", "").strip()[:80],
            "price": parsed.get("price"),
            "category": parsed.get("category", "other"),
            "flippable": parsed.get("flippable", True),
        }
    except Exception as e:
        print(f"  LLM extract failed: {e}")
        return _fallback_extract(title)


def _fallback_extract(title):
    """Regex fallback if LLM unavailable."""
    if not title:
        return {"query": None, "price": None, "category": "other", "flippable": True}
    t = re.sub(r"\$\s?\d[\d,.]*", " ", title)
    t = re.sub(r"\d{1,3}\s?%\s?off", " ", t, flags=re.I)
    t = re.sub(r"\b(deal|sale|clearance|free shipping|w/|with|coupon|promo|code|"
               r"only|today|new|hot|limited|extra|save)\b", " ", t, flags=re.I)
    t = re.sub(r"[^\w\s\-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return {
        "query": " ".join(t.split()[:6]) or None,
        "price": extract_price(title),
        "category": "other",
        "flippable": True,
    }


# =============================================================
# STOCK CHECKER
# =============================================================

def check_stock(deal_url):
    """
    Quick check: is this URL still showing in-stock?
    Returns: True (in stock), False (out of stock), None (couldn't tell).
    """
    if not deal_url:
        return None

    domain = urlparse(deal_url).netloc.lower()

    # For Slickdeals/aggregators, we can't easily tell — return None (don't block)
    if "slickdeals" in domain or "dealnews" in domain or "rss.app" in domain:
        return None

    # For known retailers, fetch the page and look for OOS markers
    html_text = _scraper_get(deal_url, timeout=20)
    if not html_text:
        return None

    text = html_text.lower()
    # Strong OOS signals
    oos_markers = [
        "currently unavailable", "out of stock", "sold out",
        "no longer available", "this item is unavailable",
        "temporarily out of stock", "product not available",
    ]
    for marker in oos_markers:
        if marker in text:
            return False

    # In-stock signals (presence of buy buttons usually means available)
    in_stock_markers = [
        "add to cart", "add-to-cart", "buy now", "addtocart",
        "in stock", "ships in", "available to ship",
    ]
    for marker in in_stock_markers:
        if marker in text:
            return True

    return None  # ambiguous


# =============================================================
# MARKETPLACE LOOKUPS (Amazon, eBay, Mercari, Google Shopping)
# =============================================================

def amazon_lookup(query):
    if not query:
        return None
    target = f"https://www.amazon.com/s?k={quote_plus(query)}&ref=nb_sb_noss"
    html_text = _scraper_get(target)
    if not html_text:
        return None

    whole = re.search(r'<span class="a-price-whole">([\d,]+)', html_text)
    frac = re.search(r'<span class="a-price-fraction">(\d{2})', html_text)
    if not whole:
        return {"found": False, "query": query}

    try:
        price = float(whole.group(1).replace(",", ""))
        if frac:
            price += float(frac.group(1)) / 100
    except ValueError:
        return {"found": False, "query": query}

    asin_match = re.search(r'data-asin="([A-Z0-9]{10})"', html_text)
    asin = asin_match.group(1) if asin_match else None

    return {
        "found": True,
        "price": price,
        "asin": asin,
        "url": f"https://www.amazon.com/dp/{asin}" if asin else None,
        "query": query,
    }


def ebay_sold_lookup(query):
    if not query:
        return None
    target = (f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(query)}"
              f"&LH_Sold=1&LH_Complete=1&_ipg=60")
    html_text = _scraper_get(target)
    if not html_text:
        return None

    prices = []
    for m in re.finditer(r'<span class="s-item__price">[^<]*?\$([\d,]+\.\d{2})',
                         html_text):
        try:
            prices.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
    if len(prices) > 5:
        prices = prices[1:]

    if not prices:
        return {"found": False, "query": query}

    prices.sort()
    return {
        "found": True,
        "median_price": prices[len(prices) // 2],
        "sold_count": len(prices),
        "min_price": min(prices),
        "max_price": max(prices),
        "url": target,
        "query": query,
    }


def mercari_lookup(query):
    if not query:
        return None
    target = (f"https://www.mercari.com/search/?keyword={quote_plus(query)}"
              f"&status=sold_out")
    html_text = _scraper_get(target)
    if not html_text:
        return None

    prices = []
    for m in re.finditer(r'"price"\s*:\s*"?(\d{2,5})(?:\.\d{2})?"?', html_text):
        try:
            p = float(m.group(1))
            if 5 <= p <= 10000:
                prices.append(p)
        except ValueError:
            continue

    if len(prices) < 3:
        return {"found": False, "query": query}

    prices.sort()
    return {
        "found": True,
        "median_price": prices[len(prices) // 2],
        "sold_count": len(prices),
        "min_price": min(prices),
        "max_price": max(prices),
        "url": target,
        "query": query,
    }


def google_shopping_lookup(query):
    if not query:
        return None
    target = f"https://www.google.com/search?tbm=shop&q={quote_plus(query)}"
    html_text = _scraper_get(target)
    if not html_text:
        return None

    prices = []
    for m in re.finditer(r'\$(\d{1,4}(?:,\d{3})*\.\d{2})', html_text):
        try:
            p = float(m.group(1).replace(",", ""))
            if 5 <= p <= 10000:
                prices.append(p)
        except ValueError:
            continue

    if len(prices) < 3:
        return {"found": False, "query": query}

    prices.sort()
    return {
        "found": True,
        "median_price": prices[len(prices) // 2],
        "min_price": min(prices),
        "max_price": max(prices),
        "listing_count": len(prices),
        "url": target,
        "query": query,
    }


# =============================================================
# VERDICT ENGINE
# =============================================================

def compute_verdict(deal_price, amazon, ebay, mercari, google, in_stock):
    """Returns (verdict, profit_estimate, tier, reasoning).
    Tier: 'EMERGENCY' | 'STRONG' | 'NORMAL' | None
    """
    # Hard skip: known OOS
    if in_stock is False:
        return "SKIP", None, None, "Out of stock"

    if deal_price is None:
        return "WATCH", None, None, "Couldn't parse deal price"

    market_prices = []
    if amazon and amazon.get("found"):
        market_prices.append(("Amazon", amazon["price"]))
    if ebay and ebay.get("found"):
        market_prices.append(("eBay", ebay["median_price"]))
    if mercari and mercari.get("found"):
        market_prices.append(("Mercari", mercari["median_price"]))
    if google and google.get("found"):
        market_prices.append(("Google", google["median_price"]))

    if not market_prices:
        return "WATCH", None, None, "No marketplace data"

    # Catch wrong-product matches: if Amazon is way higher than other markets
    amazon_suspect = False
    if amazon and amazon.get("found") and len(market_prices) >= 2:
        other = [p for name, p in market_prices if name != "Amazon"]
        if other:
            other_median = sorted(other)[len(other) // 2]
            if amazon["price"] > other_median * 2.5:
                amazon_suspect = True

    # Best profit path
    best = None  # (profit, roi, channel, sell_price)

    if amazon and amazon.get("found") and not amazon_suspect:
        net = amazon["price"] * (1 - FBA_FEE_RATE) - FIXED_FBA_FEE
        profit = net - deal_price
        roi = (profit / deal_price * 100) if deal_price > 0 else 0
        best = (profit, roi, "Amazon FBA", amazon["price"])

    if ebay and ebay.get("found") and ebay.get("sold_count", 0) >= 3:
        net = ebay["median_price"] * (1 - EBAY_FEE_RATE) - 5
        profit = net - deal_price
        roi = (profit / deal_price * 100) if deal_price > 0 else 0
        if best is None or profit > best[0]:
            best = (profit, roi, "eBay", ebay["median_price"])

    if mercari and mercari.get("found") and mercari.get("sold_count", 0) >= 3:
        net = mercari["median_price"] * 0.90 - 3
        profit = net - deal_price
        roi = (profit / deal_price * 100) if deal_price > 0 else 0
        if best is None or profit > best[0]:
            best = (profit, roi, "Mercari", mercari["median_price"])

    if not best:
        return "WATCH", None, None, "Insufficient market data"

    profit, roi, channel, sell = best
    demand_sources = sum(1 for m in [ebay, mercari]
                        if m and m.get("found") and m.get("sold_count", 0) >= 3)
    has_demand = demand_sources >= 1
    passes_3x = sell >= deal_price * 3

    reason = f"{channel} ${sell:.0f} → ${profit:.0f} ({roi:.0f}% ROI)"

    # Tier assignment
    if profit >= 50 and roi >= 75 and has_demand:
        return "BUY", profit, "EMERGENCY", reason
    if profit >= 20 and roi >= 50 and has_demand:
        return "BUY", profit, "STRONG", reason
    if profit >= 8 and roi >= 30 and (has_demand or passes_3x):
        return "BUY", profit, "NORMAL", reason
    if profit > 0:
        return "WATCH", profit, None, f"Thin margin: {reason}"

    return "SKIP", profit, None, f"Loses money: {reason}"


# =============================================================
# TOP-LEVEL ENRICHMENT
# =============================================================

def enrich_deal(deal):
    title = deal["title"]
    desc = deal.get("description", "")

    # 1. LLM extracts clean product info
    extracted = llm_extract_product(title, desc)
    query = extracted["query"]
    deal_price = extracted["price"] or extract_price(title) or extract_price(desc)
    flippable = extracted.get("flippable", True)

    if not query:
        return {**deal, "verdict": "SKIP", "tier": None,
                "verdict_reason": "Couldn't extract product"}

    # 2. Skip non-flippable categories early (saves ScraperAPI calls)
    if not flippable:
        return {**deal, "verdict": "SKIP", "tier": None,
                "deal_price": deal_price, "category": extracted.get("category"),
                "verdict_reason": f"Not flippable ({extracted.get('category', 'unknown')})"}

    print(f"  [{extracted.get('category', '?')}] '{query}' (deal: ${deal_price})")

    # 3. Run stock check + 4 marketplace lookups in parallel
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_stock = ex.submit(check_stock, deal["url"])
        f_amazon = ex.submit(amazon_lookup, query)
        f_ebay = ex.submit(ebay_sold_lookup, query)
        f_mercari = ex.submit(mercari_lookup, query)
        f_google = ex.submit(google_shopping_lookup, query)

        in_stock = f_stock.result()
        amazon = f_amazon.result()
        ebay = f_ebay.result()
        mercari = f_mercari.result()
        google = f_google.result()

    verdict, profit, tier, reason = compute_verdict(
        deal_price, amazon, ebay, mercari, google, in_stock
    )

    return {
        **deal,
        "deal_price": deal_price,
        "query": query,
        "category": extracted.get("category"),
        "in_stock": in_stock,
        "amazon": amazon,
        "ebay": ebay,
        "mercari": mercari,
        "google": google,
        "verdict": verdict,
        "profit": profit,
        "tier": tier,
        "verdict_reason": reason,
    }

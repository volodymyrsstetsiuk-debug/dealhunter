"""
Enrichment v4 — parallel marketplace lookups.
Adds Mercari + Google Shopping. Runs all 4 marketplaces concurrently.
"""
import os
import re
import requests
from urllib.parse import quote_plus
from concurrent.futures import ThreadPoolExecutor

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
SCRAPER_API_BASE = "https://api.scraperapi.com/"

FBA_FEE_RATE = 0.25
FIXED_FBA_FEE = 3.50
EBAY_FEE_RATE = 0.13

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")


# =============================================================
# Helpers
# =============================================================

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


def extract_search_term(title, max_words=6):
    if not title:
        return None
    t = re.sub(r"\$\s?\d[\d,.]*", " ", title)
    t = re.sub(r"\d{1,3}\s?%\s?off", " ", t, flags=re.I)
    t = re.sub(r"\b(deal|sale|clearance|free shipping|w/|with|coupon|promo|code|"
               r"only|today|new|hot|limited|extra|save)\b",
               " ", t, flags=re.I)
    t = re.sub(r"[^\w\s\-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    words = t.split()[:max_words]
    return " ".join(words) if words else None


def _scraper_get(target_url, timeout=25):
    if not SCRAPER_API_KEY:
        print("  WARNING: SCRAPER_API_KEY not set")
        return None
    try:
        r = requests.get(SCRAPER_API_BASE, params={
            "api_key": SCRAPER_API_KEY,
            "url": target_url,
            "country_code": "us",
        }, timeout=timeout)
        return r.text if r.status_code == 200 else None
    except Exception as e:
        print(f"  ScraperAPI error: {e}")
        return None


# =============================================================
# Amazon
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


# =============================================================
# eBay sold
# =============================================================

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
    median = prices[len(prices) // 2]

    return {
        "found": True,
        "median_price": median,
        "sold_count": len(prices),
        "min_price": min(prices),
        "max_price": max(prices),
        "url": target,
        "query": query,
    }


# =============================================================
# Mercari — sold listings via their search
# =============================================================

def mercari_lookup(query):
    if not query:
        return None

    # Mercari's "sold" filter: status=sold_out in the search URL
    target = (f"https://www.mercari.com/search/?keyword={quote_plus(query)}"
              f"&status=sold_out")
    html_text = _scraper_get(target)
    if not html_text:
        return None

    # Mercari embeds prices in JSON within the page
    prices = []
    for m in re.finditer(r'"price"\s*:\s*"?(\d{2,5})(?:\.\d{2})?"?', html_text):
        try:
            p = float(m.group(1))
            if 5 <= p <= 10000:   # sanity filter
                prices.append(p)
        except ValueError:
            continue

    if len(prices) < 3:
        return {"found": False, "query": query}

    prices.sort()
    median = prices[len(prices) // 2]

    return {
        "found": True,
        "median_price": median,
        "sold_count": len(prices),
        "min_price": min(prices),
        "max_price": max(prices),
        "url": target,
        "query": query,
    }


# =============================================================
# Google Shopping — current across retailers
# =============================================================

def google_shopping_lookup(query):
    if not query:
        return None

    target = f"https://www.google.com/search?tbm=shop&q={quote_plus(query)}"
    html_text = _scraper_get(target)
    if not html_text:
        return None

    prices = []
    # Google Shopping prices appear in format: $XX.XX or $X,XXX.XX
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
    median = prices[len(prices) // 2]

    return {
        "found": True,
        "median_price": median,
        "min_price": min(prices),
        "max_price": max(prices),
        "listing_count": len(prices),
        "url": target,
        "query": query,
    }


# =============================================================
# Verdict — now factors in 4 market signals
# =============================================================

def compute_verdict(deal_price, amazon, ebay, mercari, google):
    """
    Returns (verdict, profit_estimate, reasoning).
    Strategy: prefer Amazon FBA profit (highest margin with FBA fees factored in),
    fall back to eBay, use Mercari/Google as sanity check for "is this a real market price?"
    """
    if deal_price is None:
        return "WATCH", None, "Couldn't parse deal price"

    # Collect median/current prices from each marketplace for consensus check
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
        return "WATCH", None, "No marketplace data"

    # Sanity: if Amazon is 3x higher than ALL other markets, it's likely a bad Amazon match
    if amazon and amazon.get("found") and len(market_prices) >= 2:
        other = [p for name, p in market_prices if name != "Amazon"]
        if other:
            other_median = sorted(other)[len(other) // 2]
            if amazon["price"] > other_median * 2.5:
                # Amazon price looks inflated vs other markets — probably wrong product match
                amazon_suspect = True
            else:
                amazon_suspect = False
        else:
            amazon_suspect = False
    else:
        amazon_suspect = False

    # Compute best profit path
    best_profit = None
    best_reason = None

    # Path A: Amazon FBA
    if amazon and amazon.get("found") and not amazon_suspect:
        net = amazon["price"] * (1 - FBA_FEE_RATE) - FIXED_FBA_FEE
        profit = net - deal_price
        roi = (profit / deal_price * 100) if deal_price > 0 else 0
        if best_profit is None or profit > best_profit[0]:
            best_profit = (profit, roi, "Amazon FBA", amazon["price"])
            best_reason = f"Amazon ${amazon['price']:.0f} → ${profit:.0f} profit ({roi:.0f}% ROI)"

    # Path B: eBay
    if ebay and ebay.get("found") and ebay.get("sold_count", 0) >= 3:
        net = ebay["median_price"] * (1 - EBAY_FEE_RATE) - 5
        profit = net - deal_price
        roi = (profit / deal_price * 100) if deal_price > 0 else 0
        if best_profit is None or profit > best_profit[0]:
            best_profit = (profit, roi, "eBay", ebay["median_price"])
            best_reason = (f"eBay ${ebay['median_price']:.0f} median ({ebay['sold_count']} sold) "
                           f"→ ${profit:.0f} profit ({roi:.0f}% ROI)")

    # Path C: Mercari (lower fees, ~10%)
    if mercari and mercari.get("found") and mercari.get("sold_count", 0) >= 3:
        net = mercari["median_price"] * 0.90 - 3
        profit = net - deal_price
        roi = (profit / deal_price * 100) if deal_price > 0 else 0
        if best_profit is None or profit > best_profit[0]:
            best_profit = (profit, roi, "Mercari", mercari["median_price"])
            best_reason = (f"Mercari ${mercari['median_price']:.0f} median → "
                           f"${profit:.0f} profit ({roi:.0f}% ROI)")

    if not best_profit:
        return "WATCH", None, "Insufficient market data to assess"

    profit, roi, channel, sell_price = best_profit
    demand_sources = sum(1 for m in [ebay, mercari]
                        if m and m.get("found") and m.get("sold_count", 0) >= 3)

    # 3x rule check (buy at 1/3 the sell price)
    passes_3x = sell_price >= deal_price * 3

    if profit >= 10 and roi >= 50 and (demand_sources >= 1 or passes_3x):
        return "BUY", profit, f"{best_reason}; demand confirmed"

    if profit >= 6 and roi >= 30:
        return "WATCH", profit, f"{best_reason}; margin OK but not strong"

    if profit > 0:
        return "WATCH", profit, f"Thin margin: {best_reason}"

    return "SKIP", profit, f"Loses money: {best_reason}"


# =============================================================
# Top-level enrichment — PARALLEL lookups
# =============================================================

def enrich_deal(deal):
    title = deal["title"]
    desc = deal.get("description", "")

    deal_price = extract_price(title) or extract_price(desc)
    query = extract_search_term(title)
    if not query:
        return {**deal, "verdict": "SKIP", "verdict_reason": "Couldn't parse title"}

    print(f"  Looking up: '{query}' (deal price: ${deal_price})")

    # Run all 4 marketplace lookups in parallel
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_amazon = ex.submit(amazon_lookup, query)
        f_ebay = ex.submit(ebay_sold_lookup, query)
        f_mercari = ex.submit(mercari_lookup, query)
        f_google = ex.submit(google_shopping_lookup, query)

        amazon = f_amazon.result()
        ebay = f_ebay.result()
        mercari = f_mercari.result()
        google = f_google.result()

    verdict, profit, reason = compute_verdict(deal_price, amazon, ebay, mercari, google)

    return {
        **deal,
        "deal_price": deal_price,
        "amazon": amazon,
        "ebay": ebay,
        "mercari": mercari,
        "google": google,
        "verdict": verdict,
        "profit": profit,
        "verdict_reason": reason,
    }

"""
Enrichment — uses ScraperAPI to route Amazon + eBay lookups
through residential IPs so we don't get 503 blocked from GitHub's datacenter.
"""
import os
import re
import time
import requests
from urllib.parse import quote_plus

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
SCRAPER_API_BASE = "https://api.scraperapi.com/"

# Fee estimates for profit math
FBA_FEE_RATE = 0.25    # 15% referral + fulfillment/storage overhead
FIXED_FBA_FEE = 3.50   # typical small-item fulfillment fee

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")


# ---- Helpers ----

def extract_price(text):
    """Pull the first dollar amount from text."""
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
    """Turn a deal title into a clean search query for Amazon/eBay."""
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


def _scraper_get(target_url, timeout=30):
    """
    Fetch a URL through ScraperAPI. Returns HTML text or None on failure.
    Free tier: 5000 requests/month, shared residential proxies.
    """
    if not SCRAPER_API_KEY:
        print("  WARNING: SCRAPER_API_KEY not set; skipping lookup")
        return None

    try:
        r = requests.get(SCRAPER_API_BASE, params={
            "api_key": SCRAPER_API_KEY,
            "url": target_url,
            "country_code": "us",
        }, timeout=timeout)
        if r.status_code != 200:
            print(f"  ScraperAPI status {r.status_code}: {r.text[:150]}")
            return None
        return r.text
    except Exception as e:
        print(f"  ScraperAPI error: {e}")
        return None


# ---- Amazon lookup via ScraperAPI ----

def amazon_lookup(query):
    """Search Amazon through ScraperAPI, return dict with price/asin or None."""
    if not query:
        return None

    target = f"https://www.amazon.com/s?k={quote_plus(query)}&ref=nb_sb_noss"
    html_text = _scraper_get(target)
    if not html_text:
        return None

    # Amazon's first organic product result — price shown as two spans
    whole_match = re.search(r'<span class="a-price-whole">([\d,]+)', html_text)
    frac_match = re.search(r'<span class="a-price-fraction">(\d{2})', html_text)

    if not whole_match:
        return {"found": False, "query": query}

    try:
        price = float(whole_match.group(1).replace(",", ""))
        if frac_match:
            price += float(frac_match.group(1)) / 100
    except ValueError:
        return {"found": False, "query": query}

    # Grab the first product's ASIN for a clickable link
    asin_match = re.search(r'data-asin="([A-Z0-9]{10})"', html_text)
    asin = asin_match.group(1) if asin_match else None

    return {
        "found": True,
        "price": price,
        "asin": asin,
        "url": f"https://www.amazon.com/dp/{asin}" if asin else None,
        "query": query,
    }


# ---- eBay sold-listings via ScraperAPI ----

def ebay_sold_lookup(query):
    """Get median sold price + count for last 90 days."""
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

    # eBay's first result is always a placeholder "Shop on eBay" card
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


# ---- Verdict engine ----

def compute_verdict(deal_price, amazon, ebay):
    """Returns (verdict, profit_estimate, reasoning)."""
    if deal_price is None:
        return "WATCH", None, "Couldn't parse deal price from title"

    # If Amazon lookup failed entirely, try eBay-only analysis
    if not amazon or not amazon.get("found"):
        if ebay and ebay.get("found"):
            sell = ebay["median_price"]
            net = sell * 0.87 - 5  # ~13% eBay fees + shipping
            profit = net - deal_price
            roi = (profit / deal_price) * 100 if deal_price > 0 else 0
            if profit >= 10 and roi >= 50 and ebay["sold_count"] >= 3:
                return "BUY", profit, f"eBay-only: ${sell:.0f} median, ${profit:.0f} profit, {roi:.0f}% ROI"
            if profit >= 5:
                return "WATCH", profit, f"eBay margin thin: ${profit:.0f} profit, {roi:.0f}% ROI"
            return "SKIP", profit, f"eBay sells too low: ${sell:.0f}"
        return "WATCH", None, "No marketplace data found"

    amazon_price = amazon["price"]
    net_amazon = amazon_price * (1 - FBA_FEE_RATE) - FIXED_FBA_FEE
    profit_amazon = net_amazon - deal_price
    roi_amazon = (profit_amazon / deal_price) * 100 if deal_price > 0 else 0

    has_ebay_demand = ebay and ebay.get("found") and ebay.get("sold_count", 0) >= 3

    passes_3x = amazon_price >= deal_price * 3
    decent_margin = profit_amazon >= 8 and roi_amazon >= 40

    if (passes_3x or decent_margin) and has_ebay_demand:
        return ("BUY", profit_amazon,
                f"Amazon ${amazon_price:.0f} → ${profit_amazon:.0f} profit, "
                f"{roi_amazon:.0f}% ROI; eBay confirms demand")

    if decent_margin and not has_ebay_demand:
        return ("WATCH", profit_amazon,
                f"Amazon shows ${profit_amazon:.0f} profit but eBay demand unclear")

    if profit_amazon > 0:
        return ("WATCH", profit_amazon,
                f"Margin too thin: ${profit_amazon:.0f} profit, {roi_amazon:.0f}% ROI")

    return ("SKIP", profit_amazon,
            f"Loses money: Amazon ${amazon_price:.0f} after fees < ${deal_price:.0f} cost")


# ---- Top-level enrichment ----

def enrich_deal(deal):
    title = deal["title"]
    desc = deal.get("description", "")

    deal_price = extract_price(title) or extract_price(desc)
    query = extract_search_term(title)
    if not query:
        return {**deal, "verdict": "SKIP",
                "verdict_reason": "Couldn't parse title"}

    print(f"  Looking up: '{query}' (deal price: ${deal_price})")

    amazon = amazon_lookup(query)
    # Small delay between ScraperAPI calls to be polite (not strictly required)
    time.sleep(0.5)
    ebay = ebay_sold_lookup(query)

    verdict, profit, reason = compute_verdict(deal_price, amazon, ebay)

    return {
        **deal,
        "deal_price": deal_price,
        "amazon": amazon,
        "ebay": ebay,
        "verdict": verdict,
        "profit": profit,
        "verdict_reason": reason,
    }

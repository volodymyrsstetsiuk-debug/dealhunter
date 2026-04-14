"""
Enrichment — for a given deal, look up Amazon and eBay data,
then compute a BUY / WATCH / SKIP verdict using the 3x rule.
"""
import re
import time
import requests
from urllib.parse import quote_plus

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")

# Amazon FBA fees are roughly: 15% referral + ~$3-5 fulfillment for small items.
# We use 25% total as a safe blended estimate.
FBA_FEE_RATE = 0.25
FIXED_FBA_FEE = 3.50  # rough small-item fulfillment fee


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
    # Strip prices, percentages, special chars, common deal words
    t = re.sub(r"\$\s?\d[\d,.]*", " ", title)
    t = re.sub(r"\d{1,3}\s?%\s?off", " ", t, flags=re.I)
    t = re.sub(r"\b(deal|sale|clearance|free shipping|w/|with|coupon|promo|code|"
               r"only|today|new|hot|limited|extra|save)\b",
               " ", t, flags=re.I)
    t = re.sub(r"[^\w\s\-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    words = t.split()[:max_words]
    return " ".join(words) if words else None


# ---- Amazon scraping (light-touch, used only on already-scored candidates) ----

def amazon_lookup(query):
    """Search Amazon for the product. Returns dict with price, rank, title or None."""
    if not query:
        return None

    url = f"https://www.amazon.com/s?k={quote_plus(query)}&ref=nb_sb_noss"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/webp,*/*;q=0.8"),
    }

    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            print(f"  Amazon status {r.status_code} for '{query}'")
            return None
        html_text = r.text
    except Exception as e:
        print(f"  Amazon error: {e}")
        return None

    # First product result — extract price from search results page
    # Amazon's search result blocks contain spans with class "a-price-whole" and "a-price-fraction"
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

    # Try to extract first ASIN for a product link
    asin_match = re.search(r'data-asin="([A-Z0-9]{10})"', html_text)
    asin = asin_match.group(1) if asin_match else None

    return {
        "found": True,
        "price": price,
        "asin": asin,
        "url": f"https://www.amazon.com/dp/{asin}" if asin else None,
        "query": query,
    }


# ---- eBay sold-listings scrape ----

def ebay_sold_lookup(query):
    """Get median sold price + rough count from last 90 days."""
    if not query:
        return None

    # LH_Sold=1, LH_Complete=1 → sold listings only
    url = (f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(query)}"
           f"&LH_Sold=1&LH_Complete=1&_ipg=60")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            print(f"  eBay status {r.status_code} for '{query}'")
            return None
        html_text = r.text
    except Exception as e:
        print(f"  eBay error: {e}")
        return None

    # Pull all visible prices from sold-listing cards
    # eBay structure: <span class="s-item__price">$XX.XX</span>
    prices = []
    for m in re.finditer(r'<span class="s-item__price">[^<]*?\$([\d,]+\.\d{2})',
                         html_text):
        try:
            prices.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue

    # Drop the first "shop on eBay" placeholder if present
    if prices:
        prices = prices[1:] if len(prices) > 5 else prices

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
        "url": url,
        "query": query,
    }


# ---- Verdict logic ----

def compute_verdict(deal_price, amazon, ebay):
    """
    Returns (verdict, profit_estimate, reasoning).
    verdict: "BUY" | "WATCH" | "SKIP"
    """
    if deal_price is None:
        return "WATCH", None, "Couldn't parse deal price from title"

    if not amazon or not amazon.get("found"):
        if ebay and ebay.get("found"):
            # Fall back to eBay-only analysis
            sell = ebay["median_price"]
            net = sell * 0.87 - 5  # ~13% eBay fees + shipping estimate
            profit = net - deal_price
            roi = (profit / deal_price) * 100 if deal_price > 0 else 0
            if profit >= 10 and roi >= 50 and ebay["sold_count"] >= 3:
                return "BUY", profit, f"eBay-only: ${sell:.0f} median, ${profit:.0f} profit, {roi:.0f}% ROI"
            elif profit >= 5:
                return "WATCH", profit, f"eBay margin thin: ${profit:.0f} profit, {roi:.0f}% ROI"
            else:
                return "SKIP", profit, f"eBay sells too low: ${sell:.0f}"
        return "WATCH", None, "No marketplace data found"

    amazon_price = amazon["price"]
    net_amazon = amazon_price * (1 - FBA_FEE_RATE) - FIXED_FBA_FEE
    profit_amazon = net_amazon - deal_price
    roi_amazon = (profit_amazon / deal_price) * 100 if deal_price > 0 else 0

    # eBay confirms demand exists
    has_ebay_demand = ebay and ebay.get("found") and ebay.get("sold_count", 0) >= 3

    # 3x rule check
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
    """Fully enrich a deal: Amazon lookup + eBay lookup + verdict."""
    title = deal["title"]
    desc = deal.get("description", "")

    # Try to find a price in title or description
    deal_price = extract_price(title) or extract_price(desc)

    # Build search query
    query = extract_search_term(title)
    if not query:
        return {**deal, "verdict": "SKIP", "verdict_reason": "Couldn't parse title"}

    print(f"  Looking up: '{query}' (deal price: ${deal_price})")

    # Polite delays between marketplace requests
    amazon = amazon_lookup(query)
    time.sleep(2)
    ebay = ebay_sold_lookup(query)
    time.sleep(1)

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

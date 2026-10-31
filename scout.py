"""Depop -> eBay Scout.

Finds Depop listings that are worth BUYING and relisting on eBay:
  1. searches Depop (public listings only, slow and polite) with Scrapling,
  2. reads each promising listing (all photos, shipping, size, condition),
  3. looks up what the same kind of item sells for on eBay (sold prices first, active as fallback),
  4. works out your real cost (item + Depop buyer fee + Depop shipping + tax) and eBay fees + postage,
  5. posts the good ones to Discord, each as one message with up to 8 photos.

Runs on GitHub Actions on a schedule (see .github/workflows/scout.yml). Settings live in settings.py.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import settings as S

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "state.json"
DAY = 86400
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"


# ============================================================ small helpers
def now() -> float:
    return time.time()


def money(x: float) -> str:
    return f"${x:,.2f}"


def words(text: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split() if len(w) > 1 or w.isdigit()]


def to_price(v) -> float | None:
    """Price from a number, a "12.00" string, or a dict like {"amount": 12} / {"priceAmount": "12.00"}."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if isinstance(v, int) and x >= 1000:   # whole-number cents (e.g. 2500 = $25.00)
            x = x / 100
        return x if x > 0 else None
    if isinstance(v, dict):
        for k in ("discountedPriceAmount", "discounted_price_amount", "priceAmount", "price_amount", "amount", "value", "price"):
            if k in v:
                p = to_price(v[k])
                if p:
                    return p
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(v))
    if not m:
        return None
    x = float(m.group(0).replace(",", ""))
    return x if x > 0 else None


def percentile(values: list[float], p: float) -> float:
    vals = sorted(values)
    if not vals:
        return 0.0
    k = (len(vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def has_word(text: str, word: str) -> bool:
    return re.search(rf"(^|[^a-z0-9]){re.escape(word.lower())}([^a-z0-9]|$)", str(text).lower()) is not None


def log(*a):
    print(*a, flush=True)


# ============================================================ state (kept between runs by GitHub's cache)
def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text())
    except Exception:
        st = {}
    st.setdefault("seen", {})       # listing id -> {"v": verdict, "t": time, "p": price}
    st.setdefault("cursor", 0)
    st.setdefault("comps", {})      # eBay search -> {"t": time, "sold": [...], "active": [...]}
    st.setdefault("warned", {})
    return st


def save_state(st: dict) -> None:
    cutoff = now() - 45 * DAY
    st["seen"] = {k: v for k, v in st["seen"].items() if v.get("t", 0) > cutoff}
    ccut = now() - S.EBAY_COMP_CACHE_HOURS * 3600
    st["comps"] = {k: v for k, v in st["comps"].items() if v.get("t", 0) > ccut}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st))


# ============================================================ network (Scrapling)
class Net:
    """Plain requests first (fast, browser-like TLS). Stealth browser only when a site blocks them."""

    def __init__(self):
        self._last = {}
        self._session = None
        self.requests = 0
        self.blocked = {"depop": 0, "ebay": 0}

    def _pause(self, host: str):
        lo, hi = S.DELAY_SECONDS
        wait = self._last.get(host, 0) + random.uniform(lo, hi) - now()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = now()

    @staticmethod
    def _blocked(resp) -> bool:
        if resp is None:
            return True
        status = getattr(resp, "status", 200)
        if status in (403, 429, 503) or status >= 500:
            return True
        body = (getattr(resp, "html_content", "") or "")[:4000].lower()
        return any(s in body for s in ("captcha", "cf-chl", "challenge-platform", "access denied", "pardon our interruption", "verify you are a human"))

    def get(self, url: str, params: dict | None = None, accept_json: bool = False):
        from scrapling.fetchers import Fetcher
        host = urllib.parse.urlparse(url).netloc
        self._pause(host)
        self.requests += 1
        headers = {"Accept": "application/json, text/plain, */*"} if accept_json else None
        try:
            return Fetcher.get(url, params=params, impersonate="chrome", stealthy_headers=True, timeout=30, retries=2, headers=headers)
        except Exception as e:  # network error
            log("  request failed:", url[:90], type(e).__name__)
            return None

    def browser(self, url: str, record_pattern: str | None = None):
        """Load a page in Scrapling's stealth browser. Returns (page, [recorded JSON responses])."""
        if not S.USE_BROWSER_FALLBACK:
            return None, []
        from scrapling.fetchers import StealthySession
        host = urllib.parse.urlparse(url).netloc
        self._pause(host)
        self.requests += 1
        try:
            if self._session is None:
                self._session = StealthySession(headless=True, solve_cloudflare=True, block_ads=True, record_requests=True, network_idle=True, timeout=60000)
                self._session.__enter__()
            self._session.network.clear()
            page = self._session.fetch(url)
            found = []
            if record_pattern:
                for r in self._session.network.search(url_pattern=record_pattern, limit=None):
                    try:
                        found.append(r.json())
                    except Exception:
                        pass
            return page, found
        except Exception as e:
            log("  browser failed:", url[:90], type(e).__name__, str(e)[:120])
            return None, []

    def close(self):
        if self._session is not None:
            try:
                self._session.__exit__(None, None, None)
            except Exception:
                pass


def page_text(resp) -> str:
    if resp is None:
        return ""
    for attr in ("html_content", "text"):
        v = getattr(resp, attr, None)
        if isinstance(v, str) and v:
            return v
    b = getattr(resp, "body", b"")
    return b.decode("utf-8", "ignore") if isinstance(b, (bytes, bytearray)) else str(b)


def page_json(resp):
    try:
        return resp.json()
    except Exception:
        try:
            return json.loads(page_text(resp))
        except Exception:
            return None


# ============================================================ Depop
DEPOP_SEARCH_APIS = [
    "https://webapi.depop.com/presentation/api/v1/search/products/",
    "https://webapi.depop.com/api/v3/search/products/",
    "https://webapi.depop.com/api/v2/search/products/",
]


def walk(obj):
    """Every dict inside a JSON value, in document order."""
    from collections import deque
    queue = deque([obj])
    while queue:
        x = queue.popleft()
        if isinstance(x, dict):
            yield x
            queue.extend(x.values())
        elif isinstance(x, list):
            queue.extend(x)


def best_picture_urls(pics) -> list[str]:
    """Depop picture data comes in several shapes; return one (largest) URL per photo, in order."""
    out = []
    if isinstance(pics, dict):
        pics = list(pics.values()) if not any(k in pics for k in ("url", "formats")) else [pics]
    for p in pics or []:
        if isinstance(p, str):
            if p.startswith("http"):
                out.append(p)
            continue
        if not isinstance(p, dict):
            continue
        cands = []
        for k, v in (p.get("formats") or p).items():
            u = v.get("url") if isinstance(v, dict) else v
            if isinstance(u, str) and u.startswith("http"):
                size = int(re.sub(r"\D", "", k) or 0) if k not in ("P0", "url") else 99999
                if k == "url":
                    size = 50000
                cands.append((size, u))
        if cands:
            out.append(sorted(cands, reverse=True)[0][1])
    seen, uniq = set(), []
    for u in out:
        key = re.sub(r"/P\d+\.", "/P.", u)
        if key not in seen:
            seen.add(key)
            uniq.append(u)
    return uniq


def normalize_depop(d: dict) -> dict | None:
    slug = d.get("slug")
    if not isinstance(slug, str) or not slug:
        return None
    price = None
    for k in ("discountedPrice", "discounted_price", "price", "pricing", "priceAmount"):
        if k in d:
            v = d[k]
            if isinstance(v, dict) and k == "pricing":
                v = v.get("discounted_price") or v.get("original_price") or v
            price = to_price(v)
            if price:
                break
    if not price:
        return None
    total = to_price(d.get("totalPrice") or d.get("total_price"))
    pics = d.get("pictureUrls") or d.get("pictures") or d.get("pictures_data") or d.get("images") or d.get("preview")
    brand = d.get("brand") or d.get("brandName") or d.get("brand_name") or ""
    if isinstance(brand, dict):
        brand = brand.get("name") or brand.get("title") or ""
    size = d.get("size") or ""
    if isinstance(size, dict):
        size = size.get("name") or ""
    if not size and isinstance(d.get("sizes"), list) and d["sizes"]:
        s0 = d["sizes"][0]
        size = s0.get("name", "") if isinstance(s0, dict) else str(s0)
    cond = d.get("condition") or d.get("conditionRaw") or ""
    if isinstance(cond, dict):
        cond = cond.get("name") or cond.get("slug") or cond.get("title") or ""
    status = str(d.get("status") or "").lower()
    sold = bool(d.get("isSold") or d.get("sold") or status in ("sold", "marked_as_sold"))
    ship = None
    ns = d.get("nationalShipping") or d.get("national_shipping") or d.get("shipping")
    if isinstance(ns, dict):
        if ns.get("free") or ns.get("isFree"):
            ship = 0.0
        else:
            ship = to_price(ns.get("price") or ns.get("cost") or ns.get("amount"))
    pdict = d.get("price") if isinstance(d.get("price"), dict) else {}
    for src in (d, pdict):
        for k in ("nationalShippingCost", "national_shipping_cost", "shippingPrice", "shipping_price"):
            if ship is None and k in src:
                ship = to_price(src[k]) if src[k] not in (0, "0", "0.00") else 0.0
    seller = d.get("seller") or d.get("sellerUsername") or ""
    if isinstance(seller, dict):
        seller = seller.get("username") or ""
    desc = d.get("description") or d.get("title") or ""
    title = (d.get("title") or str(desc).split("\n")[0] or "").strip()
    if not title:
        parts = slug.split("-")
        title = " ".join(parts[1:-1] if len(parts) > 2 else parts)
    return {
        "id": str(d.get("id") or slug), "slug": slug, "url": f"https://www.depop.com/products/{slug}/",
        "title": title[:140], "description": str(desc)[:1500], "brand": str(brand).strip(), "size": str(size).strip(),
        "condition": str(cond).strip(), "price": round(price, 2), "total": round(total, 2) if total and total >= price else None,
        "ship": ship, "photos": best_picture_urls(pics), "sold": sold, "seller": str(seller),
        "likes": d.get("likeCount") or d.get("likes") or d.get("favourites_count") or 0,
    }


def products_from_json(data) -> list[dict]:
    out, seen = [], set()
    for d in walk(data):
        if "slug" in d and any(k in d for k in ("price", "pricing", "priceAmount", "discountedPrice")):
            p = normalize_depop(d)
            if p and p["slug"] not in seen:
                seen.add(p["slug"])
                out.append(p)
    return out


def json_blobs_from_html(html: str) -> list:
    """__NEXT_DATA__, JSON-LD and other JSON <script> blocks on a page."""
    blobs = []
    for m in re.finditer(r'<script[^>]*type="application/(?:ld\+)?json"[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            blobs.append(json.loads(m.group(1)))
        except Exception:
            pass
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            blobs.append(json.loads(m.group(1)))
        except Exception:
            pass
    return blobs


def depop_search(net: Net, query: str) -> list[dict]:
    params = {"what": query, "country": "us", "currency": "USD", "items_per_page": S.DEPOP_RESULTS_PER_KEYWORD, "itemsPerPage": S.DEPOP_RESULTS_PER_KEYWORD}
    for api in DEPOP_SEARCH_APIS:
        r = net.get(api, params=params, accept_json=True)
        if r is not None and getattr(r, "status", 0) == 200:
            prods = products_from_json(page_json(r))
            if prods:
                return prods
    # fallback: the normal search page in the stealth browser, reading the data the page itself loads
    net.blocked["depop"] += 1
    page, recorded = net.browser(f"https://www.depop.com/search/?q={urllib.parse.quote(query)}", record_pattern=r"search/products")
    prods = []
    for blob in recorded:
        prods += products_from_json(blob)
    if not prods and page is not None:
        for blob in json_blobs_from_html(page_text(page)):
            prods += products_from_json(blob)
    return prods


def depop_details(net: Net, item: dict) -> dict:
    """Open the listing page for all photos, shipping, size and condition."""
    r = net.get(item["url"])
    html = page_text(r) if r is not None and not Net._blocked(r) else ""
    if not html:
        page, _ = net.browser(item["url"])
        html = page_text(page)
    if not html:
        return item
    best = None
    for blob in json_blobs_from_html(html):
        for d in walk(blob):
            if d.get("slug") == item["slug"]:
                p = normalize_depop(d)
                if p and (best is None or len(p["photos"]) > len(best["photos"])):
                    best = p
            if str(d.get("@type", "")).lower() == "product" and not best:
                imgs = d.get("image") or []
                imgs = [imgs] if isinstance(imgs, str) else imgs
                offers = d.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if imgs:
                    item["photos"] = item["photos"] or [u for u in imgs if isinstance(u, str)]
                if "outofstock" in str(offers.get("availability", "")).lower():
                    item["sold"] = True
    if best:
        if len(best["photos"]) > len(item["photos"]):
            item["photos"] = best["photos"]
        for k in ("size", "condition", "description", "brand"):
            if best.get(k) and not item.get(k):
                item[k] = best[k]
        if best.get("ship") is not None:
            item["ship"] = best["ship"]
        if best.get("total"):
            item["total"] = best["total"]
        item["sold"] = item["sold"] or best["sold"]
    return item


# ============================================================ eBay comps (Scrapling)
def parse_ebay_results(html: str) -> list[dict]:
    """Works with both eBay result layouts (s-item and the newer s-card)."""
    from scrapling.parser import Selector
    doc = Selector(html)
    cards = doc.css("li.s-item, li.s-card") or doc.css("div.s-card, div.s-item")
    out = []
    for c in cards:
        title = (c.css(".s-item__title ::text, .s-card__title ::text").getall() or [])
        title = " ".join(t.strip() for t in title if t.strip())
        title = re.sub(r"^(New Listing|Shop on eBay)\s*", "", title).replace("Opens in a new window or tab", "").strip()
        if not title or title.lower() == "shop on ebay":
            continue
        text = " ".join(t.strip() for t in c.css("::text").getall() if t.strip())
        price_txt = " ".join(c.css(".s-item__price ::text, .s-card__price ::text").getall()) or text
        if re.search(r"\$[\d,.]+\s*to\s*\$", price_txt):
            continue   # multi-variation price range: ambiguous
        price = to_price(re.search(r"\$\s*[\d,]+(?:\.\d{2})?", price_txt).group(0)) if re.search(r"\$\s*[\d,]+(?:\.\d{2})?", price_txt) else None
        if not price:
            continue
        ship = None
        m = re.search(r"(free\s+(?:shipping|delivery))|\+?\s*\$\s*([\d,]+(?:\.\d{2})?)\s*(?:shipping|delivery|postage)", text, re.I)
        if m:
            ship = 0.0 if m.group(1) else float(m.group(2).replace(",", ""))
        link = next((h for h in c.css("a::attr(href)").getall() if "/itm/" in h), "")
        sold = bool(re.search(r"\bSold\s+[A-Z][a-z]{2}\s+\d{1,2}", text))
        out.append({"title": title, "price": price, "ship": ship, "total": round(price + (ship or 0), 2), "url": link.split("?")[0], "sold": sold})
    return out


def ebay_url(query: str, condition: str, sold: bool) -> str:
    cond = "1000|1500" if condition == "new" else "3000"
    q = {"_nkw": query, "_sacat": "0", "LH_BIN": "1", "LH_PrefLoc": "1", "_ipg": "60", "LH_ItemCondition": cond}
    if sold:
        q.update({"LH_Sold": "1", "LH_Complete": "1"})
        q.pop("LH_BIN")
    return "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode(q)


def ebay_comps(net: Net, st: dict, query: str, condition: str) -> dict:
    key = f"{condition}|{query.lower()}"
    cached = st["comps"].get(key)
    if cached and now() - cached["t"] < S.EBAY_COMP_CACHE_HOURS * 3600:
        return cached
    res = {"t": now(), "sold": [], "active": []}
    for kind in (["sold", "active"] if S.PREFER_SOLD else ["active"]):
        url = ebay_url(query, condition, kind == "sold")
        r = net.get(url)
        html = page_text(r) if r is not None and not Net._blocked(r) else ""
        if not html or "s-item" not in html and "s-card" not in html:
            net.blocked["ebay"] += 1
            page, _ = net.browser(url)
            html = page_text(page)
        items = parse_ebay_results(html) if html else []
        if kind == "sold":
            items = [i for i in items if i["sold"]] or []
        res[kind] = items
        if kind == "sold" and len(items) >= S.MIN_COMPS:
            break   # enough sold data: skip the active lookup
    st["comps"][key] = res
    return res


GARMENT_WORDS = set("""jacket coat parka puffer fleece vest hoodie sweatshirt crewneck sweater cardigan shirt tee t-shirt polo jersey flannel
pants jeans shorts overalls skirt dress leggings joggers trousers boots boot shoes sneakers loafer clog bag backpack purse tote hat cap beanie
watch sunglasses camera doll windbreaker tracksuit pullover blazer""".split())


def comp_query(item: dict, keyword: str) -> str:
    """What an eBay buyer would type: brand + the item's key words."""
    brand = item["brand"] if item["brand"] and item["brand"].lower() not in ("other", "unbranded", "vintage") else ""
    base = words(item["title"])
    kw = words(keyword)
    bw = set(words(brand))
    keep = [w for w in dict.fromkeys(base) if (w in GARMENT_WORDS or w in kw) and w not in bw][:4]
    if not keep:
        keep = [w for w in kw if w not in bw][:4]
    q = " ".join(([brand] if brand else []) + keep)
    return q.strip() or keyword


def matches(item: dict, comp_title: str, query: str) -> bool:
    t = comp_title.lower()
    if re.search(r"\b(lot|bundle|wholesale|bulk|kids|youth|toddler)\b", t) and not re.search(r"\b(lot|bundle|kids|youth)\b", item["title"].lower()):
        return False
    b = item["brand"].lower()
    bw = [w for w in words(b) if w not in ("the", "and", "co", "company")][:2]
    if b and b not in ("other", "unbranded", "vintage") and bw and not all(has_word(t, w) or w in t.replace(" ", "") for w in bw):
        return False
    q = [w for w in words(query) if w not in words(b)]
    if q and sum(1 for w in q if w in t) / len(q) < 0.5:
        return False
    return True


def condition_of(item: dict) -> str:
    c = (item["condition"] + " " + item["description"][:200]).lower()
    return "new" if re.search(r"\b(brand new|new with tags|nwt|bnwt|deadstock|never worn)\b", c) else "used"


# ============================================================ money
def weight_class(item: dict) -> str:
    t = (item["title"] + " " + item["description"][:300]).lower()
    if re.search(r"\b(boots?|parka|coat|puffer|down jacket|nuptse|blanket|camera|game ?boy|console|doll|leather jacket|carhartt detroit|chore coat|backpack)\b", t):
        return "heavy"
    if re.search(r"\b(jeans|hoodie|sweatshirt|sweater|fleece|jacket|pants|shoes|sneakers|clog|bag|overalls|cardigan|crewneck)\b", t):
        return "medium"
    return "light"


def depop_cost(item: dict) -> dict:
    fee = (item["total"] - item["price"]) if item.get("total") else item["price"] * S.DEPOP_BUYER_FEE_PCT / 100 + S.DEPOP_BUYER_FEE_FIXED
    ship_known = item.get("ship") is not None
    ship = item["ship"] if ship_known else S.DEPOP_SHIPPING_ESTIMATE[weight_class(item)]
    tax = (item["price"] + fee) * S.BUY_TAX_PCT / 100
    return {"item": item["price"], "fee": round(fee, 2), "fee_exact": bool(item.get("total")), "ship": round(ship, 2), "ship_known": ship_known,
            "tax": round(tax, 2), "total": round(item["price"] + fee + ship + tax, 2)}


def ebay_side(list_price: float, item: dict) -> dict:
    base = list_price * (1 + S.EBAY_BUYER_TAX_PCT / 100)
    fvf = base * S.EBAY_FEE_PCT / 100
    ads = base * S.EBAY_AD_RATE_PCT / 100
    order = S.EBAY_ORDER_FEE if base > 10 else S.EBAY_ORDER_FEE_SMALL
    post = S.SHIP_TO_EBAY_BUYER[weight_class(item)]
    total = fvf + ads + order + post + S.PACKAGING_COST
    return {"fvf": round(fvf, 2), "ads": round(ads, 2), "order": order, "post": post, "pack": S.PACKAGING_COST, "total": round(total, 2), "net": round(list_price - total, 2)}


def evaluate(item: dict, comps: dict, query: str) -> dict | None:
    sold = [c for c in comps.get("sold", []) if matches(item, c["title"], query)]
    active = [c for c in comps.get("active", []) if matches(item, c["title"], query)]
    if len(sold) >= S.MIN_COMPS:
        basis, prices = "sold", [c["total"] for c in sold]
        est = percentile(prices, S.PRICE_PERCENTILE)
        conf = "High" if len(sold) >= 6 else "Medium"
    elif len(active) >= S.MIN_COMPS:
        basis, prices = "active", [c["total"] for c in active]
        est = percentile(prices, S.PRICE_PERCENTILE) * (1 - S.ACTIVE_PRICE_HAIRCUT_PCT / 100)
        conf = "Medium" if len(active) >= 8 else "Low"
    else:
        return None
    list_price = max(0.99, round(est) - 0.01)
    cost = depop_cost(item)
    ebay = ebay_side(list_price, item)
    profit = round(ebay["net"] - cost["total"], 2)
    roi = profit / cost["total"] * 100 if cost["total"] else 0
    comps_used = sold if basis == "sold" else active
    return {"basis": basis, "n": len(prices), "low": min(prices), "median": percentile(prices, 0.5), "list": list_price, "cost": cost,
            "ebay": ebay, "profit": profit, "roi": roi, "conf": conf, "examples": sorted(comps_used, key=lambda c: c["total"])[:3]}


# ============================================================ Discord
SENT: list = []   # what would have been sent (dry runs and tests)


def discord(payload: dict) -> bool:
    if DRY_RUN or not WEBHOOK:
        SENT.append(payload)
        log("  [discord]", json.dumps({k: v for k, v in payload.items() if k != "_plain"})[:300])
        return True
    url = WEBHOOK + ("&" if "?" in WEBHOOK else "?") + "wait=true"
    plain = payload.get("_plain", "")
    payload = {k: v for k, v in payload.items() if k != "_plain"}
    payload["allowed_mentions"] = {"parse": []}
    for attempt in range(3):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "User-Agent": "DepopScout/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30):
                time.sleep(S.DISCORD_PACE_SECONDS)
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    time.sleep(min(10, float(json.loads(e.read() or b"{}").get("retry_after", 2))))
                except Exception:
                    time.sleep(2)
                continue
            if e.code == 400 and payload.get("embeds"):
                payload = {"content": plain[:1990], "allowed_mentions": {"parse": []}}
                continue
            log("  discord error", e.code)
            return False
        except Exception as e:
            log("  discord error", type(e).__name__)
            time.sleep(2)
    return False


def gallery(url: str, photos: list[str], color: int) -> list[dict]:
    """Discord shows up to 4 images as one gallery when embeds share a url: two urls = up to 8 photos in ONE message."""
    url_b = url + ("&" if "?" in url else "?") + "photos=2"
    return [{"url": url if i < 3 else url_b, "color": color, "image": {"url": u}} for i, u in enumerate(photos[1:S.MAX_PHOTOS])]


def alert(item: dict, ev: dict, keyword: str, query: str) -> bool:
    c, e = ev["cost"], ev["ebay"]
    color = 0x22C55E if ev["conf"] == "High" else 0x3B82F6 if ev["conf"] == "Medium" else 0xEAB308
    sold_url = "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode({"_nkw": query, "LH_Sold": "1", "LH_Complete": "1"})
    checks = []
    if not c["ship_known"]:
        checks.append("Depop shipping not shown: estimated " + money(c["ship"]))
    if not c["fee_exact"]:
        checks.append("Depop buyer fee estimated at 5% + $1 (check at checkout)")
    if ev["basis"] == "active":
        checks.append("based on active eBay prices (sold prices weren't available): open the sold link before buying")
    if not item["size"]:
        checks.append("size not listed: ask the seller")
    ex = "\n".join(f"• [{money(x['total'])}]({x['url']}) {x['title'][:60]}" for x in ev["examples"] if x.get("url")) or "—"
    title_draft = " ".join(x for x in [item["brand"], " ".join(words(item["title"])[:8]).title(), ("Size " + item["size"]) if item["size"] else "", "New" if condition_of(item) == "new" else "Pre-owned"] if x)[:80]
    fields = [
        {"name": f"{'🟢' if ev['conf'] == 'High' else '🟡'} Confidence", "value": f"**{ev['conf']}** • {ev['n']} matching eBay {ev['basis']} listing{'s' if ev['n'] != 1 else ''} • low {money(ev['low'])}, median {money(ev['median'])}", "inline": False},
        {"name": "🛍️ You pay on Depop", "value": f"**{money(c['total'])}**\nitem {money(c['item'])} + fee {money(c['fee'])}\n+ ship {money(c['ship'])} + tax {money(c['tax'])}", "inline": True},
        {"name": "💲 List on eBay at", "value": f"**{money(ev['list'])}**\nfree shipping to buyer", "inline": True},
        {"name": "💰 Profit", "value": f"**{money(ev['profit'])}**\n{ev['roi']:.0f}% ROI", "inline": True},
        {"name": "🧾 eBay costs", "value": f"fee {money(e['fvf'])} + ads {money(e['ads'])} + {money(e['order'])}\npostage {money(e['post'])} + pack {money(e['pack'])} = **{money(e['total'])}**", "inline": False},
        {"name": "📏 Details", "value": f"Size: {item['size'] or '?'} • Condition: {item['condition'] or '?'} • Seller: {item['seller'] or '?'}", "inline": False},
        {"name": "📦 Cheapest eBay matches", "value": ex[:1000], "inline": False},
    ]
    if checks:
        fields.append({"name": "❓ Check before buying", "value": "\n".join("• " + x for x in checks)[:1000], "inline": False})
    fields.append({"name": "🏷️ eBay title draft", "value": f"`{title_draft}`", "inline": False})
    fields.append({"name": "Links", "value": f"[Buy on Depop]({item['url']})  •  [eBay sold prices]({sold_url})", "inline": False})
    main = {"title": f"🛍️ {item['brand'] + ': ' if item['brand'] else ''}{item['title']}"[:240], "url": item["url"], "color": color, "fields": fields,
            "image": {"url": item["photos"][0]} if item["photos"] else None, "footer": {"text": f"Search: {keyword}"}}
    main = {k: v for k, v in main.items() if v is not None}
    plain = f"{item['title']} | Depop {money(c['total'])} total -> eBay {money(ev['list'])} | profit {money(ev['profit'])} | {item['url']}"
    return discord({"embeds": [main] + gallery(item["url"], item["photos"], color), "_plain": plain})


# ============================================================ main run
def is_junk(item: dict) -> bool:
    t = item["title"] + " " + item["description"][:300]
    return any(has_word(t, w) for w in S.SKIP_WORDS)


def run() -> dict:
    st = load_state()
    kws = [l.strip() for l in (ROOT / "keywords.txt").read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
    start = st["cursor"] % len(kws)
    batch = (kws + kws)[start:start + min(S.KEYWORDS_PER_RUN, len(kws))]
    st["cursor"] = (start + len(batch)) % len(kws)
    net = Net()
    stats = {"keywords": len(batch), "depop_found": 0, "new": 0, "ebay_checked": 0, "no_comps": 0, "not_profitable": 0, "alerts": 0, "sold_out": 0}
    pending = []
    log(f"Searching {len(batch)} keywords (from #{start + 1})")
    try:
        for kw in batch:
            items = depop_search(net, kw)
            stats["depop_found"] += len(items)
            fresh = []
            for it in items:
                if it["sold"] or is_junk(it) or not (S.MIN_DEPOP_PRICE <= it["price"] <= S.MAX_DEPOP_PRICE):
                    continue
                seen = st["seen"].get(it["id"])
                if seen:
                    window = S.REMEMBER_ALERTED_DAYS if seen["v"] == "alert" else S.REMEMBER_CHECKED_DAYS
                    dropped = seen.get("p") and it["price"] <= seen["p"] * 0.9
                    if now() - seen["t"] < window * DAY and not dropped:
                        continue
                fresh.append(it)
            # the cheapest listings relative to the rest of this search are the likeliest bargains
            if fresh:
                med = percentile([i["price"] for i in items if not i["sold"]] or [1], 0.5)
                fresh.sort(key=lambda i: i["price"] / med - (0.1 if i["brand"] else 0))
            for it in fresh[:S.CANDIDATES_PER_KEYWORD]:
                pending.append((kw, it))
            stats["new"] += len(fresh)
            log(f"  {kw}: {len(items)} listings, {len(fresh)} new")

        alerts_sent = 0
        for kw, it in pending[:S.MAX_EBAY_CHECKS_PER_RUN]:
            it = depop_details(net, it)
            if it["sold"]:
                stats["sold_out"] += 1
                st["seen"][it["id"]] = {"v": "fail", "t": now(), "p": it["price"]}
                continue
            q = comp_query(it, kw)
            comps = ebay_comps(net, st, q, condition_of(it))
            stats["ebay_checked"] += 1
            ev = evaluate(it, comps, q)
            if not ev:
                stats["no_comps"] += 1
                st["seen"][it["id"]] = {"v": "fail", "t": now(), "p": it["price"]}
                continue
            log(f"  {it['title'][:50]}: Depop {money(ev['cost']['total'])} -> eBay {money(ev['list'])} = {money(ev['profit'])} ({ev['basis']}, {ev['n']})")
            if ev["profit"] >= S.MIN_PROFIT and ev["roi"] >= S.MIN_ROI_PCT and alerts_sent < S.MAX_ALERTS_PER_RUN:
                if alert(it, ev, kw, q):
                    alerts_sent += 1
                    st["seen"][it["id"]] = {"v": "alert", "t": now(), "p": it["price"]}
                continue
            stats["not_profitable"] += 1
            st["seen"][it["id"]] = {"v": "fail", "t": now(), "p": it["price"]}
        stats["alerts"] = alerts_sent
    finally:
        net.close()
        stats["requests"] = net.requests
        stats["blocked"] = dict(net.blocked)
        save_state(st)

    if S.SEND_RUN_SUMMARY:
        lines = [f"📋 **Depop → eBay scout**: {stats['keywords']} searches, {stats['depop_found']} Depop listings, {stats['new']} new, {stats['ebay_checked']} checked on eBay → **{stats['alerts']} find{'s' if stats['alerts'] != 1 else ''}**.",
                 f"Not enough eBay matches: {stats['no_comps']} • below your ${S.MIN_PROFIT:.0f} / {S.MIN_ROI_PCT}% target: {stats['not_profitable']} • already sold: {stats['sold_out']}. Next run starts at keyword #{st['cursor'] + 1} of {len(kws)}."]
        if stats["depop_found"] == 0:
            lines.append("⚠️ Depop returned nothing this run. It may be blocking GitHub's servers. If this repeats, the scout may need to run from your own PC or through a proxy.")
        elif stats["blocked"]["ebay"] > stats["ebay_checked"] * 0.5 and stats["ebay_checked"]:
            lines.append("⚠️ eBay blocked many lookups this run, so fewer items could be priced.")
        discord({"content": "\n".join(lines)})
    log(json.dumps(stats))
    return stats


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # tell Discord instead of failing silently
        discord({"content": f"⚠️ Depop scout crashed: {type(e).__name__}: {str(e)[:300]}"})
        raise

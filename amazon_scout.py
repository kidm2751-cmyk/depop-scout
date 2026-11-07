"""Amazon -> eBay Deal Scout (replaces the n8n + Bright Data Deal Scout).

Every run (4 a day on GitHub Actions):
  1. searches Amazon with Scrapling for 250 of today's 1,000 keywords (5,000 in all, a new 1,000 each day),
  2. reads every result's price, Amazon's typical / list price, coupons, deal badges and "bought in past month",
  3. ranks them: strongest deals and highest demand first (a deal is a bonus, not a requirement),
  4. opens the best ones for all photos, brand, coupon and "lowest price in 30 days",
  5. prices each on eBay (sold listings first, active as fallback) and works out profit after every fee,
  6. posts each find to Discord as ONE message with up to 8 photos, the deal, demand, costs and profit.

It also keeps its own price history, so it can say "lowest price in 90 days / a year" as the history builds up.
Settings live in amazon_settings.py. Shared network, eBay and Discord code comes from scout.py.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.parse
from pathlib import Path

import scout as D               # shared: Scrapling network, eBay pricing, Discord
import amazon_settings as A

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state_amazon" / "state.json"
KEYWORDS_FILE = ROOT / "amazon_keywords.txt"
D.WEBHOOK = (os.environ.get("AMAZON_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
D.DEBUG_DIR = ROOT / "debug_amazon"
DAY = 86400
log, money, now, to_price, percentile, has_word = D.log, D.money, D.now, D.to_price, D.percentile, D.has_word
AMAZON = "https://www.amazon.com"


# ============================================================ state
def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text())
    except Exception:
        st = {}
    for k in ("seen", "comps", "hist", "rotation"):
        st.setdefault(k, {})
    return st


def save_state(st: dict) -> None:
    t = now()
    st["seen"] = {k: v for k, v in st["seen"].items() if t - v.get("t", 0) < 30 * DAY}
    st["comps"] = {k: v for k, v in st["comps"].items() if t - v.get("t", 0) < D.S.EBAY_COMP_CACHE_HOURS * 3600}
    today = day_no()
    st["hist"] = {k: v for k, v in st["hist"].items() if today - v[1] < 400}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, separators=(",", ":")))


def day_no() -> int:
    return int(now() // DAY)


# ============================================================ keyword rotation (1,000 a day, 250 a run)
def todays_batch(kws: list[str], st: dict):
    per_day = max(1, min(A.KEYWORDS_PER_DAY, len(kws)))
    days = max(1, -(-len(kws) // per_day))
    rot = st["rotation"]
    today = D.local_today()
    if rot.get("date") != today:
        day = (rot["day"] + 1) % days if isinstance(rot.get("day"), int) else 0
        rot.update(date=today, day=day, pos=0)
    rot["day"] %= days
    block = kws[rot["day"] * per_day:(rot["day"] + 1) * per_day]
    start = rot["pos"] % len(block)
    batch = (block + block)[start:start + min(A.KEYWORDS_PER_RUN, len(block))]
    rot["pos"] = start + len(batch)
    return rot, block, start, batch, days


# ============================================================ reading Amazon pages
BLOCK_SIGNS = ("enter the characters you see below", "validatecaptcha", "api-services-support@amazon.com",
               "make sure you're not a robot", "to discuss automated access", "sorry! something went wrong")


def amazon_blocked(html: str, status: int = 200) -> bool:
    if status in (403, 429, 503) or not html:
        return True
    low = html[:20000].lower()
    return any(s in low for s in BLOCK_SIGNS)


def big_image(url: str) -> str:
    """Amazon thumbnails -> the large version of the same photo."""
    return re.sub(r"\._[^/]*?_\.(jpg|jpeg|png|webp)$", r"._AC_SL1500_.\1", url or "", flags=re.I)


def parse_count(txt: str) -> int:
    m = re.search(r"([\d.,]+)\s*([KkMm])?", txt or "")
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * (1000 if (m.group(2) or "").lower() == "k" else 1_000_000 if (m.group(2) or "").lower() == "m" else 1))


def read_signals(text: str) -> dict:
    """Deal and demand signals that Amazon prints in plain text (search cards and product pages)."""
    out = {}
    m = re.search(r"\b(Typical|List|Was)(?:\s+[Pp]rice)?\s*:?\s*\$\s*([\d,]+\.\d{2})", text)
    if m:
        out["typical"], out["typical_kind"] = to_price(m.group(2)), {"Typical": "typical", "List": "list", "Was": "was"}[m.group(1)]
    m = re.search(r"Save\s+(\d{1,2})%\s+with\s+coupon|Apply\s+(\d{1,2})%\s+coupon|(\d{1,2})%\s+off\s+coupon", text, re.I)
    if m:
        out["coupon_pct"] = int(next(g for g in m.groups() if g))
    else:
        m = re.search(r"Save\s+\$\s*([\d.]+)\s+with\s+coupon|Apply\s+\$\s*([\d.]+)\s+coupon|\$\s*([\d.]+)\s+off\s+coupon", text, re.I)
        if m:
            out["coupon_amt"] = float(next(g for g in m.groups() if g))
    m = re.search(r"(Lightning Deal|Limited time deal|Black Friday Deal|Cyber Monday Deal|Prime Big Deal|Prime Day Deal|Deal of the Day|Holiday Deal|Prime Exclusive Deal)", text, re.I)
    if m:
        out["badge"] = m.group(1)
    if re.search(r"lowest price in 30 days", text, re.I):
        out["low30"] = True
    m = re.search(r"([\d.,]+[KkMm]?)\+\s*bought in past month", text)
    if m:
        out["bought"] = parse_count(m.group(1))
    m = re.search(r"(\d\.\d)\s+out of 5 stars", text)
    if m:
        out["rating"] = float(m.group(1))
    m = re.search(r"([\d,]+)\s+(?:global\s+)?ratings", text)
    if m:
        out["reviews"] = parse_count(m.group(1))
    m = re.search(r"#([\d,]+)\s+in\s+([A-Z][A-Za-z&,' ]{2,40}?)\s*\(", text)
    if m:
        out["rank"], out["rank_cat"] = parse_count(m.group(1)), m.group(2).strip()
    m = re.search(r"\b(#1 Best Seller|Best Seller|Amazon's Choice|Overall Pick)\b", text)
    if m:
        out["tag"] = m.group(1)
    if re.search(r"currently unavailable|temporarily out of stock", text, re.I):
        out["unavailable"] = True
    return out


def parse_search(html: str) -> list[dict]:
    from scrapling.parser import Selector
    doc = Selector(html)
    out, seen = [], set()
    for c in doc.css('div[data-component-type="s-search-result"], div.s-result-item[data-asin]'):
        asin = (c.attrib.get("data-asin") or "").strip()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin) or asin in seen:
            continue
        text = " ".join(t.strip() for t in c.css("::text").getall() if t.strip())
        h2 = c.css("h2")
        title = (h2[0].attrib.get("aria-label") or "") if h2 else ""
        title = title or " ".join(t.strip() for t in c.css("h2 ::text").getall() if t.strip())
        title = re.sub(r"^Sponsored Ad\s*[-–]\s*", "", title).strip()
        if not title:
            continue
        price = None
        for p in c.css(".a-price .a-offscreen::text").getall():   # first non-strikethrough price
            price = to_price(p)
            if price:
                break
        strike = [to_price(p) for p in c.css('.a-price.a-text-price .a-offscreen::text, .a-text-price[data-a-strike="true"] .a-offscreen::text').getall()]
        if price and strike and strike[0] == price:  # the first price found was the strikethrough one
            rest = [to_price(p) for p in c.css(".a-price:not(.a-text-price) .a-offscreen::text").getall()]
            price = next((x for x in rest if x), price)
        if not price:
            continue
        labels = " ".join(c.css("[aria-label]::attr(aria-label)").getall())   # ratings counts often live only in aria-labels
        sig = read_signals(text + " | " + labels)
        if "typical" not in sig and strike and strike[0] and strike[0] > price:
            sig["typical"], sig["typical_kind"] = strike[0], "list"
        brand = ""   # some layouts print the brand on its own line above the title
        for v in c.css('div[data-cy="title-recipe"] span.a-size-base-plus.a-color-base::text, h5 span.a-size-base-plus::text').getall():
            v = v.strip()
            if v and len(v) <= 40 and v.lower() != title.lower() and not v.lower().startswith("sponsored"):
                brand = v
                break
        img = (c.css("img.s-image::attr(src)").getall() or [""])[0]
        seen.add(asin)
        out.append({
            "asin": asin, "title": title[:300], "brand": brand, "price": round(price, 2),
            "url": f"{AMAZON}/dp/{asin}", "photos": [big_image(img)] if img else [],
            "sponsored": bool(re.search(r"\bSponsored\b", text)) or "AdHolder" in (c.attrib.get("class") or ""),
            "prime": bool(c.css("i.a-icon-prime")) or " Prime " in f" {text} ",
            **sig,
        })
    return out


def parse_product(html: str) -> dict:
    """The product page: all photos, brand, confirmed price, coupon, 30-day low badge, best-seller rank."""
    from scrapling.parser import Selector
    doc = Selector(html)
    out = {}
    t = " ".join(x.strip() for x in doc.css("#productTitle::text").getall() if x.strip())
    if t:
        out["title"] = t[:300]
    by = " ".join(x.strip() for x in doc.css("#bylineInfo::text, #bylineInfo ::text").getall() if x.strip())
    m = re.search(r"Visit the (.+?) Store", by) or re.search(r"Brand:\s*(.+)", by)
    if m:
        out["brand"] = m.group(1).strip()
    for sel in ("#corePrice_feature_div .a-price .a-offscreen::text", ".priceToPay .a-offscreen::text", "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen::text",
                "#apex_desktop .a-price .a-offscreen::text", "#price_inside_buybox::text", "#priceblock_ourprice::text", "#priceblock_dealprice::text"):
        p = next((to_price(x) for x in doc.css(sel).getall() if to_price(x)), None)
        if p:
            out["price"] = round(p, 2)
            break
    zones = " ".join(" ".join(x.strip() for x in doc.css(f"{z} ::text").getall() if x.strip()) for z in
                     ("#corePriceDisplay_desktop_feature_div", "#corePrice_feature_div", "#apex_desktop", "#dealBadge_feature_div", "#promoPriceBlockMessage_feature_div",
                      "#couponBadgeRegularVpc", "#vpcButton", "#social-proofing-faceout-title-tk_bought", "#averageCustomerReviews", "#acrCustomerReviewText",
                      "#productDetails_detailBullets_sections1", "#detailBulletsWrapper_feature_div", "#availability", "#merchantInfoFeature_feature_div"))
    sig = read_signals(zones or html)
    out.update({k: v for k, v in sig.items()})
    photos = []
    for u in re.findall(r'"hiRes"\s*:\s*"(https://m\.media-amazon\.com/images/I/[^"]+)"', html) or re.findall(r'"large"\s*:\s*"(https://m\.media-amazon\.com/images/I/[^"]+)"', html):
        if u not in photos:
            photos.append(u)
    if not photos:
        main = doc.css("#landingImage")
        if main:
            u = main.attrib.get("data-old-hires") or main.attrib.get("src") or ""
            if u.startswith("http"):
                photos.append(big_image(u))
    if photos:
        out["photos"] = photos
    if re.search(r"Ships from\s+Amazon|Sold by\s+Amazon", zones or html):
        out["by_amazon"] = True
    return out


# ============================================================ price history (the scout's own)
_RUN_INFO: dict = {}


def remember_price(st: dict, asin: str, price: float) -> dict:
    """Keeps 30 / 90 / 365-day lows per product. Returns what today's price means BEFORE it's recorded."""
    if asin in _RUN_INFO:          # the same product showing up in another search this run
        return _RUN_INFO[asin]
    _RUN_INFO[asin] = info = _remember(st, asin, price)
    return info


def _remember(st: dict, asin: str, price: float) -> dict:
    d = day_no()
    h = st["hist"].get(asin)
    if not h:
        st["hist"][asin] = [d, d, price, price, d, price, d, price, d, 1]
        return {"tracked": 0}
    first, last, last_p, l30, d30, l90, d90, l365, d365, n = h
    tracked = d - first
    info = {"tracked": tracked, "obs": n, "prev": last_p}
    for w, lo, ld in ((365, l365, d365), (90, l90, d90), (30, l30, d30)):
        if tracked >= w and d - ld <= w and price < lo - 0.005:
            info["low_window"] = w
            break
    if "low_window" not in info and 7 <= tracked and n >= 2 and price < min(l30, l90, l365) - 0.005:
        info["low_since_tracking"] = True
    new = [first, d, price]
    for lo, ld, w in ((l30, d30, 30), (l90, d90, 90), (l365, d365, 365)):
        if d - ld > w or price <= lo:   # expired low (rolls to today) or a new low
            lo, ld = price, d
        new += [lo, ld]
    st["hist"][asin] = new + [n + 1]
    return info


# ============================================================ deals, demand, ranking
def effective_price(it: dict) -> float:
    p = it["price"]
    if A.APPLY_COUPONS:
        if it.get("coupon_pct"):
            p = p * (1 - it["coupon_pct"] / 100)
        elif it.get("coupon_amt") and it["coupon_amt"] < p:
            p = p - it["coupon_amt"]
    return round(p, 2)


def pct_off(it: dict) -> float:
    """% below Amazon's typical price, or below the list price. Sellers can type in any "List" price, so a list-price
    discount bigger than MAX_LIST_PCT_OFF is treated as fake and ignored ("87% off $123" lip gloss)."""
    t = it.get("typical")
    if not t or t <= it["price"]:
        return 0.0
    pct = round((1 - it["price"] / t) * 100, 1)
    if it.get("typical_kind", "list") != "typical" and pct > getattr(A, "MAX_LIST_PCT_OFF", 60):
        return 0.0
    return pct


def deal_lines(it: dict) -> list[str]:
    out = []
    po = pct_off(it)
    if po >= A.MIN_PCT_OFF:
        out.append(f"**{po:.0f}% off** {it.get('typical_kind', 'list')} price ({money(it['typical'])})")
    hi = it.get("hist_info") or {}
    if hi.get("low_window") == 365:
        out.append("**Lowest price in a year** (scout's price history)")
    elif hi.get("low_window"):
        out.append(f"**Lowest price in {hi['low_window']} days** (scout's price history)")
    elif hi.get("low_since_tracking"):
        out.append(f"Lowest price the scout has seen (tracking {hi['tracked']} days)")
    if it.get("low30"):
        out.append("Amazon: **lowest price in 30 days**")
    if it.get("coupon_pct"):
        out.append(f"**Coupon: {it['coupon_pct']}% off** (clip it on Amazon)")
    elif it.get("coupon_amt"):
        out.append(f"**Coupon: {money(it['coupon_amt'])} off** (clip it on Amazon)")
    if it.get("badge"):
        out.append(f"**{it['badge']}**")
    if hi.get("prev") and it["price"] < hi["prev"] * 0.9:
        out.append(f"Price dropped from {money(hi['prev'])}")
    return out


def demand_line(it: dict) -> str:
    bits = []
    if it.get("bought"):
        b = it["bought"]
        bits.append(f"**{b // 1000}K+ bought** in past month" if b >= 1000 else f"**{b}+ bought** in past month")
    if it.get("rating"):
        bits.append(f"{it['rating']}★" + (f" ({it['reviews']:,} ratings)" if it.get("reviews") else ""))
    if it.get("rank"):
        bits.append(f"#{it['rank']:,} in {it.get('rank_cat', 'its category')}")
    if it.get("tag"):
        bits.append(it["tag"])
    return " • ".join(bits) or "no demand info shown"


def is_big_deal(it: dict) -> bool:
    """Deals Amazon itself backs (typical-price drops, deal badges, coupons, 30-day lows) or the scout's own 90/365-day lows.
    A big drop from a seller's own "List" price only counts when Amazon also flags it."""
    hi = it.get("hist_info") or {}
    po, typical = pct_off(it), it.get("typical_kind") == "typical"
    return ((po >= A.BIG_PCT_OFF and (typical or it.get("badge") or it.get("low30")))
            or (it.get("coupon_pct") or 0) >= A.BIG_COUPON_PCT or bool(it.get("badge"))
            or (it.get("low30") and po >= 25) or hi.get("low_window") in (90, 365))


def score(it: dict) -> float:
    s = min(pct_off(it), 70)
    s += min(it.get("coupon_pct") or 0, 40) + (8 if it.get("coupon_amt") else 0)
    s += 15 if it.get("badge") else 0
    s += 10 if it.get("low30") else 0
    hi = it.get("hist_info") or {}
    s += {365: 30, 90: 20, 30: 10}.get(hi.get("low_window"), 6 if hi.get("low_since_tracking") else 0)
    s += 8 * math.log10(1 + (it.get("bought") or 0))          # 100 bought = +16, 1K = +24
    s += 3 * math.log10(1 + (it.get("reviews") or 0))
    s += 4 if it.get("tag") else 0
    s += 10 if it.get("branded") else 0                       # brand searches price on eBay far more often than no-name items
    return round(s, 1)


# ============================================================ brand lock (knockoff filter)
def norm(t: str) -> str:
    return re.sub(r"['’.]", "", str(t).lower()).replace("-", " ")


def brand_lock(keyword: str) -> list[str]:
    """Brand searches start with capital letters ("Chicago White Sox blanket", "LEGO City police"); generic ones don't
    ("air fryer liners"). Every leading capitalised word (up to 3) must appear in the listing, so a Chicago BEARS blanket
    doesn't pass for a White Sox search and plain Hot Wheels cars don't pass for "Hot Wheels Pixar Cars"."""
    lock = []
    for w in keyword.split():
        if not w[:1].isupper() or len(lock) == 3:
            break
        lock.append(norm(w))
    return lock


def passes_lock(it: dict, lock) -> bool:
    if not lock:
        return True
    if isinstance(lock, str):
        lock = [norm(w) for w in lock.split()]
    t = norm(f"{it.get('brand', '')} {it['title']}")
    return all(has_word(t, w) for w in lock)


PET_WORDS = {"dog", "dogs", "cat", "cats", "pet", "pets", "puppy", "kitten"}
APPAREL_WORDS = {"hoodie", "shirt", "hat", "blanket", "jersey", "sweater", "jacket", "costume", "pajamas", "socks", "backpack", "beanie", "collar"}
CARD_WORDS = ["topps", "panini prizm", "panini select", "donruss", "upper deck", "hobby box", "blaster box", "trading card", "trading cards", "card pack", "booster box", "play booster", "magic the gathering"]


def is_junk(it: dict, keyword: str = "") -> bool:
    t = it["title"].lower()
    if any((has_word(t, w) if " " not in w else w in t) for w in A.SKIP_WORDS + CARD_WORDS):
        return True
    if norm(t).startswith(("replacement", "compatible", "for ")):
        return True   # third-party parts and accessories made "for" the brand
    kw = set(D.words(keyword))
    if kw & APPAREL_WORDS and not (kw & PET_WORDS) and set(D.words(t)) & PET_WORDS:
        return True   # a dog hoodie for a "Houston Texans hoodie" search
    return False


# ============================================================ eBay matching and money
STOP = set("""a an and or the of for with to in on by from at as is it this that your you our 2023 2024 2025 2026 2027 kids kid boys girls adults adult
men mens women womens toddlers toddler baby ages age years year old gift gifts idea ideas birthday christmas holiday perfect great best premium upgraded
original official authentic genuine quality durable portable lightweight heavy duty easy use home office travel outdoor indoor""".split())
BAD_EBAY = ["lot", "lots", "used", "broken", "parts", "for parts", "empty box", "box only", "no box", "read", "damaged", "incomplete", "custom", "replica",
            "fake", "manual only", "instructions only", "pre-owned", "preowned", "open box", "missing", "bundle"]


def identifiers(text: str) -> list[str]:
    """Model / set numbers: things like 60316, DW5600, WH-1000XM5 (must match on eBay)."""
    out = []
    for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]{2,}", text):
        t = tok.strip("-")
        has_d, has_a = any(ch.isdigit() for ch in t), any(ch.isalpha() for ch in t)
        if (has_d and has_a and len(t) >= 4 and not re.fullmatch(r"\d+(pcs|pc|pack|pk|ct|oz|ml|in|inch|ft|mm|cm|lb|lbs|w|v|mah|gb|tb|x)", t.lower())) or re.fullmatch(r"\d{4,6}", t):
            if not re.fullmatch(r"(19|20)\d\d", t) and t.lower() not in out:
                out.append(t.lower())
    return out[:3]


def pack_count(text: str) -> int | None:
    m = re.search(r"(\d{1,3})\s*[- ]?(?:pack|pk|count|ct|pcs|pieces|piece set)\b|(?:pack|set) of (\d{1,3})\b", text, re.I)
    return int(m.group(1) or m.group(2)) if m else None


def comp_query(it: dict) -> str:
    brand = it.get("brand") or ""
    core = re.split(r"[,|(\[]| - | – ", it["title"])[0]
    ids = identifiers(it["title"])
    ws, seen = [], set(w.lower() for w in brand.split())
    for w in re.findall(r"[A-Za-z0-9'&+.-]+", core):
        lw = w.lower().strip(".-")
        if not lw or lw in STOP or lw in seen or lw in ids:
            continue
        seen.add(lw)
        ws.append(w.strip(".-"))
    q = " ".join([brand] + ids[:2] + ws[:6 - min(2, len(ids))]).strip()
    return re.sub(r"\s+", " ", q)[:80]


def ebay_match(it: dict, title: str, query: str) -> bool:
    t = title.lower().replace("-", " ")
    if any((has_word(t, b) if " " not in b else b in t) for b in BAD_EBAY):
        return False
    brand = (it.get("brand") or "").lower().replace("-", " ").strip()
    if brand and not has_word(t, brand.split()[0]):
        return False
    tight = t.replace(" ", "")
    for i in identifiers(it["title"])[:2]:
        if i.replace("-", "") not in tight:
            return False
    a, b = pack_count(it["title"]), pack_count(title)
    if a != b and (a or 1) != (b or 1):
        return False
    qw = [w for w in D.words(query) if w not in STOP]
    if not qw:
        return True
    hit = sum(1 for w in qw if has_word(t, w))
    return hit / len(qw) >= 0.6


def ebay_costs(list_price: float) -> dict:
    base = list_price * (1 + A.EBAY_BUYER_TAX_PCT / 100)
    fvf, ads = base * A.EBAY_FEE_PCT / 100, base * A.EBAY_AD_RATE_PCT / 100
    order = A.EBAY_ORDER_FEE if list_price > 10 else A.EBAY_ORDER_FEE_SMALL
    total = fvf + ads + order + A.SHIP_TO_EBAY_BUYER + A.PACKAGING_COST
    return {k: round(v, 2) for k, v in {"fvf": fvf, "ads": ads, "order": order, "post": A.SHIP_TO_EBAY_BUYER, "pack": A.PACKAGING_COST, "total": total}.items()}


def evaluate(it: dict, comps: dict, query: str) -> dict:
    """Returns the money picture. ev['ok'] is False when eBay couldn't be priced reliably."""
    eff = effective_price(it)
    cost = round(eff * (1 + A.AMAZON_TAX_PCT / 100), 2)
    res = {"eff": eff, "cost": cost, "tax": round(cost - eff, 2), "ok": False}
    for basis in ("sold", "active"):
        m = [c for c in comps.get(basis, []) if ebay_match(it, c["title"], query)]
        if len(m) < A.MIN_COMPS:
            continue
        totals = sorted(c["total"] for c in m)
        med = percentile(totals, 0.5)
        kept = [x for x in totals if med * 0.45 <= x <= med * 2.5]
        if len(kept) < A.MIN_COMPS:
            continue
        est = percentile(kept, A.PRICE_PERCENTILE)
        if basis == "active":
            est *= 1 - A.ACTIVE_PRICE_HAIRCUT_PCT / 100
        L = max(0.99, math.floor(est) - 0.01 if est >= 10 else round(est, 2))   # e.g. 24.60 -> 23.99
        e = ebay_costs(L)
        profit = round(L - e["total"] - cost, 2)
        spread = percentile(kept, 0.75) / max(0.01, percentile(kept, 0.25))
        if basis == "sold":
            conf = "High" if len(kept) >= 5 and spread <= 1.6 else "Medium"
        else:   # eBay hides sold prices from the scout, so many active listings at similar prices is the next best thing
            conf = "Medium" if len(kept) >= 8 and spread <= 1.6 else "Low"
        res.update(ok=True, basis=basis, n=len(kept), low=kept[0], median=round(percentile(kept, 0.5), 2), list=L, ebay=e,
                   profit=profit, roi=round(profit / cost * 100, 1) if cost else 0, conf=conf,
                   examples=sorted(m, key=lambda c: c["total"])[:3])
        return res
    res["n_sold"], res["n_active"] = len(comps.get("sold", [])), len(comps.get("active", []))
    return res


def tier(it: dict, ev: dict) -> str | None:
    if ev["ok"]:
        if ev["profit"] >= A.MIN_PROFIT and ev["roi"] >= A.MIN_ROI_PCT and ev["conf"] != "Low":
            return "BUY"
        if ev["profit"] >= A.MAYBE_MIN_PROFIT:
            return "MAYBE"
        return None
    if A.SEND_DEAL_WATCH and it.get("branded") and is_big_deal(it) and (it.get("bought") or 0) >= A.HIGH_DEMAND_BOUGHT:
        return "DEAL"   # only from brand searches: no-name items with big "% off" are rarely real deals
    if getattr(A, "SEND_UNPRICED", True):
        return "LIST"   # eBay couldn't price it: send it anyway with the price you'd need to list at
    return None


def price_needed(cost: float, profit: float) -> float:
    """The eBay price that leaves `profit` after every fee (solves price - fees(price) - cost = profit)."""
    rate = (1 + A.EBAY_BUYER_TAX_PCT / 100) * (A.EBAY_FEE_PCT + A.EBAY_AD_RATE_PCT) / 100
    fixed = A.SHIP_TO_EBAY_BUYER + A.PACKAGING_COST
    L = (cost + profit + fixed + A.EBAY_ORDER_FEE) / (1 - rate)
    if L <= 10:
        L = (cost + profit + fixed + A.EBAY_ORDER_FEE_SMALL) / (1 - rate)
    return round(L, 2)


# ============================================================ Discord
TIER_STYLE = {"BUY": ("🟢 BUY", 0x22C55E), "MAYBE": ("🟡 MAYBE", 0xEAB308), "DEAL": ("🔥 DEAL: check eBay yourself", 0xF97316),
              "LIST": ("📋 TO LIST: no eBay price found", 0x64748B)}


def alert(it: dict, ev: dict, kind: str, keyword: str, query: str) -> bool:
    label, color = TIER_STYLE[kind]
    sold_url = "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode({"_nkw": query, "LH_Sold": "1", "LH_Complete": "1", "LH_ItemCondition": "1000"})
    active_url = "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode({"_nkw": query, "LH_ItemCondition": "1000", "LH_BIN": "1"})
    deals = deal_lines(it)
    amz = f"**{money(ev['cost'])}** total\n{money(it['price'])}"
    if ev["eff"] < it["price"]:
        amz += f" − coupon {money(it['price'] - ev['eff'])}"
    amz += f" + tax {money(ev['tax'])}"
    fields = [{"name": "🏷️ Deal", "value": "\n".join("• " + d for d in deals) if deals else "No special deal right now (regular price)", "inline": False},
              {"name": "🛒 You pay Amazon", "value": amz, "inline": True}]
    if ev["ok"]:
        e = ev["ebay"]
        fields += [
            {"name": "💲 List on eBay at", "value": f"**{money(ev['list'])}**", "inline": True},
            {"name": "💰 Profit", "value": f"**{money(ev['profit'])}**\n{ev['roi']:.0f}% ROI", "inline": True},
            {"name": "🧾 eBay costs", "value": f"fee {money(e['fvf'])} + ads {money(e['ads'])} + {money(e['order'])}" + (f" + postage {money(e['post'])}" if e["post"] else "") + f" = **{money(e['total'])}**", "inline": False},
            {"name": f"{'🟢' if ev['conf'] == 'High' else '🟡'} eBay prices", "value": f"**{ev['conf']}** confidence • {ev['n']} matching {ev['basis']} listings • low {money(ev['low'])}, median {money(ev['median'])}"
             + ("\n(no sold data: based on active listings, check sold prices before buying)" if ev["basis"] == "active" else ""), "inline": False},
            {"name": "📦 Cheapest eBay matches", "value": ("\n".join(f"• [{money(x['total'])}]({x['url']}) {x['title'][:60]}" for x in ev["examples"] if x.get("url")) or "—")[:1000], "inline": False},
        ]
    else:
        why = "Big deal + high demand, so it's worth checking." if kind == "DEAL" else "Check what it sells for before you list it."
        fields.append({"name": "❓ eBay", "value": f"Couldn't find enough matching eBay listings to price this. {why}", "inline": False})
        fields.append({"name": "💲 List on eBay at", "value": f"**{money(price_needed(ev['cost'], A.MIN_PROFIT))}+** for {money(A.MIN_PROFIT)} profit\n"
                                                             f"break-even {money(price_needed(ev['cost'], 0))}", "inline": True})
    fields += [
        {"name": "📈 Demand", "value": demand_line(it), "inline": False},
        {"name": "Links", "value": f"[Buy on Amazon]({it['url']})  •  [eBay sold]({sold_url})  •  [eBay active]({active_url})  •  "
                                   f"[Price history](https://camelcamelcamel.com/product/{it['asin']})", "inline": False},
    ]
    title = f"{label}: {it.get('brand') + ' ' if it.get('brand') and not it['title'].lower().startswith(it['brand'].lower()) else ''}{it['title']}"
    main = {"title": title[:250], "url": it["url"], "color": color, "fields": fields,
            "footer": {"text": f"Search: {keyword} • ASIN {it['asin']}" + (" • Prime" if it.get("prime") else "")}}
    if it.get("photos"):
        main["image"] = {"url": it["photos"][0]}
    plain = f"{label} | {it['title'][:120]} | Amazon {money(ev['cost'])}" + (f" -> eBay {money(ev['list'])} | profit {money(ev['profit'])}" if ev["ok"] else "") + f" | {it['url']}"
    return D.discord({"embeds": [main] + D.gallery(it["url"], it.get("photos") or [], color), "_plain": plain})


# ============================================================ fetching
class AmazonNet:
    """Plain requests first; if Amazon shows its robot check, the stealth browser; if that fails too, give up on that page."""

    def __init__(self, net: D.Net):
        self.net = net
        self.plain_refused = 0
        self.blocked = 0

    def page(self, url: str, wait_selector: str, debug_name: str) -> str:
        if self.plain_refused < 3:
            r = self.net.get(url)
            html, status = D.page_text(r), getattr(r, "status", 0) if r is not None else 0
            D.debug_save(f"{debug_name}_plain.html", f"<!-- HTTP {status} -->\n" + html)
            if not amazon_blocked(html, status):
                self.plain_refused = 0
                return html
            self.plain_refused += 1
            if self.plain_refused == 3:
                log("  (Amazon refuses plain requests from here: using the browser from now on)")
        page, _ = self.net.browser(url, wait_selector=wait_selector)
        html = D.page_text(page)
        D.debug_save(f"{debug_name}_browser.html", html)
        if amazon_blocked(html, 200):
            self.blocked += 1
            return ""
        return html


def search_url(kw: str) -> str:
    lo, hi = int(A.MIN_AMAZON_PRICE), int(math.ceil(A.MAX_AMAZON_PRICE))
    return f"{AMAZON}/s?" + urllib.parse.urlencode({"k": kw, "rh": f"p_36:{lo * 100}-{hi * 100}", "low-price": lo, "high-price": hi})


# ============================================================ main run
def run() -> dict:
    st = load_state()
    _RUN_INFO.clear()
    kws = [l.strip() for l in KEYWORDS_FILE.read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
    rot, block, start, batch, days = todays_batch(kws, st)
    net = D.Net()
    amz = AmazonNet(net)
    stats = {"keywords": len(batch), "listings": 0, "deals": 0, "checked": 0, "buy": 0, "maybe": 0, "deal_watch": 0, "to_list": 0, "no_comps": 0, "not_profitable": 0, "unavailable": 0}
    t0, budget = now(), A.RUN_MINUTES * 60
    mins = lambda: (now() - t0) / 60  # noqa: E731
    pending = []
    log(f"Day {rot['day'] + 1} of {days}: searching Amazon for keywords {start + 1}-{start + len(batch)} of today's {len(block)}")
    try:
        for n, kw in enumerate(batch):
            if now() - t0 > budget * A.SEARCH_SHARE_OF_TIME:
                stats["keywords"], rot["pos"] = n, start + n
                log(f"Search time used up after {n} keywords ({mins():.0f} min); the rest are searched next run.")
                break
            if n >= A.STOP_AFTER_EMPTY_SEARCHES and stats["listings"] == 0:
                stats["stopped_early"], stats["keywords"], rot["pos"] = n, n, start
                log(f"Amazon returned nothing for the first {n} searches, so this run stops here.")
                break
            html = amz.page(search_url(kw), 'div[data-component-type="s-search-result"]', "amazon_search")
            items = parse_search(html) if html else []
            stats["listings"] += len(items)
            lock, picks = brand_lock(kw), []
            for it in items:
                if not (A.MIN_AMAZON_PRICE <= it["price"] <= A.MAX_AMAZON_PRICE):
                    continue
                it["hist_info"] = remember_price(st, it["asin"], it["price"])
                if (A.SKIP_SPONSORED and it["sponsored"]) or is_junk(it, kw) or not passes_lock(it, lock) or it.get("unavailable"):
                    continue
                if it.get("rating") and it["rating"] < A.MIN_RATING:
                    continue
                seen = st["seen"].get(it["asin"])
                if seen:
                    window = A.REMEMBER_ALERTED_DAYS if seen["v"] == "alert" else A.REMEMBER_CHECKED_DAYS
                    if now() - seen["t"] < window * DAY and not (seen.get("p") and it["price"] <= seen["p"] * 0.95):
                        continue
                if deal_lines(it):
                    stats["deals"] += 1
                it["keyword"], it["branded"] = kw, bool(lock)
                it["score"] = score(it)
                picks.append(it)
            picks.sort(key=lambda x: -x["score"])
            for rank, it in enumerate(picks[:A.CANDIDATES_PER_KEYWORD]):
                pending.append((rank, -it["score"], it))
            log(f"  {kw}: {len(items)} listings, {len(picks)} worth a look" + (f", best: {picks[0]['title'][:40]} (score {picks[0]['score']})" if picks else ""))

        save_state(st)
        # the strongest candidate of every search first, then the second of each, and so on
        pending.sort(key=lambda x: (x[0] > 0, x[1]))
        todo = pending[:A.MAX_EBAY_CHECKS_PER_RUN]
        log(f"Checking {len(todo)} of {len(pending)} candidates ({mins():.0f} min so far)")
        sent = deal_sent = 0
        for i, (_, _, it) in enumerate(todo):
            if now() - t0 > budget:
                stats["out_of_time"] = len(todo) - i
                log(f"Time limit reached; {len(todo) - i} candidates left.")
                break
            if i and i % 5 == 0:
                save_state(st)
            html = amz.page(it["url"], "#productTitle", "amazon_product")
            if html:
                info = parse_product(html)
                for k, v in info.items():
                    if k == "photos" and len(v) <= len(it.get("photos") or []):
                        continue
                    if k == "price" and abs(v - it["price"]) > it["price"] * 0.5:
                        continue   # a different variant's price; keep the search price
                    it[k] = v
            if it.get("unavailable") or not (A.MIN_AMAZON_PRICE <= it["price"] <= A.MAX_AMAZON_PRICE):
                stats["unavailable"] += 1
                st["seen"][it["asin"]] = {"v": "fail", "t": now(), "p": it["price"]}
                continue
            q = comp_query(it)
            comps = D.ebay_comps(net, st, q, "new")
            stats["checked"] += 1
            ev = evaluate(it, comps, q)
            kind = tier(it, ev)
            if ev["ok"]:
                log(f"  {it['title'][:50]}: Amazon {money(ev['cost'])} -> eBay {money(ev['list'])} = {money(ev['profit'])} ({ev['basis']}, {ev['n']}) {kind or ''}")
            else:
                log(f"  {it['title'][:50]}: not enough eBay matches for '{q}' ({ev['n_sold']} sold, {ev['n_active']} active read)" + (" -> deal watch" if kind else ""))
            if kind == "DEAL" and deal_sent >= A.MAX_DEAL_WATCH_PER_RUN:
                kind = "LIST" if getattr(A, "SEND_UNPRICED", True) else None
            if kind and sent < A.MAX_ALERTS_PER_RUN and alert(it, ev, kind, it["keyword"], q):
                sent += 1
                deal_sent += kind == "DEAL"
                stats[{"BUY": "buy", "MAYBE": "maybe", "DEAL": "deal_watch", "LIST": "to_list"}[kind]] += 1
                st["seen"][it["asin"]] = {"v": "alert", "t": now(), "p": it["price"]}
                continue
            stats["no_comps" if not ev["ok"] else "not_profitable"] += 1
            st["seen"][it["asin"]] = {"v": "fail", "t": now(), "p": it["price"]}
    finally:
        net.close()
        stats["minutes"], stats["requests"], stats["blocked"] = round(mins(), 1), net.requests, amz.blocked
        save_state(st)

    lines = [f"🛒 **Amazon scout**: {stats['keywords']} searches, {stats['listings']} listings, {stats['deals']} with a deal, {stats['checked']} checked on eBay → "
             f"**{stats['buy']} BUY**, {stats['maybe']} maybe, {stats['deal_watch']} deal watch, {stats['to_list']} to list (no eBay price).",
             f"Not enough eBay matches: {stats['no_comps']} • below target: {stats['not_profitable']} • unavailable: {stats['unavailable']}. "
             + D.rotation_note(rot, block, days).replace("keyword #", "Amazon keyword #")]
    if stats.get("stopped_early"):
        lines.append(f"⚠️ Amazon returned nothing for the first {stats['stopped_early']} searches, so this run stopped early. Amazon is likely blocking GitHub's servers. "
                     "Download **debug-amazon** from this run's Summary page on GitHub and send it to Claude.")
    elif stats["blocked"] > 5:
        lines.append(f"⚠️ Amazon showed its robot check {stats['blocked']} times this run, so some searches came back empty.")
    D.discord({"content": "\n".join(lines)})
    log(json.dumps(stats))
    return stats


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        D.discord({"content": f"⚠️ Amazon scout crashed: {type(e).__name__}: {str(e)[:300]}"})
        raise

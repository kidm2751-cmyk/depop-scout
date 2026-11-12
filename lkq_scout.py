"""Auto Parts Scout: LKQ Online -> eBay.

Finds used OEM parts on LKQ Online (the biggest US auto recycler, ships nationwide) that sell for at least
$100 more on eBay after every cost, and posts them to Discord.

Each run (4 a day on GitHub Actions):
  1. searches LKQ Online for 150 vehicle x year x part combinations from lkq_searches.txt (about 11,000 in all,
     so the full list is covered about every 19 days): headlights, tail lights, bumpers, hoods, doors, modules,
     radios, clusters, wheels, mirrors, AC compressors, engines, transmissions and more,
  2. reads every listing: price, core charge, shipping, mileage, donor vehicle, fitment years, yard, photos,
  3. prices each part type on eBay (used listings for the same vehicle, years, part and side),
  4. works out profit after LKQ price + core + shipping + 9.75% tax, eBay fees + ads, and postage to your buyer
     by size: parcel, oversize, or FREIGHT for bumpers, hoods, doors, engines and transmissions,
  5. sends parts that clear $100 profit (and a few $60-$100 "close calls") to your auto parts Discord channel.
Settings: lkq_settings.py. Shared network / eBay / Discord code: scout.py.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from pathlib import Path

import scout as D
import lkq_settings as K

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state_lkq" / "state.json"
SEARCHES_FILE = ROOT / "lkq_searches.txt"
D.WEBHOOK = (os.environ.get("AUTO_PARTS_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
D.DEBUG_DIR = ROOT / "debug_lkq"
LKQ = "https://lkqonline.com"
DAY = 86400
log, money, now, percentile, has_word = D.log, D.money, D.now, D.percentile, D.has_word


# ============================================================ state
def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text())
    except Exception:
        st = {}
    st.setdefault("seen", {})
    st.setdefault("comps", {})
    st.setdefault("cursor", 0)
    return st


def save_state(st: dict) -> None:
    t = now()
    st["seen"] = {k: v for k, v in st["seen"].items() if t - v.get("t", 0) < 30 * DAY}
    st["comps"] = {k: v for k, v in st["comps"].items() if t - v.get("t", 0) < D.S.EBAY_COMP_CACHE_HOURS * 3600}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, separators=(",", ":")))


def read_searches(path: Path = None) -> list[dict]:
    out = []
    for line in (path or SEARCHES_FILE).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 4 and parts[0].isdigit():
            out.append({"year": int(parts[0]), "make": parts[1], "model": parts[2], "part": parts[3]})
    return out


# ============================================================ parts knowledge
# part -> (eBay search words, words an eBay title must contain one of, size class)
PART_INFO = {
    "Headlight Assembly": ("(headlight,headlamp)", ["headlight", "head light", "headlamp", "head lamp"], "medium"),
    "Tail Light Assembly": ("(taillight,tail)", ["tail light", "taillight", "tail lamp", "taillamp"], "medium"),
    "Front Bumper Assembly": ("front bumper", ["front bumper"], "freight"),
    "Rear Bumper Assembly": ("rear bumper", ["rear bumper"], "freight"),
    "Front Bumper Cover": ("front bumper", ["front bumper"], "freight"),
    "Rear Bumper Cover": ("rear bumper", ["rear bumper"], "freight"),
    "Hood": ("hood", ["hood"], "freight"),
    "Fender": ("fender", ["fender"], "large"),
    "Grille": ("grille", ["grille", "grill"], "large"),
    "Front Door": ("front door", ["front door", "door shell"], "freight"),
    "Rear Door": ("rear door", ["rear door", "door shell"], "freight"),
    "Tailgate": ("tailgate", ["tailgate", "tail gate"], "freight"),
    "Liftgate": ("liftgate", ["liftgate", "lift gate", "hatch"], "freight"),
    "Trunk Lid": ("trunk lid", ["trunk lid", "decklid", "deck lid"], "freight"),
    "Side View Mirror": ("mirror", ["mirror"], "medium"),
    "Fog Light": ("fog", ["fog light", "fog lamp", "foglight"], "small"),
    "Radio": ("(radio,receiver,stereo)", ["radio", "receiver", "stereo", "head unit"], "small"),
    "Navigation Display": ("(display,screen,navigation)", ["display", "screen", "navigation", "monitor"], "small"),
    "Instrument Cluster": ("(cluster,speedometer)", ["cluster", "speedometer", "gauge"], "small"),
    "Engine Control Module": ("(ecm,ecu,pcm)", ["ecm", "ecu", "engine control", "engine computer", "pcm"], "small"),
    "Transmission Control Module": ("(tcm,transmission control)", ["tcm", "transmission control", "transmission computer"], "small"),
    "Body Control Module": ("(bcm,body control)", ["bcm", "body control"], "small"),
    "Anti Lock Brake Pump": ("abs", ["abs", "anti lock", "antilock"], "medium"),
    "Climate Control": ("(climate,temperature) control", ["climate", "ac heater control", "a/c heater", "temperature control"], "small"),
    "Backup Camera": ("backup camera", ["camera"], "small"),
    "Cruise Control Radar Sensor": ("(radar,distance) sensor", ["radar", "distance sensor", "cruise control sensor", "acc sensor"], "small"),
    "Alternator": ("alternator", ["alternator"], "medium"),
    "Starter Motor": ("starter motor", ["starter"], "medium"),
    "AC Compressor": ("compressor", ["compressor"], "medium"),
    "Radiator": ("radiator", ["radiator"], "large"),
    "Steering Gear Rack": ("(rack,steering gear)", ["rack", "steering gear"], "large"),
    "Power Steering Pump": ("power steering pump", ["power steering pump", "steering pump"], "medium"),
    "Turbocharger": ("turbo", ["turbo"], "medium"),
    "Front Seat": ("front seat", ["seat"], "freight"),
    "Wheel": ("(wheel,rim)", ["wheel", "rim"], "large"),
    "Steering Wheel": ("steering wheel", ["steering wheel"], "medium"),
    "Center Console": ("center console", ["console"], "large"),
    "Window Regulator": ("window regulator", ["regulator"], "medium"),
    "Front Axle Shaft": ("(axle,cv shaft)", ["axle", "cv shaft", "half shaft", "halfshaft"], "large"),
    "Rear Differential": ("(differential,carrier)", ["differential", "carrier", "rear axle"], "freight"),
    "Transfer Case": ("transfer case", ["transfer case"], "freight"),
    "Engine Assembly": ("(engine,motor)", ["engine", "motor"], "freight"),
    "Transmission Assembly": ("transmission", ["transmission"], "freight"),
    "Fuel Injection Pump": ("fuel pump", ["fuel pump", "injection pump", "fuel injection pump"], "medium"),
    "Throttle Body": ("throttle body", ["throttle body"], "small"),
    "Intake Manifold": ("intake manifold", ["intake manifold", "intake"], "medium"),
}
DEFAULT_PART = ("", [], "large")
MAKE_SYNONYMS = {"chevrolet": ["chevrolet", "chevy"], "mercedes-benz": ["mercedes", "benz"], "volkswagen": ["volkswagen", "vw"],
                 "ram": ["ram"], "gmc": ["gmc"], "lexus": ["lexus"], "toyota": ["toyota"]}
GENERIC_MODEL_WORDS = {"super", "duty", "hd", "pickup", "series"}
BAD_EBAY = ["aftermarket", "replacement", "tyc", "depo", "spec-d", "anzo", "lot of", "lot", "pair", "set of", "parts only", "for parts",
            "broken", "cracked", "damaged", "bracket only", "housing only", "lens only", "cover only", "bulb", "harness", "wire", "pigtail",
            "trim only", "emblem", "clip", "gasket", "bolt", "screw", "repair kit", "decal", "sticker", "manual", "core", "tab repair",
            "for repair", "as is", "not working", "untested", "rebuilt kit", "seal kit", "mount only", "bezel"]


def part_info(part: str) -> tuple:
    return PART_INFO.get(part, (part.lower(), [w for w in part.lower().split() if w not in ("assembly",)][:1], "large"))


def tight(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(t).lower())


def model_key(model: str) -> str:
    return tight(" ".join(w for w in re.split(r"[\s]+", model) if w.lower() not in GENERIC_MODEL_WORDS))


def side_of(text: str) -> str | None:
    t = f" {str(text).lower()} "
    left = re.search(r"\b(driver|left|lh|l/h)\b", t)
    right = re.search(r"\b(passenger|right|rh|r/h)\b", t)
    if left and not right:
        return "L"
    if right and not left:
        return "R"
    return None


def years_in_title(title: str) -> set[int]:
    """Years an eBay title says the part fits: 2018, 2018-2020, 18-20, 2018 2019 2020."""
    t = str(title)
    ys = set()
    for a, b in re.findall(r"\b((?:19|20)\d\d)\s*[-–/]\s*((?:19|20)?\d\d)\b", t):
        a = int(a)
        b = int(b) if len(b) == 4 else (a // 100) * 100 + int(b)
        if a <= b <= a + 15:
            ys.update(range(a, b + 1))
    for a, b in re.findall(r"(?<![\d.$])\b(\d\d)\s*-\s*(\d\d)\b(?![\d.])", t):
        a, b = 2000 + int(a), 2000 + int(b)
        if 2000 <= a <= b <= a + 15 and b <= 2030:
            ys.update(range(a, b + 1))
    ys.update(int(y) for y in re.findall(r"\b((?:19|20)\d\d)\b", t))
    m = re.match(r"\s*((?:\d\d\s+){1,7}\d\d)\b", t)     # "18 19 20 Toyota Camry ..."
    if m:
        seq = [int(x) for x in m.group(1).split()]
        if all(b == a + 1 for a, b in zip(seq, seq[1:])) and seq[-1] <= 30:
            ys.update(2000 + x for x in seq)
    return {y for y in ys if 1980 <= y <= 2030}


# ============================================================ reading LKQ Online
def slug(text: str) -> str:
    return "-".join(w[:1].upper() + w[1:].lower() if not w.isdigit() else w for w in re.split(r"\s+", str(text).strip()))


def search_url(s: dict) -> str:
    return f"{LKQ}/{s['year']}-{slug(s['make'])}-{slug(s['model'])}-{slug(s['part'])}"


NAME_KEYS = ("partName", "partDescription", "productName", "displayName", "name", "title", "description")
PRICE_KEYS = ("price", "salePrice", "sellPrice", "finalPrice", "currentPrice", "unitPrice", "ourPrice", "yourPrice", "basePrice")
ID_KEYS = ("listingId", "inventoryId", "stockNumber", "stockNo", "sku", "partId", "id")


def num(v) -> float | None:
    """A dollar amount from a number, "$1,234.56" or {"amount": ...} (never treats whole numbers as cents)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, dict):
        for k in ("amount", "value", "price"):
            if k in v:
                return num(v[k])
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(v))
    return float(m.group(0).replace(",", "")) if m else None


def first(d: dict, keys, conv=None):
    low = {k.lower(): k for k in d}
    for k in keys:
        if k.lower() in low:
            v = d[low[k.lower()]]
            v = conv(v) if conv else v
            if v not in (None, "", [], {}):
                return v
    return None


def images_of(d: dict) -> list[str]:
    out = []
    for k in ("images", "imageUrls", "photos", "pictures", "imageUrl", "image", "thumbnail", "thumbnailUrl", "photoUrl"):
        for kk in d:
            if kk.lower() != k.lower():
                continue
            v = d[kk]
            vals = v if isinstance(v, list) else [v]
            for x in vals:
                u = x.get("url") or x.get("src") or x.get("large") if isinstance(x, dict) else x
                if isinstance(u, str) and u.startswith(("http", "//")):
                    u = "https:" + u if u.startswith("//") else u
                    if u not in out:
                        out.append(u)
    return out


def normalize(d: dict) -> dict | None:
    name = first(d, NAME_KEYS)
    price = first(d, PRICE_KEYS, num)
    if not isinstance(name, str) or not price or not (5 <= price <= 50000):
        return None
    desc = first(d, ("description", "partDescription", "notes", "comments", "conditionNotes")) or ""
    lid = first(d, ID_KEYS)
    url = first(d, ("url", "productUrl", "detailUrl", "link", "href", "seoUrl", "canonicalUrl")) or ""
    if isinstance(url, str) and url.startswith("/"):
        url = LKQ + url
    years = first(d, ("fitmentYears", "years", "fitsYears"))
    city = first(d, ("yardCity", "city", "locationCity"))
    state = first(d, ("yardState", "state", "locationState"))
    return {
        "id": str(lid or f"{name}|{price}")[:80], "name": name.strip()[:160], "description": str(desc).strip()[:400], "price": round(price, 2),
        "list_price": first(d, ("listPrice", "msrp", "originalPrice", "compareAtPrice"), num),
        "core": first(d, ("corePrice", "coreCharge", "core", "coreAmount"), num) or 0.0,
        "shipping": first(d, ("shipping", "shippingPrice", "shippingCost", "freight", "shippingCharge"), num),
        "env_fee": first(d, ("environmentalFee", "envFee", "environmentFee"), num) or 0.0,
        "mileage": first(d, ("mileage", "miles", "vehicleMileage", "donorMileage"), num),
        "photos": images_of(d), "url": url if isinstance(url, str) else "",
        "years": [int(y) for y in years if str(y).isdigit()] if isinstance(years, list) else [],
        "interchange": str(first(d, ("interchange", "interchangeNumber", "hollanderNumber")) or ""),
        "part_number": str(first(d, ("partNumber", "oemPartNumber", "oemNumber")) or ""),
        "stock": str(first(d, ("stockNumber", "stockNo", "tagNumber")) or ""),
        "grade": str(first(d, ("grade", "condition", "partGrade")) or ""),
        "location": ", ".join(str(x).title() if len(str(x)) > 2 else str(x) for x in (city, state) if x),
        "availability": str(first(d, ("availability", "status", "stockStatus")) or ""),
        "source_vehicle": " ".join(str(x) for x in (first(d, ("sourceYear", "vehicleYear")), first(d, ("sourceMake", "vehicleMake")),
                                                       first(d, ("sourceModel", "vehicleModel"))) if x),
    }


def listings_from_json(blobs: list) -> list[dict]:
    out, seen = [], set()
    for blob in blobs:
        for d in D.walk(blob):
            p = normalize(d)
            if p and p["id"] not in seen:
                seen.add(p["id"])
                out.append(p)
    return out


def listings_from_dom(html: str) -> list[dict]:
    """Last resort: product cards on the page (name, price, photo, link)."""
    if not html or "$" not in html:
        return []
    from scrapling.parser import Selector
    doc = Selector(html)
    out, seen = [], set()
    for c in doc.css('[class*="product"], [class*="Product"], [class*="listing"], [class*="Listing"], [class*="result-item"], [class*="part-card"], [class*="PartCard"]'):
        text = " ".join(t.strip() for t in c.css("::text").getall() if t.strip())
        prices = re.findall(r"\$\s?\d[\d,]*(?:\.\d{2})?", text)
        if not prices or len(prices) > 4:
            continue
        name = next((t.strip() for t in c.css("h1 ::text, h2 ::text, h3 ::text, h4 ::text, [class*='name'] ::text, [class*='title'] ::text").getall() if len(t.strip()) > 3), "")
        if not name:
            continue
        price = num(prices[0])
        key = (name, price)
        if key in seen or not price:
            continue
        seen.add(key)
        img = next((u for u in c.css("img::attr(src)").getall() if u.startswith(("http", "//"))), "")
        href = next((h for h in c.css("a::attr(href)").getall() if h and not h.startswith("#")), "")
        ship = re.search(r"(?:shipping|freight)[^$]{0,20}\$\s?(\d[\d,]*(?:\.\d{2})?)", text, re.I)
        core = re.search(r"core[^$]{0,20}\$\s?(\d[\d,]*(?:\.\d{2})?)", text, re.I)
        miles = re.search(r"([\d,]{3,7})\s*(?:miles|mi\b)", text, re.I)
        out.append({"id": f"{name}|{price}"[:80], "name": name[:160], "description": text[:400], "price": price, "list_price": None,
                    "core": num(core.group(1)) if core else 0.0, "shipping": 0.0 if re.search(r"free shipping", text, re.I) else (num(ship.group(1)) if ship else None),
                    "env_fee": 0.0, "mileage": num(miles.group(1)) if miles else None,
                    "photos": [("https:" + img) if img.startswith("//") else img] if img else [],
                    "url": (LKQ + href) if href.startswith("/") else href, "years": [], "interchange": "", "part_number": "", "stock": "",
                    "grade": "", "location": "", "availability": "", "source_vehicle": ""})
    return out


def lkq_search(net: D.Net, s: dict) -> list[dict]:
    url = search_url(s)
    r = net.get(url)
    status = getattr(r, "status", 0) if r is not None else 0
    html = D.page_text(r) if r is not None and not D.Net._blocked(r) else ""
    D.debug_save("lkq_search_plain.html", f"<!-- {url} HTTP {status} -->\n" + D.page_text(r))
    found = listings_from_json(D.json_blobs_from_html(html) + D.objects_with_slug(html)) if html else []
    if not found and html:
        found = listings_from_dom(html)
    if not found and status != 404:
        page, recorded = net.browser(url, record_pattern=r"lkq|api|search|catalog|inventory|graphql|part",
                                     wait_selector='[class*="price"], [class*="Price"]')
        bh = D.page_text(page)
        D.debug_save("lkq_search_browser.html", f"<!-- {url} -->\n" + bh)
        D.debug_save("lkq_browser_requests.txt", "\n".join(net.last_requests))
        found = listings_from_json(recorded) or listings_from_json(D.json_blobs_from_html(bh) + D.objects_with_slug(D.rsc_text(bh))) or listings_from_dom(bh)
    words = [w.replace("-", " ") for w in part_info(s["part"])[1]] + [s["part"].lower().split()[0]]
    found = [f for f in found if any(w in f"{f['name']} {f['description']}".lower().replace("-", " ") for w in words)]
    for f in found:
        f.setdefault("search", s)
        if not f["url"]:
            f["url"] = url
    return found


# ============================================================ eBay matching and money
def comp_query(s: dict, item: dict | None = None) -> str:
    """Make + model + the part, the way eBay titles say it. Year and side are left out of the search on purpose
    (titles say "2018-2020" and "LH/left/driver" in many ways) and checked on each result instead."""
    model = " ".join(w for w in s["model"].split() if w.lower() not in GENERIC_MODEL_WORDS)
    q = f"{s['make']} {model} {part_info(s['part'])[0]}"
    return re.sub(r"\s+", " ", q).strip()


def ebay_match(s: dict, item: dict, title: str) -> bool:
    t = str(title).lower()
    tt = tight(t)
    if any((has_word(t, b) if " " not in b and b.isalpha() else b in t) for b in BAD_EBAY):
        return False
    makes = MAKE_SYNONYMS.get(s["make"].lower(), [s["make"].lower()])
    if not any(has_word(t, m) for m in makes):
        return False
    if model_key(s["model"]) not in tt:
        return False
    must = part_info(s["part"])[1]
    if must and not any(m in t for m in must):
        return False
    side = side_of(f"{item['name']} {item['description']}")
    eside = side_of(t)
    if side and eside and side != eside:
        return False
    ys = years_in_title(title)
    want = set(item.get("years") or []) or {s["year"]}
    if ys and not (ys & want):
        return False
    return True


def size_class(s: dict) -> str:
    return part_info(s["part"])[2]


def lkq_cost(item: dict, s: dict) -> dict:
    size = size_class(s)
    ship_known = item.get("shipping") is not None
    ship = item["shipping"] if ship_known else K.LKQ_SHIPPING_ESTIMATE[size]
    core = (item.get("core") or 0.0) if K.COUNT_CORE_CHARGE else 0.0
    tax = (item["price"] + core) * K.BUY_TAX_PCT / 100
    total = item["price"] + core + ship + (item.get("env_fee") or 0) + tax
    return {"part": item["price"], "core": round(core, 2), "ship": round(ship, 2), "ship_known": ship_known, "env": round(item.get("env_fee") or 0, 2),
            "tax": round(tax, 2), "total": round(total, 2), "size": size}


def ebay_costs(list_price: float, size: str) -> dict:
    base = list_price * (1 + K.EBAY_BUYER_TAX_PCT / 100)
    fvf, ads = base * K.EBAY_FEE_PCT / 100, base * K.EBAY_AD_RATE_PCT / 100
    post, pack = K.SHIP_TO_BUYER[size], K.PACKAGING[size]
    total = fvf + ads + K.EBAY_ORDER_FEE + post + pack
    return {k: round(v, 2) for k, v in {"fvf": fvf, "ads": ads, "order": K.EBAY_ORDER_FEE, "post": post, "pack": pack, "total": total}.items()}


def evaluate(s: dict, item: dict, comps: dict) -> dict:
    cost = lkq_cost(item, s)
    res = {"cost": cost, "ok": False}
    for basis in ("sold", "active"):
        m = [c for c in comps.get(basis, []) if ebay_match(s, item, c["title"])]
        if len(m) < K.MIN_COMPS:
            continue
        totals = sorted(c["total"] for c in m)
        med = percentile(totals, 0.5)
        kept = [x for x in totals if med * 0.45 <= x <= med * 2.5]
        if len(kept) < K.MIN_COMPS:
            continue
        est = percentile(kept, K.PRICE_PERCENTILE)
        if basis == "active":
            est *= 1 - K.ACTIVE_PRICE_HAIRCUT_PCT / 100
        L = max(0.99, round(est) - 0.01)
        e = ebay_costs(L, cost["size"])
        profit = round(L - e["total"] - cost["total"], 2)
        res.update(ok=True, basis=basis, n=len(kept), low=kept[0], median=round(percentile(kept, 0.5), 2), list=L, ebay=e, profit=profit,
                   roi=round(profit / cost["total"] * 100, 1) if cost["total"] else 0,
                   examples=sorted(m, key=lambda c: c["total"])[:3])
        return res
    res["n_read"] = len(comps.get("sold", [])) + len(comps.get("active", []))
    return res


# ============================================================ Discord
SIZE_LABEL = {"small": "parcel", "medium": "parcel", "large": "oversize parcel", "freight": "FREIGHT"}


def alert(s: dict, item: dict, ev: dict, kind: str, query: str) -> bool:
    c, e = ev["cost"], ev["ebay"]
    color = 0x22C55E if kind == "BUY" else 0xEAB308
    label = "🟢 BUY" if kind == "BUY" else "🟡 CLOSE"
    vehicle = f"{s['year']} {s['make']} {s['model']}"
    side = {"L": " (driver side)", "R": " (passenger side)"}.get(side_of(f"{item['name']} {item['description']}"), "")
    active_url = "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode({"_nkw": query.replace(" OEM", ""), "LH_ItemCondition": "3000"})
    sold_url = "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode({"_nkw": query.replace(" OEM", ""), "LH_Sold": "1", "LH_Complete": "1"})
    info = []
    if item.get("mileage"):
        info.append(f"{int(item['mileage']):,} miles")
    if item.get("source_vehicle"):
        info.append(f"from a {item['source_vehicle']}")
    if item.get("years"):
        info.append(f"fits {min(item['years'])}-{max(item['years'])}")
    if item.get("grade"):
        info.append(f"grade/condition: {item['grade']}")
    if item.get("location"):
        info.append(f"yard: {item['location']}")
    for k, lab in (("interchange", "interchange"), ("part_number", "part #"), ("stock", "stock #")):
        if item.get(k):
            info.append(f"{lab} {item[k]}")
    ex = "\n".join(f"• [{money(x['total'])}]({x['url']}) {x['title'][:60]}" for x in ev["examples"] if x.get("url")) or "—"
    fields = [
        {"name": "🛒 You pay LKQ", "value": f"**{money(c['total'])}**\npart {money(c['part'])}" + (f" + core {money(c['core'])}" if c["core"] else "")
         + f"\n+ ship {money(c['ship'])}{'' if c['ship_known'] else ' (est.)'}" + (f" + fee {money(c['env'])}" if c["env"] else "") + f" + tax {money(c['tax'])}", "inline": True},
        {"name": "💲 List on eBay at", "value": f"**{money(ev['list'])}**\nfree shipping to buyer", "inline": True},
        {"name": "💰 Profit", "value": f"**{money(ev['profit'])}**\n{ev['roi']:.0f}% ROI", "inline": True},
        {"name": "🧾 eBay costs", "value": f"fee {money(e['fvf'])} + ads {money(e['ads'])} + {money(e['order'])}\n"
         f"postage {money(e['post'])} ({SIZE_LABEL[c['size']]}) + packing {money(e['pack'])} = **{money(e['total'])}**", "inline": False},
        {"name": "🔧 Part", "value": (item["name"] + (f"\n{item['description'][:200]}" if item.get("description") and item["description"] != item["name"] else "")
                                     + ("\n" + " • ".join(info) if info else ""))[:1000], "inline": False},
        {"name": "📦 eBay prices", "value": f"{ev['n']} matching used {ev['basis']} listings • low {money(ev['low'])}, median {money(ev['median'])}"
         + ("\n(eBay hides sold prices: based on current listings, lowered 10%)" if ev["basis"] == "active" else "") + f"\n{ex}"[:900], "inline": False},
    ]
    checks = []
    if not c["ship_known"]:
        checks.append(f"LKQ shipping not shown: estimated {money(c['ship'])} ({SIZE_LABEL[c['size']]})")
    if c["size"] == "freight":
        checks.append("freight item: get a real freight quote before listing (estimate used: " + money(e["post"]) + ")")
    checks.append("confirm the exact fitment / part number and the side before buying")
    fields.append({"name": "❓ Check before buying", "value": "\n".join("• " + x for x in checks), "inline": False})
    fields.append({"name": "Links", "value": f"[Buy on LKQ Online]({item['url']})  •  [eBay used listings]({active_url})  •  [eBay sold]({sold_url})", "inline": False})
    title = f"{label} {money(ev['profit'])} profit: {vehicle} {s['part']}{side}"
    main = {"title": title[:250], "url": item["url"], "color": color, "fields": fields, "footer": {"text": f"LKQ Online • {item['name'][:80]}"}}
    if item.get("photos"):
        main["image"] = {"url": item["photos"][0]}
    plain = f"{label} | {vehicle} {s['part']}{side} | LKQ {money(c['total'])} -> eBay {money(ev['list'])} | profit {money(ev['profit'])} | {item['url']}"
    return D.discord({"embeds": [main] + D.gallery(item["url"], item.get("photos") or [], color), "_plain": plain})


# ============================================================ main run
def run() -> dict:
    st = load_state()
    searches = read_searches()
    start = st["cursor"] % len(searches)
    batch = (searches + searches)[start:start + min(K.SEARCHES_PER_RUN, len(searches))]
    st["cursor"] = (start + len(batch)) % len(searches)
    net = D.Net()
    stats = {"searches": len(batch), "listings": 0, "in_range": 0, "ebay_checked": 0, "priced": 0, "buy": 0, "close": 0, "below": 0, "no_comps": 0}
    t0, budget = now(), K.RUN_MINUTES * 60
    mins = lambda: (now() - t0) / 60  # noqa: E731
    groups = []   # (search, [cheapest listings])
    log(f"Searching LKQ Online: {len(batch)} searches from #{start + 1} of {len(searches)}")
    try:
        for n, s in enumerate(batch):
            if now() - t0 > budget * K.SEARCH_SHARE_OF_TIME:
                stats["searches"], st["cursor"] = n, (start + n) % len(searches)
                log(f"Search time used up after {n} searches ({mins():.0f} min); the rest are searched next run.")
                break
            if n >= K.STOP_AFTER_EMPTY_SEARCHES and stats["listings"] == 0:
                stats["stopped_early"], stats["searches"], st["cursor"] = n, n, start
                log(f"LKQ returned nothing for the first {n} searches, so this run stops here.")
                break
            found = lkq_search(net, s)
            stats["listings"] += len(found)
            fresh = []
            for it in found:
                if not (K.MIN_PART_PRICE <= it["price"] <= K.MAX_BUY_PRICE):
                    continue
                seen = st["seen"].get(it["id"])
                if seen:
                    window = K.REMEMBER_ALERTED_DAYS if seen["v"] == "alert" else K.REMEMBER_CHECKED_DAYS
                    if now() - seen["t"] < window * DAY and not (seen.get("p") and it["price"] <= seen["p"] * 0.92):
                        continue
                if lkq_cost(it, s)["total"] > K.MAX_BUY_PRICE:
                    continue
                fresh.append(it)
            stats["in_range"] += len(fresh)
            fresh.sort(key=lambda x: x["price"])
            if fresh:
                groups.append((s, fresh[:3]))
            log(f"  {s['year']} {s['make']} {s['model']} {s['part']}: {len(found)} listings, {len(fresh)} in range"
                + (f", cheapest {money(fresh[0]['price'])}" if fresh else ""))

        save_state(st)
        # one eBay lookup per search; pricier parts first (bigger dollar gaps)
        groups.sort(key=lambda g: -g[1][0]["price"])
        todo = groups[:K.MAX_EBAY_CHECKS_PER_RUN]
        log(f"Pricing {len(todo)} part searches on eBay ({mins():.0f} min so far)")
        sent = close_sent = 0
        for i, (s, items) in enumerate(todo):
            if now() - t0 > budget:
                stats["out_of_time"] = len(todo) - i
                log(f"Time limit reached; {len(todo) - i} left for another run.")
                break
            if i and i % 5 == 0:
                save_state(st)
            q = comp_query(s, items[0])
            comps = D.ebay_comps(net, st, q, "used")
            stats["ebay_checked"] += 1
            for it in items:
                ev = evaluate(s, it, comps)
                if not ev["ok"]:
                    stats["no_comps"] += 1
                    log(f"  {s['year']} {s['make']} {s['model']} {s['part']}: not enough eBay matches for '{q}' ({ev['n_read']} read)")
                    st["seen"][it["id"]] = {"v": "fail", "t": now(), "p": it["price"]}
                    break   # same comps for the rest of this search
                stats["priced"] += 1
                log(f"  {s['year']} {s['make']} {s['model']} {s['part']}: LKQ {money(ev['cost']['total'])} -> eBay {money(ev['list'])} = {money(ev['profit'])} ({ev['basis']}, {ev['n']})")
                kind = "BUY" if ev["profit"] >= K.MIN_PROFIT else ("CLOSE" if K.SEND_CLOSE_CALLS and ev["profit"] >= K.CLOSE_CALL_MIN_PROFIT and close_sent < K.MAX_CLOSE_CALLS_PER_RUN else None)
                if kind and sent < K.MAX_ALERTS_PER_RUN and alert(s, it, ev, kind, q):
                    sent += 1
                    close_sent += kind == "CLOSE"
                    stats["buy" if kind == "BUY" else "close"] += 1
                    st["seen"][it["id"]] = {"v": "alert", "t": now(), "p": it["price"]}
                else:
                    stats["below"] += 1
                    st["seen"][it["id"]] = {"v": "fail", "t": now(), "p": it["price"]}
    finally:
        net.close()
        stats["minutes"], stats["requests"] = round(mins(), 1), net.requests
        save_state(st)

    lines = [f"🚗 **Auto parts scout**: {stats['searches']} LKQ searches, {stats['listings']} listings, {stats['in_range']} new in your price range, "
             f"{stats['ebay_checked']} priced on eBay → **{stats['buy']} BUY ($100+)**, {stats['close']} close calls.",
             f"Below target: {stats['below']} • not enough eBay matches: {stats['no_comps']}. Next run starts at search #{st['cursor'] + 1} of {len(searches)}."]
    if stats.get("stopped_early"):
        lines.append(f"⚠️ LKQ Online returned nothing for the first {stats['stopped_early']} searches, so this run stopped early. "
                     "Download **debug-lkq** from this run's Summary page on GitHub and send it to Claude.")
    D.discord({"content": "\n".join(lines)})
    log(json.dumps(stats))
    return stats


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        D.discord({"content": f"⚠️ Auto parts scout crashed: {type(e).__name__}: {str(e)[:300]}"})
        raise

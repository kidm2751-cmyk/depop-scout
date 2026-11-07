"""Price check for your agent: Amazon AND Depop (replaces the n8n "Price check" webhook).

Two ways it runs (.github/workflows/price.yml):
  1. ON DEMAND: your agent starts the workflow through GitHub's API with ASINs, Amazon links and/or Depop links
     (and optionally a max price and a request id). About 1 to 2 minutes later the answer is in
     requests/<request id>.json on the "prices" branch.
  2. EVERY HOUR: it re-checks every item in watchlist.txt and posts to Discord when a price goes over your max,
     an Amazon item goes out of stock or comes back, a Depop listing SELLS, or a price moves 5% or more.

Amazon: price, price after coupon, list price, deal, 30-day low, stock, seller.
Depop:  price, whether it has SOLD, shipping, your full cost (item + buyer fee + shipping + tax), size, condition, seller.

Every check also updates latest.json (newest result per item) and history.json (last 60 prices per item) on the
"prices" branch. Amazon items are keyed by ASIN, Depop items by "depop:<listing slug>".
Uses the same Scrapling setup as the scouts. No Bright Data, no n8n.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import scout as D
import amazon_scout as AS
import amazon_settings as A

ROOT = Path(__file__).parent
OUT = Path(os.environ.get("PRICES_DIR", ROOT / "prices_out"))   # the workflow pushes this folder to the "prices" branch
WATCHLIST = ROOT / "watchlist.txt"
D.WEBHOOK = (os.environ.get("PRICE_WEBHOOK_URL") or os.environ.get("AMAZON_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
D.DEBUG_DIR = ROOT / "debug_price"
MAX_PER_RUN = 200
MOVE_PCT = 5            # watchlist: alert when the price moves this much (%) since the last check
HISTORY_KEEP = 60
DEPOP_RE = re.compile(r"depop\.com/products/([A-Za-z0-9][A-Za-z0-9_-]+)", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================ reading what to check
def find_asins(text: str) -> list[str]:
    """ASINs from plain ASINs or Amazon links, in order, no repeats."""
    text = DEPOP_RE.sub(" ", str(text or ""))
    found = re.findall(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})", text, re.I) + re.findall(r"\b(B0[A-Z0-9]{8})\b", text, re.I)
    out = []
    for a in found:
        a = a.upper()
        if a not in out:
            out.append(a)
    return out


def find_depop(text: str) -> list[str]:
    """Depop listing slugs from Depop links (https://www.depop.com/products/<slug>/)."""
    out = []
    for s in DEPOP_RE.findall(str(text or "")):
        s = s.lower().strip("-")
        if s not in out:
            out.append(s)
    return out


def find_items(text: str) -> list[dict]:
    return ([{"kind": "amazon", "id": a} for a in find_asins(text)] +
            [{"kind": "depop", "id": s} for s in find_depop(text)])


def key_of(item: dict) -> str:
    return item["id"] if item["kind"] == "amazon" else f"depop:{item['id']}"


def read_watchlist(path: Path | None = None) -> list[dict]:
    """watchlist.txt: one item per line: an ASIN, Amazon link or Depop link, then optionally your max price, then a note.
    Examples:  B00T0C9XRK 6.50 Essence mascara, listed at 12.99
               https://www.depop.com/products/seller-vintage-carhartt-jacket/ 45 listed on eBay at 89.99"""
    items = []
    path = path or WATCHLIST
    if not path.exists():
        return items
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        found = find_items(line)
        if not found:
            continue
        rest = re.sub(r"https?://\S+|www\.\S+|\b" + re.escape(found[0]["id"]) + r"\b", " ", line, flags=re.I).strip()
        m = re.match(r"\$?(\d+(?:\.\d{1,2})?)\b\s*(.*)", rest)
        items.append({**found[0], "max": float(m.group(1)) if m else None, "note": (m.group(2) if m else rest).strip()[:120], "watch": True})
    return items


# ============================================================ Amazon
def seller_info(html: str) -> tuple[str, bool | None]:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text)
    m = re.search(r"Sold by\s+(.{2,60}?)(?:\s+(?:and|Returns|Payment|Packaging|Gift options|Details|Add to)|\.|$)", text)
    seller = m.group(1).strip() if m else ""
    return seller or "not shown", (bool(re.search(r"amazon", seller, re.I)) if seller else None)


def check_amazon(amz: AS.AmazonNet, asin: str, max_price: float | None) -> dict:
    url = f"{AS.AMAZON}/dp/{asin}"
    html = amz.page(url + "?th=1&psc=1", "#productTitle", "price_product")
    rec = {"platform": "amazon", "asin": asin, "key": asin, "url": url, "checkedAt": now_iso()}
    if not html:
        rec.update(ok=False, status="blocked", error="Amazon showed its robot check or didn't answer. Try again in a few minutes.")
        return rec
    info = AS.parse_product(html)
    if not info.get("title"):
        rec.update(ok=False, status="not_found", error="No product page for this ASIN (wrong ASIN, or the listing was removed).")
        return rec
    price = info.get("price")
    seller, by_amazon = seller_info(html)
    item = {"price": price or 0, **info}
    eff = AS.effective_price(item) if price else None
    coupon = (f"{info['coupon_pct']}% off coupon" if info.get("coupon_pct") else f"${info['coupon_amt']:.2f} off coupon" if info.get("coupon_amt") else None)
    in_stock = bool(price) and not info.get("unavailable")
    rec.update({
        "ok": True, "status": "ok" if in_stock else "out_of_stock",
        "title": info["title"][:160], "brand": info.get("brand", ""),
        "price": price, "priceAfterCoupon": eff if eff != price else None, "coupon": coupon,
        "listPrice": info.get("typical"), "listPriceKind": info.get("typical_kind"), "pctOff": AS.pct_off(item) if price else 0,
        "lowestIn30Days": bool(info.get("low30")), "deal": info.get("badge"),
        "inStock": in_stock, "seller": seller, "soldByAmazon": by_amazon if by_amazon is not None else bool(info.get("by_amazon")) or None,
        "boughtPastMonth": info.get("bought"), "rating": info.get("rating"), "reviews": info.get("reviews"),
        "photo": (info.get("photos") or [None])[0],
    })
    pay = eff or price
    if max_price is not None:
        rec["max"] = max_price
        if not in_stock:
            rec["verdict"] = "OUT OF STOCK"
        elif pay is not None:
            rec["verdict"] = f"OK: {pay:.2f} is at or under your max {max_price:.2f}" if pay <= max_price else f"TOO HIGH: {pay:.2f} is over your max {max_price:.2f}"
    return rec


# ============================================================ Depop
def depop_page(net: D.Net, url: str, slug: str) -> str:
    """Depop usually refuses plain requests from GitHub, so the stealth browser does most of the work."""
    r = net.get(url)
    html = D.page_text(r) if r is not None and not D.Net._blocked(r) else ""
    if html and getattr(r, "status", 200) != 404 and (slug in html.lower() or "application/ld+json" in html or "__NEXT_DATA__" in html):
        return html
    if getattr(r, "status", 0) == 404:
        return "<!-- 404 -->"
    page, _ = net.browser(url, wait_selector='img[src*="depop"]')
    return D.page_text(page)


def parse_depop(html: str, slug: str) -> dict:
    """The listing's own data from the page: price, sold, shipping, photos, size, condition, seller."""
    blobs = D.json_blobs_from_html(html) + D.objects_with_slug(D.rsc_text(html)) + D.objects_with_slug(html)
    best, ld = None, None
    for blob in blobs:
        for d in D.walk(blob):
            if str(d.get("slug", "")).lower() == slug:
                p = D.normalize_depop(d)
                if p and (best is None or len(p["photos"]) > len(best["photos"])):
                    best = p
            if str(d.get("@type", "")).lower() == "product" and ld is None:
                ld = d
    out = dict(best) if best else {}
    if ld:
        offers = ld.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if not out.get("price"):
            out["price"] = D.to_price(offers.get("price"))
        avail = str(offers.get("availability", "")).lower()
        if "outofstock" in avail or "soldout" in avail or "discontinued" in avail:
            out["sold"] = True
        out.setdefault("title", str(ld.get("name") or "")[:140])
        b = ld.get("brand")
        out.setdefault("brand", (b.get("name") if isinstance(b, dict) else b) or "")
        imgs = ld.get("image") or []
        imgs = [imgs] if isinstance(imgs, str) else [u for u in imgs if isinstance(u, str)]
        if imgs and len(imgs) > len(out.get("photos") or []):
            out["photos"] = imgs
    # Only when the page has no listing data at all: look for Depop's "Sold" banner (with data, a "Sold" badge on a
    # "similar items" card could be mistaken for this listing)
    if not best and not ld and re.search(r'>\s*Sold\s*<|"isSold"\s*:\s*true|"status"\s*:\s*"(?:sold|marked_as_sold)"|This item (?:has been|is) sold', html or "", re.I):
        out["sold"] = True
    return out


def check_depop(net: D.Net, slug: str, max_price: float | None) -> dict:
    url = f"https://www.depop.com/products/{slug}/"
    rec = {"platform": "depop", "slug": slug, "key": f"depop:{slug}", "url": url, "checkedAt": now_iso()}
    html = depop_page(net, url, slug)
    D.debug_save("price_depop.html", html)
    if html == "<!-- 404 -->":
        rec.update(ok=False, status="not_found", error="This Depop listing doesn't exist anymore (deleted, or a wrong link).")
        return rec
    if not html:
        rec.update(ok=False, status="blocked", error="Depop didn't answer (it may be blocking GitHub's servers). Try again in a few minutes.")
        return rec
    info = parse_depop(html, slug)
    if not info.get("price") and not info.get("sold"):
        rec.update(ok=False, status="not_found", error="Couldn't read this listing (removed, or Depop changed its page).")
        return rec
    sold = bool(info.get("sold"))
    item = {"price": info.get("price") or 0, "total": info.get("total"), "ship": info.get("ship"), "title": info.get("title") or slug,
            "description": info.get("description", "")}
    cost = D.depop_cost(item) if item["price"] else None
    rec.update({
        "ok": True, "status": "sold" if sold else "ok", "sold": sold, "inStock": not sold,
        "title": (info.get("title") or slug)[:160], "brand": info.get("brand", ""),
        "price": info.get("price"), "shipping": cost["ship"] if cost else None, "shippingKnown": cost["ship_known"] if cost else None,
        "buyerFee": cost["fee"] if cost else None, "tax": cost["tax"] if cost else None, "totalCost": cost["total"] if cost else None,
        "size": info.get("size") or None, "condition": info.get("condition") or None, "seller": info.get("seller") or None,
        "photo": (info.get("photos") or [None])[0],
    })
    if max_price is not None:
        rec["max"] = max_price
        if sold:
            rec["verdict"] = "SOLD"
        elif cost:
            rec["verdict"] = (f"OK: {cost['total']:.2f} total is at or under your max {max_price:.2f}" if cost["total"] <= max_price
                              else f"TOO HIGH: {cost['total']:.2f} total is over your max {max_price:.2f}")
    elif sold:
        rec["verdict"] = "SOLD"
    return rec


# ============================================================ alerts and saving
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def pay_of(rec: dict | None):
    if not rec:
        return None
    return rec.get("totalCost") if rec.get("platform") == "depop" else (rec.get("priceAfterCoupon") or rec.get("price"))


def watch_alert(rec: dict, prev: dict | None, w: dict) -> str | None:
    """Why a watchlist item deserves a Discord message (None = nothing to report)."""
    if not rec.get("ok"):
        return None
    gone = "SOLD on Depop" if rec.get("platform") == "depop" else "now OUT OF STOCK"
    if not rec["inStock"]:
        return gone if (not prev or prev.get("inStock", True)) else None
    pay, old = pay_of(rec), pay_of(prev)
    if pay and w.get("max") and pay > w["max"] and not (old and old > w["max"]):
        return f"{'total cost' if rec.get('platform') == 'depop' else 'price'} is now {D.money(pay)}, over your max {D.money(w['max'])}"
    if pay and old and abs(pay - old) / old * 100 >= MOVE_PCT:
        return f"price {'rose' if pay > old else 'dropped'} from {D.money(old)} to {D.money(pay)}"
    if prev and prev.get("inStock") is False:
        return f"back in stock at {D.money(pay or 0)}"
    return None


def send_alert(rec: dict, why: str, w: dict) -> None:
    bad = any(x in why for x in ("OUT OF STOCK", "SOLD", "over your max", "rose", "TOO HIGH"))
    color = 0xEF4444 if bad else 0x22C55E
    depop = rec.get("platform") == "depop"
    if depop:
        now = (f"**SOLD**" if rec.get("sold") else f"**{D.money(rec.get('totalCost') or 0)}** total\n{D.money(rec.get('price') or 0)} + fee {D.money(rec.get('buyerFee') or 0)}"
               f" + ship {D.money(rec.get('shipping') or 0)}{'' if rec.get('shippingKnown') else ' (est.)'} + tax {D.money(rec.get('tax') or 0)}")
        if not rec.get("sold") and (rec.get("size") or rec.get("condition")):
            now += f"\nSize {rec.get('size') or '?'} • {rec.get('condition') or '?'}"
        fields = [{"name": "Depop now", "value": now, "inline": True}]
        links = f"[Depop]({rec['url']})"
        footer = f"Depop • {rec.get('seller') or rec['slug']}"
    else:
        fields = [{"name": "Amazon now", "value": f"**{D.money(pay_of(rec) or 0)}**" + (f" (with {rec['coupon']})" if rec.get("coupon") else "")
                   + f"\n{'In stock' if rec['inStock'] else 'Out of stock'} • sold by {rec.get('seller')}", "inline": True}]
        links = f"[Amazon]({rec['url']})  •  [Price history](https://camelcamelcamel.com/product/{rec['asin']})"
        footer = f"ASIN {rec['asin']}"
    if w.get("max"):
        fields.append({"name": "Your max", "value": D.money(w["max"]), "inline": True})
    if w.get("note"):
        fields.append({"name": "Note", "value": w["note"][:200], "inline": False})
    fields.append({"name": "Links", "value": links, "inline": False})
    emb = {"title": f"🔔 {rec.get('title', rec['key'])[:200]}: {why}"[:250], "url": rec["url"], "color": color, "fields": fields,
           "footer": {"text": footer + (" • watchlist" if w.get("watch") else " • price check")}}
    if rec.get("photo"):
        emb["thumbnail"] = {"url": rec["photo"]}
    D.discord({"embeds": [emb], "_plain": f"{rec['key']}: {why} {rec['url']}"})


def run(items_text: str = "", max_text: str = "", request_id: str = "", notify: bool = False, watch: bool = False) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "requests").mkdir(exist_ok=True)
    latest = load_json(OUT / "latest.json", {})
    history = load_json(OUT / "history.json", {})
    default_max = float(max_text) if re.fullmatch(r"\s*\d+(\.\d+)?\s*", max_text or "") else None
    todo = [{**i, "max": default_max, "note": "", "watch": False} for i in find_items(items_text)]
    have = {key_of(t) for t in todo}
    if watch:
        todo += [w for w in read_watchlist() if key_of(w) not in have]
    todo = todo[:MAX_PER_RUN]
    net = D.Net()
    amz = AS.AmazonNet(net)
    results, alerts = [], 0
    D.log(f"Checking {len(todo)} item{'s' if len(todo) != 1 else ''}" + (f" (request {request_id})" if request_id else ""))
    try:
        for w in todo:
            if w["kind"] == "amazon":
                mx = w["max"] if (w["max"] is not None or w["watch"]) else A.MAX_AMAZON_PRICE   # old n8n default: your $45 max
                rec = check_amazon(amz, w["id"], mx)
            else:
                rec = check_depop(net, w["id"], w["max"])
            prev = latest.get(key_of(w))
            if rec.get("ok"):
                latest[key_of(w)] = rec
                h = history.setdefault(key_of(w), [])
                h.append([rec["checkedAt"], pay_of(rec), rec["inStock"]])
                del h[:-HISTORY_KEEP]
            results.append(rec)
            D.log(f"  {key_of(w)}: {rec.get('status')} {rec.get('price')} {rec.get('verdict', '')} {rec.get('title', '')[:50]}")
            if rec.get("ok") and (w["watch"] or notify):
                why = watch_alert(rec, prev, w) if w["watch"] else (rec.get("verdict") or rec.get("status"))
                if why:
                    send_alert(rec, why, w)
                    alerts += 1
    finally:
        net.close()
    out = {"ok": all(r.get("ok") for r in results) if results else False, "requestId": request_id or None, "finishedAt": now_iso(),
           "checked": len(results), "items": results}
    if not results:
        out["error"] = "Nothing to check. Pass ASINs, Amazon links or Depop links, comma or space separated."
    (OUT / "latest.json").write_text(json.dumps(latest, indent=1, sort_keys=True))
    (OUT / "history.json").write_text(json.dumps(history, separators=(",", ":"), sort_keys=True))
    (OUT / "last_run.json").write_text(json.dumps(out, indent=1))
    if request_id:
        rid = re.sub(r"[^\w.-]", "_", request_id)[:80]
        (OUT / "requests" / f"{rid}.json").write_text(json.dumps(out, indent=1))
    reqs = sorted((OUT / "requests").glob("*.json"), key=lambda p: p.stat().st_mtime)
    for old in reqs[:-300]:   # keep the newest 300 answers
        old.unlink()
    D.log(json.dumps({"checked": len(results), "alerts": alerts}))
    return out


if __name__ == "__main__":
    try:
        run(os.environ.get("ASINS", ""), os.environ.get("MAX_PRICE", ""), os.environ.get("REQUEST_ID", ""),
            os.environ.get("NOTIFY", "") == "true", os.environ.get("WATCH", "") == "true")
    except Exception as e:
        D.discord({"content": f"⚠️ Price check crashed: {type(e).__name__}: {str(e)[:300]}"})
        raise

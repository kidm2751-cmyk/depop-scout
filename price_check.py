"""Amazon price check for your agent (replaces the n8n "Price check" webhook).

Two ways it runs (.github/workflows/price.yml):
  1. ON DEMAND: your agent starts the workflow through GitHub's API with a list of ASINs (and optionally a max price
     and a request id). About 1 to 2 minutes later the answer is in requests/<request id>.json on the "prices" branch.
  2. EVERY HOUR: it re-checks every ASIN in watchlist.txt and posts to Discord when a price goes over your max,
     the item goes out of stock, or the price moves 5% or more.

Every check also updates latest.json (newest result per ASIN) and history.json (last 60 prices per ASIN) on the
"prices" branch, so your agent can read the freshest known price of anything it has checked.
Uses the same Scrapling setup as amazon_scout.py. No Bright Data, no n8n.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
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


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_asins(text: str) -> list[str]:
    """ASINs from plain ASINs or Amazon links, in order, no repeats."""
    text = str(text or "")
    found = re.findall(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})", text, re.I) + re.findall(r"\b(B0[A-Z0-9]{8})\b", text, re.I)
    out = []
    for a in found:
        a = a.upper()
        if a not in out:
            out.append(a)
    return out


def read_watchlist(path: Path | None = None) -> list[dict]:
    """watchlist.txt: one item per line: ASIN or Amazon link, then optionally your max price, then any note.
    Example:  B09BNGBLQK 32.50 LEGO police station, listed on eBay at 54.99"""
    items = []
    path = path or WATCHLIST
    if not path.exists():
        return items
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        asins = find_asins(line)
        if not asins:
            continue
        rest = re.sub(r"https?://\S+|\b" + asins[0] + r"\b", " ", line, flags=re.I).strip()
        m = re.match(r"\$?(\d+(?:\.\d{1,2})?)\b\s*(.*)", rest)
        items.append({"asin": asins[0], "max": float(m.group(1)) if m else None, "note": (m.group(2) if m else rest).strip()[:120]})
    return items


def seller_info(html: str) -> tuple[str, bool | None]:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text)
    m = re.search(r"Sold by\s+(.{2,60}?)(?:\s+(?:and|Returns|Payment|Packaging|Gift options|Details|Add to)|\.|$)", text)
    seller = m.group(1).strip() if m else ""
    return seller or "not shown", (bool(re.search(r"amazon", seller, re.I)) if seller else None)


def check_one(amz: AS.AmazonNet, asin: str, max_price: float | None) -> dict:
    url = f"{AS.AMAZON}/dp/{asin}"
    html = amz.page(url + "?th=1&psc=1", "#productTitle", "price_product")
    rec = {"asin": asin, "url": url, "checkedAt": now_iso()}
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
    if max_price is not None:
        rec["max"] = max_price
        pay = eff or price
        if not in_stock:
            rec["verdict"] = "OUT OF STOCK"
        elif pay is not None:
            rec["verdict"] = f"OK: {pay:.2f} is at or under your max {max_price:.2f}" if pay <= max_price else f"TOO HIGH: {pay:.2f} is over your max {max_price:.2f}"
    return rec


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def watch_alert(rec: dict, prev: dict | None, w: dict) -> str | None:
    """Why a watchlist item deserves a Discord message (None = nothing to report)."""
    if not rec.get("ok"):
        return None
    if not rec["inStock"] and (not prev or prev.get("inStock", True)):
        return "now OUT OF STOCK"
    pay = rec.get("priceAfterCoupon") or rec.get("price")
    if pay and w.get("max") and pay > w["max"] and not (prev and (prev.get("priceAfterCoupon") or prev.get("price") or 0) > w["max"]):
        return f"price is now {D.money(pay)}, over your max {D.money(w['max'])}"
    old = prev and (prev.get("priceAfterCoupon") or prev.get("price"))
    if pay and old and abs(pay - old) / old * 100 >= MOVE_PCT:
        return f"price {'rose' if pay > old else 'dropped'} from {D.money(old)} to {D.money(pay)}"
    if rec["inStock"] and prev and prev.get("inStock") is False:
        return f"back in stock at {D.money(pay)}"
    return None


def send_watch_alert(rec: dict, why: str, w: dict) -> None:
    color = 0xEF4444 if ("OUT OF STOCK" in why or "over your max" in why or "rose" in why) else 0x22C55E
    fields = [{"name": "Amazon now", "value": f"**{D.money(rec.get('priceAfterCoupon') or rec.get('price') or 0)}**" + (f" (with {rec['coupon']})" if rec.get("coupon") else "")
               + f"\n{'In stock' if rec['inStock'] else 'Out of stock'} • sold by {rec.get('seller')}", "inline": True}]
    if w.get("max"):
        fields.append({"name": "Your max", "value": D.money(w["max"]), "inline": True})
    if w.get("note"):
        fields.append({"name": "Note", "value": w["note"][:200], "inline": False})
    fields.append({"name": "Links", "value": f"[Amazon]({rec['url']})  •  [Price history](https://camelcamelcamel.com/product/{rec['asin']})", "inline": False})
    emb = {"title": f"🔔 {rec.get('title', rec['asin'])[:200]}: {why}"[:250], "url": rec["url"], "color": color, "fields": fields, "footer": {"text": f"ASIN {rec['asin']} • watchlist"}}
    if rec.get("photo"):
        emb["thumbnail"] = {"url": rec["photo"]}
    D.discord({"embeds": [emb], "_plain": f"{rec['asin']}: {why} {rec['url']}"})


def run(asins_text: str = "", max_text: str = "", request_id: str = "", notify: bool = False, watch: bool = False) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "requests").mkdir(exist_ok=True)
    latest = load_json(OUT / "latest.json", {})
    history = load_json(OUT / "history.json", {})
    default_max = float(max_text) if re.fullmatch(r"\s*\d+(\.\d+)?\s*", max_text or "") else None
    todo = [{"asin": a, "max": default_max, "note": ""} for a in find_asins(asins_text)]
    watchlist = read_watchlist() if watch else []
    have = {t["asin"] for t in todo}
    todo += [w for w in watchlist if w["asin"] not in have]
    todo = todo[:MAX_PER_RUN]
    net = D.Net()
    amz = AS.AmazonNet(net)
    results, alerts = [], 0
    D.log(f"Checking {len(todo)} ASIN{'s' if len(todo) != 1 else ''}" + (f" (request {request_id})" if request_id else ""))
    try:
        for w in todo:
            mx = w["max"]
            if mx is None and w not in watchlist:
                mx = A.MAX_AMAZON_PRICE   # same as the old n8n check: compare to your $45 max when no max is given
            rec = check_one(amz, w["asin"], mx)
            prev = latest.get(w["asin"])
            if rec.get("ok"):
                latest[w["asin"]] = rec
                h = history.setdefault(w["asin"], [])
                h.append([rec["checkedAt"], rec.get("priceAfterCoupon") or rec.get("price"), rec["inStock"]])
                del h[:-HISTORY_KEEP]
            results.append(rec)
            D.log(f"  {w['asin']}: {rec.get('status')} {rec.get('price')} {rec.get('verdict', '')} {rec.get('title', '')[:50]}")
            if rec.get("ok") and (w in watchlist or notify):
                why = watch_alert(rec, prev, w) if w in watchlist else (rec.get("verdict") or rec.get("status"))
                if why:
                    send_watch_alert(rec, why, w)
                    alerts += 1
    finally:
        net.close()
    out = {"ok": all(r.get("ok") for r in results) if results else False, "requestId": request_id or None, "finishedAt": now_iso(),
           "checked": len(results), "items": results}
    if not results:
        out["error"] = "No ASINs given. Pass ASINs or Amazon links, comma or space separated."
    (OUT / "latest.json").write_text(json.dumps(latest, indent=1, sort_keys=True))
    (OUT / "history.json").write_text(json.dumps(history, separators=(",", ":"), sort_keys=True))
    (OUT / "last_run.json").write_text(json.dumps(out, indent=1))
    if request_id:
        rid = re.sub(r"[^\w.-]", "_", request_id)[:80]
        (OUT / "requests" / f"{rid}.json").write_text(json.dumps(out, indent=1))
    # keep the requests folder small: newest 300 answers
    reqs = sorted((OUT / "requests").glob("*.json"), key=lambda p: p.stat().st_mtime)
    for old in reqs[:-300]:
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

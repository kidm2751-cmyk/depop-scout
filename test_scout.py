"""Offline tests: run with `python tests/test_scout.py` (no internet needed)."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DRY_RUN"] = "1"
import scout  # noqa: E402
import settings as S  # noqa: E402

FX = Path(__file__).parent / "fixtures"
passed = 0


def ok(cond, msg):
    global passed
    assert cond, msg
    passed += 1
    print("  ✓", msg)


class FakeResp:
    def __init__(self, text, status=200):
        self.html_content, self.status, self.body = text, status, text.encode()

    def json(self):
        return json.loads(self.html_content)


print("\n1) Reading Depop data (both formats)")
pres = scout.products_from_json(json.loads((FX / "depop_search_presentation.json").read_text()))
ok(len(pres) == 5, "newer Depop format: 5 listings read")
p = pres[0]
ok(p["price"] == 15.0 and p["total"] == 16.75 and p["brand"] == "Patagonia" and p["size"] == "M", "price, total (with buyer fee), brand, size")
ok(len(p["photos"]) == 6, "all 6 photos read")
ok([x for x in pres if x["id"] == "104"][0]["sold"], "sold listing detected")
v2 = scout.products_from_json(json.loads((FX / "depop_search_v2.json").read_text()))
c = [x for x in v2 if x["id"] == "201"][0]
ok(c["price"] == 35.0 and c["brand"] == "Carhartt" and c["size"] == "L", "older Depop format: price string, brand/size objects")
ok(c["photos"][0].endswith("/201/P0.jpg") and len(c["photos"]) == 2, "picks the largest version of each photo (P0)")
ok(c["ship"] == 8.99, "Depop shipping read even when it's inside the price data ($8.99)")
ok([x for x in v2 if x["id"] == "202"][0]["price"] == 42.0, "whole-number cents (4200) read as $42.00")

print("\n2) Listing page details")
item = dict(p)
net = scout.Net()
net.get = lambda url, params=None, accept_json=False: FakeResp((FX / "depop_product.html").read_text())
item = scout.depop_details(net, item)
ok(len(item["photos"]) == 9 and item["ship"] == 7.49 and item["condition"] == "Used - Good", "listing page adds all 9 photos and the $7.49 Depop shipping")

print("\n3) eBay pages (old and new layouts)")
sold = scout.parse_ebay_results((FX / "ebay_sold_sitem.html").read_text())
ok(len(sold) == 6 and all(s["sold"] for s in sold), "sold page: 6 sold listings ('Shop on eBay' placeholder and price ranges skipped)")
ok([s for s in sold if s["url"].endswith("/112")][0]["total"] == 65.95, "shipping added: $58.00 + $7.95 = $65.95")
active = scout.parse_ebay_results((FX / "ebay_active_scard.html").read_text())
ok(len(active) == 5 and active[0]["total"] == 102.39 and active[1]["ship"] == 0.0, "new eBay layout (s-card) works too")
ok("Opens in a new window" not in active[0]["title"], "cleans eBay screen-reader text out of titles")

print("\n4) Matching and money")
q = scout.comp_query(item, "Patagonia Synchilla fleece")
ok(q.lower().startswith("patagonia") and "fleece" in q.lower() and q.lower().count("patagonia") == 1, f"eBay search built like a buyer would type it: '{q}'")
ok(not scout.matches(item, "Lot of 3 Patagonia Synchilla fleece", q), "lots/bundles don't count as comps")
ok(not scout.matches(item, "Columbia Synchilla style fleece", q), "other brands don't count")
ev = scout.evaluate(item, {"sold": sold, "active": []}, q)
cost = ev["cost"]
exp_fee = round(16.75 - 15.0, 2)
ok(cost["fee"] == exp_fee and cost["fee_exact"], f"uses Depop's exact buyer fee from the total price ({scout.money(exp_fee)})")
exp_total = round(15.0 + exp_fee + 7.49 + (15.0 + exp_fee) * S.BUY_TAX_PCT / 100, 2)
ok(abs(cost["total"] - exp_total) < 0.02, f"Depop cost = item + fee + shipping + tax = {scout.money(cost['total'])}")
e = ev["ebay"]
L = ev["list"]
base = L * (1 + S.EBAY_BUYER_TAX_PCT / 100)
exp_e = base * S.EBAY_FEE_PCT / 100 + base * S.EBAY_AD_RATE_PCT / 100 + 0.40 + S.SHIP_TO_EBAY_BUYER["medium"] + S.PACKAGING_COST
ok(abs(e["total"] - exp_e) < 0.02, f"eBay costs = 13.6% fee + ads on price+tax + $0.40 + postage + packaging = {scout.money(e['total'])}")
ok(ev["basis"] == "sold" and ev["n"] == 5, "priced from 5 matching SOLD listings (lot excluded)")
ok(abs(ev["profit"] - round(L - e["total"] - cost["total"], 2)) < 0.02, f"profit = {scout.money(ev['profit'])} on eBay list price {scout.money(L)}")
no_fee = dict(item, total=None)
ok(abs(scout.depop_cost(no_fee)["fee"] - (15 * 0.05 + 1)) < 0.01, "without Depop's total, buyer fee estimated at 5% + $1")
ok(scout.weight_class({"title": "Carhartt Detroit jacket", "description": ""}) == "heavy" and scout.weight_class({"title": "band tee", "description": ""}) == "light", "postage size from the item type")
ok(scout.is_junk({"title": "Lot of 3 fleece", "description": ""}) and not scout.is_junk({"title": "Baby Phat jacket", "description": ""}), "skip words are whole words (Baby Phat isn't skipped)")

print("\n5) Full run, offline (fake Depop + eBay), checking the Discord messages")
tmp = Path(tempfile.mkdtemp())
scout.STATE_FILE = tmp / "state.json"
(tmp / "keywords.txt").write_text("Patagonia Synchilla fleece\nCarhartt Detroit jacket\n")
scout.ROOT = tmp
S.DELAY_SECONDS = (0, 0)


def fake_get(self, url, params=None, accept_json=False):
    if "webapi.depop.com" in url:
        what = (params or {}).get("what", "")
        f = "depop_search_presentation.json" if "Patagonia" in what else "depop_search_v2.json"
        return FakeResp((FX / f).read_text())
    if "depop.com/products/" in url:
        return FakeResp((FX / "depop_product.html").read_text() if "seller1" in url else "<html></html>")
    if "ebay.com" in url:
        if "LH_Sold" in url:
            return FakeResp((FX / "ebay_sold_sitem.html").read_text() if "patagonia" in url.lower() else "<html><body>sign in</body></html>")
        return FakeResp((FX / "ebay_active_scard.html").read_text())
    return FakeResp("", 404)


scout.Net.get = fake_get
scout.Net.browser = lambda self, url, record_pattern=None: (None, [])
scout.SENT.clear()
stats = scout.run()
alerts = [m for m in scout.SENT if m.get("embeds")]
print("   stats:", json.dumps({k: stats[k] for k in ("depop_found", "new", "ebay_checked", "alerts", "no_comps", "not_profitable")}))
ok(stats["alerts"] >= 1, "at least one find sent")
first = alerts[0]["embeds"]
ok(len(first) == 8 and all(e.get("image") for e in first), "the alert is ONE message with 8 photos")
ok(len({e["url"] for e in first}) == 2, "photos grouped into two galleries of 4")
fields = json.dumps(first[0]["fields"])
ok("You pay on Depop" in fields and "eBay costs" in fields and "Profit" in fields and "Buy on Depop" in fields, "card shows Depop cost breakdown, eBay costs, profit and links")
carhartt = [m for m in alerts if "Carhartt" in m["embeds"][0]["title"]]
if carhartt:
    ok("active eBay prices" in json.dumps(carhartt[0]), "when eBay hides sold prices, it says the price is from active listings")
summary = [m["content"] for m in scout.SENT if m.get("content")][-1]
ok("Depop → eBay scout" in summary, "run summary sent")
st = json.loads(scout.STATE_FILE.read_text())
ok(st["cursor"] == 0 and any(v["v"] == "alert" for v in st["seen"].values()), "remembers what it sent and where the keyword rotation is")
scout.SENT.clear()
stats2 = scout.run()
ok(stats2["alerts"] == 0, "second run doesn't repeat the same finds")
ok(all("_plain" not in m for m in scout.SENT), "internal fallback text is never sent to Discord")

print(f"\nALL {passed} CHECKS PASSED")

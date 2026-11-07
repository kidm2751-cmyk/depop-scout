# Depop → eBay Scout

Finds Depop listings worth **buying and relisting on eBay**, and posts each one to Discord as a single message with up to 8 photos, your full Depop cost, eBay fees and postage, and profit.

It runs on **GitHub Actions** (free, on a schedule, with your PC off) using the **Scrapling** scraper.

## How it works

1. **Searches Depop** with 5,000 keywords in 5 daily blocks of 1,000: each day it searches a new block, 250 per run over 4 runs, and starts over after Day 5. It reads public listings only, never logged in.
2. **Picks the bargains:** for each search, the 3 cheapest listings compared to the rest of the results (skipping lots, replicas, kids' items and sold items).
3. **Opens each listing** for all its photos, the Depop shipping price, size and condition.
4. **Prices it on eBay:** searches eBay the way a buyer would (brand plus item). It uses **sold** prices when eBay shows them, otherwise active prices lowered by 12%, and takes a conservative 35th-percentile price from at least 3 matching listings.
5. **Does the math:**
   - **You pay on Depop:** item + Depop buyer fee (exact when Depop shows the total, otherwise 5% + $1) + Depop shipping + 9.75% tax
   - **eBay takes:** 13.6% final value fee + 5% ads (both on price + buyer tax) + $0.40 order fee + your postage + packaging
   - **Profit:** eBay price minus all of the above. It alerts at **$10+ profit and 30%+ return**.
6. **Remembers what it sent**, so the same listing isn't sent again for 30 days unless the price drops 10%.

## Setup (about 10 minutes)

**1. Make a GitHub account** at github.com if you don't have one.

**2. Create a repository:**
- Click **+** at the top right, then **New repository**.
- Name it `depop-scout`. Choose **Public**: public repos get unlimited free Actions minutes, and 4 runs a day of about 90 minutes is far more than the 2,000 a month private repos get. Your Discord webhook stays hidden either way (it's a secret).
- Click **Create repository**.

**3. Upload the files:**
- On the new repo page, click **uploading an existing file**.
- Unzip the folder I sent and drag **everything inside it** into the upload box, including the `.github` folder.
- Click **Commit changes**.
- Check that `.github/workflows/scout.yml` appears in the repo. If the `.github` folder didn't upload (some browsers skip folders that start with a dot): click **Add file → Create new file**, type `.github/workflows/scout.yml` as the name, paste in the contents of that file, and commit.

**4. Add your Discord webhook as a secret** (it never goes in the files):
- In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**.
- Name: `DISCORD_WEBHOOK_URL`
- Secret: your Discord webhook URL. Use a new channel for this if you want to keep it separate from Deal Scout.
- Click **Add secret**.

**5. Test it:**
- Open the **Actions** tab. If GitHub asks, click to enable workflows.
- Click **Depop to eBay scout → Run workflow**.
- Optional: tick "Test only" to see the finds in the log without sending them to Discord.
- A run takes about 60 to 90 minutes. Click into the run to watch the log; each keyword shows how many listings it found.

After that it runs on its own at about 8am, 11am, 3pm and 8pm Pacific.

## Changing things

- **Thresholds, fees, postage, number of photos:** edit `settings.py` on GitHub (open the file, click the pencil icon, then **Commit**).
- **Keywords:** edit `keywords.txt`, one search per line. The blocks are simply every 1,000 lines in order, so adding or removing lines is fine.

## If something goes wrong

- **"Depop returned nothing this run":** Depop is probably blocking GitHub's servers. The scout already tries Scrapling's stealth browser automatically. If it keeps happening, it needs to run from a home internet connection (your PC) or through a residential proxy.
- **"eBay blocked many lookups":** same idea for eBay. Some items get priced from active listings instead of sold ones, and the alert says so.
- **Crashes** are posted to Discord as "Depop scout crashed: ...". Send me that message.

## Good to know

- **Depop's Terms of Service forbid scraping** (section 7.2) and allow them to ban accounts that do it. The scout never logs in and goes slowly, but it carries some risk. Buy through your normal Depop account by hand.
- **Buy first, then list.** Depop items are one of a kind, and eBay requires you to have the item.
- **Check before buying:** open "eBay sold prices" on each alert, and look at the photos for flaws. Prices are estimates from comparable listings, not guarantees.
- **Fees** were checked in October 2026: Depop's US buyer fee is up to 5% + $1, and eBay's is 13.6% + $0.40 for most clothing. Check your category on eBay's fee page if you sell something unusual.

---

# Amazon → eBay Deal Scout (replaces the n8n Deal Scout)

Lives in the same repo and runs on its own schedule (about 7:47am, 10:47am, 2:47pm and 7:47pm Pacific).

**What it does each run:**
1. Searches Amazon for 250 of today's 1,000 keywords (`amazon_keywords.txt` has 5,000; a new 1,000 each day, repeating after Day 5). Only items between $3 and your $45 max.
2. Reads every result's deals: % off Amazon's typical or list price, coupons, Limited time / Lightning deals, Amazon's "lowest price in 30 days", and its own price history (lowest in 30, 90 and 365 days as the history builds up). It also reads demand: "bought in past month", ratings and best-seller rank.
3. Drops knockoffs (brand searches must show the brand), sponsored results, low ratings and your old skip list.
4. Checks the strongest deals and highest demand first, prices them on eBay (sold listings first) and works out profit after Amazon tax and eBay fees.
5. Sends each find as one Discord message with up to 8 photos:
   - 🟢 **BUY**: $3+ profit and 10%+ return, priced from eBay sold listings
   - 🟡 **MAYBE**: thinner profit, or priced from active listings only
   - 🔥 **DEAL**: a big deal on a high-demand item that eBay couldn't price; check it yourself

A deal is a bonus, not a requirement: profitable items at regular price are sent too.

**Setup:** add a second repository secret named `AMAZON_WEBHOOK_URL` with the Discord webhook for your Amazon channel (Settings → Secrets and variables → Actions). If it's missing, it uses `DISCORD_WEBHOOK_URL`. Then Actions → **Amazon deal scout** → Run workflow.

**Settings:** `amazon_settings.py` (max price, profit targets, fees, what counts as a deal).

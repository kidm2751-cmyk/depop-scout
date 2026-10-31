# Depop → eBay Scout

Finds Depop listings worth **buying and relisting on eBay**, and posts each one to Discord as a single message with up to 8 photos, your full Depop cost, eBay fees and postage, and profit.

It runs on **GitHub Actions** (free, on a schedule, with your PC off) using the **Scrapling** scraper.

## How it works

1. **Searches Depop** for 75 of your 300 keywords per run (4 runs a day, so all 300 daily). It reads public listings only, never logged in, pausing 1.5 to 3.5 seconds between requests.
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
- Name it `depop-scout` and choose **Private**. On the free plan, private repos get 2,000 Actions minutes a month, and this uses roughly 1,200 to 1,800.
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
- A run takes about 10 to 15 minutes. Click into the run to watch the log; each keyword shows how many listings it found.

After that it runs on its own at about 8am, 11am, 3pm and 8pm Pacific.

## Changing things

- **Thresholds, fees, postage, number of photos:** edit `settings.py` on GitHub (open the file, click the pencil icon, then **Commit**).
- **Keywords:** edit `keywords.txt`, one search per line.

## If something goes wrong

- **"Depop returned nothing this run":** Depop is probably blocking GitHub's servers. The scout already tries Scrapling's stealth browser automatically. If it keeps happening, it needs to run from a home internet connection (your PC) or through a residential proxy.
- **"eBay blocked many lookups":** same idea for eBay. Some items get priced from active listings instead of sold ones, and the alert says so.
- **Crashes** are posted to Discord as "Depop scout crashed: ...". Send me that message.

## Good to know

- **Depop's Terms of Service forbid scraping** (section 7.2) and allow them to ban accounts that do it. The scout never logs in and goes slowly, but it carries some risk. Buy through your normal Depop account by hand.
- **Buy first, then list.** Depop items are one of a kind, and eBay requires you to have the item.
- **Check before buying:** open "eBay sold prices" on each alert, and look at the photos for flaws. Prices are estimates from comparable listings, not guarantees.
- **Fees** were checked in October 2026: Depop's US buyer fee is up to 5% + $1, and eBay's is 13.6% + $0.40 for most clothing. Check your category on eBay's fee page if you sell something unusual.

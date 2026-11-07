# =====================================================================
#  DEPOP -> EBAY SCOUT  -  SETTINGS (the only file you need to edit)
#  Your Discord webhook is NOT here: it goes in GitHub as a secret named DISCORD_WEBHOOK_URL.
# =====================================================================

# ---- What to search ----
KEYWORDS_PER_DAY = 1000          # keywords.txt has 5,000 = 5 daily blocks of 1,000; a new block each day, repeating after Day 5
KEYWORDS_PER_RUN = 250           # 250 per run x 4 runs a day = today's whole block
TIMEZONE = "America/Los_Angeles" # decides when a new day (and a new block) starts
DEPOP_RESULTS_PER_KEYWORD = 24   # Depop listings read per search
CANDIDATES_PER_KEYWORD = 3       # best-priced listings per search that get an eBay check
MAX_EBAY_CHECKS_PER_RUN = 40     # listings checked on eBay per run: the biggest bargains across all 250 searches first
MIN_DEPOP_PRICE = 8.00
MAX_DEPOP_PRICE = 150.00
SKIP_WORDS = ["lot", "bundle", "mystery", "rep", "replica", "inspired", "dupe", "custom", "handmade", "kids", "toddler", "infant"]  # whole words only

# ---- Your costs when you BUY on Depop (US) ----
DEPOP_BUYER_FEE_PCT = 5.0        # Depop's US marketplace fee is "up to 5% + up to $1" of the item price.
DEPOP_BUYER_FEE_FIXED = 1.00     #   When Depop shows the listing's total price, that exact number is used instead.
BUY_TAX_PCT = 9.75               # sales tax you pay at Depop checkout
DEPOP_SHIPPING_ESTIMATE = {"light": 6.00, "medium": 8.00, "heavy": 12.00}  # only if the listing doesn't show shipping

# ---- What it costs to SELL on eBay ----
EBAY_FEE_PCT = 13.6              # final value fee (most clothing/shoes); charged on price + buyer tax
EBAY_ORDER_FEE_SMALL = 0.30      # orders $10 or less
EBAY_ORDER_FEE = 0.40            # orders over $10
EBAY_AD_RATE_PCT = 5.0           # Promoted Listings rate you plan to use (0 = none)
EBAY_BUYER_TAX_PCT = 8.0         # average tax eBay adds for the buyer (fees are charged on it)
SHIP_TO_EBAY_BUYER = {"light": 5.50, "medium": 8.00, "heavy": 12.00}  # postage you pay (you offer free shipping)
PACKAGING_COST = 0.50

# ---- What counts as a good find ----
MIN_PROFIT = 10.00               # after every cost above
MIN_ROI_PCT = 30                 # profit as % of what you pay on Depop
MIN_COMPS = 3                    # matching eBay listings needed for a price
PREFER_SOLD = True               # try eBay SOLD prices first (eBay may hide them; active prices are the fallback)
ACTIVE_PRICE_HAIRCUT_PCT = 12    # active asking prices are lowered this much (asking > selling)
PRICE_PERCENTILE = 0.35          # 0.35 = a conservative price (35th percentile of matching eBay prices)

# ---- Discord ----
MAX_PHOTOS = 8                   # Depop photos per alert (main + 7), as galleries in one message
MAX_ALERTS_PER_RUN = 40
SEND_UNPRICED = True             # also send listings eBay couldn't price ("TO LIST"), with the eBay price you'd need for MIN_PROFIT
DISCORD_PACE_SECONDS = 0.8
SEND_RUN_SUMMARY = True

# ---- Politeness / memory ----
DELAY_SECONDS = (1.5, 3.5)       # random pause between requests to the same site
USE_BROWSER_FALLBACK = True      # if plain requests get blocked, load the page in Scrapling's stealth browser
BROWSER_TIMEOUT_SECONDS = 25     # longest the browser waits for a page
BROWSER_WAIT_SECONDS = 12        # longest it waits for search results to appear on a loaded page
STOP_AFTER_EMPTY_SEARCHES = 4    # if the first this-many Depop searches all find nothing, stop the run (Depop is blocking)
RUN_MINUTES = 85                 # the scout wraps up by this point (GitHub's limit is set to 110 in scout.yml)
SEARCH_SHARE_OF_TIME = 0.62      # at most this share of the time goes to Depop searches (~53 min); the rest to eBay checks
REMEMBER_ALERTED_DAYS = 30       # don't re-alert the same listing for this long (unless the price drops)
REMEMBER_CHECKED_DAYS = 3        # listings that didn't qualify are re-checked after this long
EBAY_COMP_CACHE_HOURS = 48       # reuse eBay price lookups for the same search this long

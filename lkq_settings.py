# =====================================================================
#  AUTO PARTS SCOUT (LKQ Online -> eBay)  -  SETTINGS
#  Your Discord webhook is NOT here: it goes in GitHub as a secret named AUTO_PARTS_WEBHOOK_URL.
# =====================================================================

# ---- What to search ----
SEARCHES_PER_RUN = 150           # lkq_searches.txt has ~11,000 (vehicle x year x part); 150 x 4 runs = 600 a day
MAX_BUY_PRICE = 1000.00          # most you'll pay LKQ for one part (part + core + shipping + tax)
MIN_PART_PRICE = 25.00           # ignore cheap parts: they can't make $100
MAX_EBAY_CHECKS_PER_RUN = 60     # parts priced on eBay per run: biggest price gaps first

# ---- What you pay LKQ ----
BUY_TAX_PCT = 9.75               # sales tax on your LKQ order
COUNT_CORE_CHARGE = True         # core charges count as cost (you're reselling the part, not returning an old one)
# LKQ shipping to you, used only when LKQ doesn't show it, by part size
LKQ_SHIPPING_ESTIMATE = {"small": 15.00, "medium": 30.00, "large": 75.00, "freight": 175.00}

# ---- What it costs to sell on eBay ----
EBAY_FEE_PCT = 13.6              # eBay Motors parts final value fee (most categories); charged on price + buyer tax
EBAY_AD_RATE_PCT = 3.0           # Promoted Listings rate you plan to use (0 = none)
EBAY_BUYER_TAX_PCT = 8.0
EBAY_ORDER_FEE = 0.40
# shipping the part to your eBay buyer (you offer free shipping), by part size
SHIP_TO_BUYER = {"small": 15.00, "medium": 35.00, "large": 85.00, "freight": 225.00}
PACKAGING = {"small": 2.00, "medium": 6.00, "large": 15.00, "freight": 45.00}

# ---- What gets sent ----
MIN_PROFIT = 100.00              # BUY alert: at least this profit after every cost
SEND_CLOSE_CALLS = True          # also send a few $60-$100 profit finds, marked "CLOSE"
CLOSE_CALL_MIN_PROFIT = 60.00
MAX_CLOSE_CALLS_PER_RUN = 8
MIN_COMPS = 3                    # matching eBay listings needed for a price
PRICE_PERCENTILE = 0.35          # conservative eBay price (35th percentile of matching listings)
ACTIVE_PRICE_HAIRCUT_PCT = 10    # eBay hides sold prices, so active asking prices are lowered this much
MAX_ALERTS_PER_RUN = 40

# ---- Time and memory ----
RUN_MINUTES = 85                 # wraps up by this point (GitHub's limit is 110 in lkq.yml)
SEARCH_SHARE_OF_TIME = 0.55
STOP_AFTER_EMPTY_SEARCHES = 8    # if the first this-many searches all find nothing, stop and save debug files
REMEMBER_ALERTED_DAYS = 14
REMEMBER_CHECKED_DAYS = 4

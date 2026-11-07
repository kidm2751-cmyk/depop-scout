# =====================================================================
#  AMAZON -> EBAY DEAL SCOUT  -  SETTINGS (the only file you need to edit)
#  Your Discord webhook is NOT here: it goes in GitHub as a secret named AMAZON_WEBHOOK_URL.
# =====================================================================

# ---- What to search ----
KEYWORDS_PER_DAY = 1000          # amazon_keywords.txt has 5,000 = 5 daily blocks of 1,000; a new block each day, repeating after Day 5
KEYWORDS_PER_RUN = 250           # 250 per run x 4 runs a day = today's whole block
MIN_AMAZON_PRICE = 3.00
MAX_AMAZON_PRICE = 45.00         # the most you're willing to pay on Amazon
CANDIDATES_PER_KEYWORD = 3       # best listings per search that get a full check
MAX_EBAY_CHECKS_PER_RUN = 50     # listings checked on eBay per run: strongest deals and highest demand first
SKIP_SPONSORED = True            # sponsored results are often off-topic or knockoffs
MIN_RATING = 3.8                 # skip listings rated below this (when Amazon shows a rating)
SKIP_WORDS = ["compatible with", "compatible for", "replacement for", "not lego", "for lego", "building blocks set compatible",
              "refurbished", "renewed", "replica", "knockoff", "dupe", "inspired by",
              "spot it", "jenga", "bop it", "ghost rider", "queen amidala", "hugmees", "dodge pickup", "case ih", "clue",
              "flip 7", "rage mode", "kim possible", "60455", "76442", "kong classic", "prym", "thread snips",
              "pokemon", "trading card", "tcg", "booster pack", "gift card"]

# ---- What counts as a deal (shown on every alert; a deal is NOT required to send an alert) ----
MIN_PCT_OFF = 10                 # "X% off" counts from this much below Amazon's typical / list price
BIG_PCT_OFF = 40                 # "big deal" level (ranks first, and can be sent even when eBay can't be priced)
BIG_COUPON_PCT = 20
MAX_LIST_PCT_OFF = 60            # sellers can type in any "List" price: a bigger "% off list" is treated as fake and ignored
HIGH_DEMAND_BOUGHT = 100         # "100+ bought in past month" = high demand

# ---- Your costs when you BUY on Amazon ----
AMAZON_TAX_PCT = 9.75            # sales tax on your Amazon order
APPLY_COUPONS = True             # coupons you can clip are taken off your cost

# ---- What it costs to SELL on eBay ----
EBAY_FEE_PCT = 13.6              # final value fee (most categories); charged on price + buyer tax
EBAY_AD_RATE_PCT = 8.0           # Promoted Listings rate you plan to use (0 = none)
EBAY_BUYER_TAX_PCT = 8.0         # average tax eBay adds for the buyer (fees are charged on it)
EBAY_ORDER_FEE_SMALL = 0.30      # orders $10 or less
EBAY_ORDER_FEE = 0.40            # orders over $10
SHIP_TO_EBAY_BUYER = 0.00        # 0 = same as your old Deal Scout. If you ship items yourself, put your average postage here.
PACKAGING_COST = 0.00

# ---- What gets sent ----
MIN_PROFIT = 3.00                # BUY alert: at least this profit after every cost
MIN_ROI_PCT = 10                 # ...and this return on what you pay Amazon
MAYBE_MIN_PROFIT = 1.50          # MAYBE alert: thinner profit, or priced from fewer eBay sales
MIN_COMPS = 3                    # matching eBay listings needed for a price
PRICE_PERCENTILE = 0.35          # conservative eBay price (35th percentile of matching listings)
ACTIVE_PRICE_HAIRCUT_PCT = 10    # when only active eBay listings are available, lower them this much
SEND_DEAL_WATCH = True           # also send big deals with high demand that eBay couldn't price (marked "check eBay yourself")
MAX_DEAL_WATCH_PER_RUN = 15
SEND_UNPRICED = True             # also send items eBay couldn't price ("TO LIST"), with the eBay price you'd need for MIN_PROFIT
MAX_ALERTS_PER_RUN = 60

# ---- Time and memory ----
RUN_MINUTES = 85                 # the scout wraps up by this point (GitHub's limit is set to 110 in amazon.yml)
SEARCH_SHARE_OF_TIME = 0.5       # at most this share of the time goes to Amazon searches; the rest to checks
STOP_AFTER_EMPTY_SEARCHES = 4    # if the first this-many searches all find nothing, stop (Amazon is blocking)
REMEMBER_ALERTED_DAYS = 7        # don't re-alert the same product for this long, unless its price drops 5%+
REMEMBER_CHECKED_DAYS = 3        # products that didn't qualify are re-checked after this long

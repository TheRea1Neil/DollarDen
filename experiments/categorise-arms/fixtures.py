"""Transactions to categorise, with what a careful human would pick.

Modelled on Neil's real budget. Ground truth is my judgement, so each
case is marked with how clear-cut it is:

  clear       one obviously right answer; getting it wrong is a failure
  defensible  more than one reasonable answer; any listed one counts
  ambiguous   no right answer from the name alone. Declining is CORRECT
              and guessing is the failure.

That last group is the point. For auto-filing, precision beats recall by
a wide margin: a wrong answer is money in the wrong envelope that nobody
notices, while "unsure" costs one tap in the review queue.
"""

CATEGORIES = [
    dict(name="Tithing", kind="obligation",
         description="Ten percent of gross pay, given to church.",
         matchers=[]),
    dict(name="Rent", kind="obligation",
         description="Monthly rent payment.", matchers=[]),
    dict(name="Health", kind="obligation",
         description="Medical, dental, prescriptions, copays.",
         matchers=[]),
    dict(name="Car Insurance", kind="obligation",
         description="The car insurance premium.", matchers=[]),
    dict(name="Food & Groceries", kind="spending",
         description="Groceries and eating out.",
         matchers=["mcdonald's", "kfc", "starbucks"]),
    dict(name="Short-Term Savings", kind="savings_bucket",
         description="A buffer for irregular costs.",
         matchers=["touchstone climbing", "tectra inc"]),
    dict(name="New Car Fund", kind="savings_bucket",
         description="Saving toward a newer car.",
         matchers=["madison bicycle center"]),
    dict(name="Gas", kind="spending",
         description="Fuel and getting around.",
         matchers=["uber", "united airlines"]),
    dict(name="Date Fund", kind="spending",
         description="Going out together — meals, films, outings.",
         matchers=[]),
    dict(name="Netflix", kind="spending",
         description="The Netflix subscription.", matchers=[]),
]

NAMES = [c["name"] for c in CATEGORIES]

# (merchant, cents out, accepted answers, difficulty)
CASES = [
    # --- unmistakable -------------------------------------------------- #
    ("NETFLIX.COM",                 1599, ["Netflix"], "clear"),
    ("CHEVRON 0093821",             5204, ["Gas"], "clear"),
    ("SHELL OIL 57445208",          4812, ["Gas"], "clear"),
    ("TRADER JOE'S #412",           6713, ["Food & Groceries"], "clear"),
    ("SAFEWAY #1842",               8930, ["Food & Groceries"], "clear"),
    ("WALGREENS PHARMACY #212",     2450, ["Health"], "clear"),
    ("QUEST DIAGNOSTICS",          11200, ["Health"], "clear"),
    ("GEICO AUTO PAYMENT",         14300, ["Car Insurance"], "clear"),
    ("CHIPOTLE MEXICAN GRILL",      1450, ["Food & Groceries", "Date Fund"],
     "defensible"),
    ("UBER TRIP 4KX2M",             1875, ["Gas"], "clear"),

    # --- needs the description, not just the name ---------------------- #
    ("AMC THEATRES 4412",           3250, ["Date Fund"], "defensible"),
    ("OLIVE GARDEN 00492",          6820, ["Date Fund", "Food & Groceries"],
     "defensible"),
    ("CVS/PHARMACY #8871",          1899, ["Health", "Food & Groceries"],
     "defensible"),
    ("PROPERTY MGMT CO ACH",       35000, ["Rent"], "clear"),

    # --- declining is the right answer --------------------------------- #
    ("SQ *MERCHANT 4821",           4200, [], "ambiguous"),
    ("PAYPAL *TRANSFER",            7500, [], "ambiguous"),
    ("ACH DEBIT 88291002",          9100, [], "ambiguous"),
    ("AMZN MKTP US*2K4JD",          3499, [], "ambiguous"),
    ("VENMO PAYMENT 110392",        2000, [], "ambiguous"),
    ("POS PURCHASE TERMINAL 02",    1560, [], "ambiguous"),
]

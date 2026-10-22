"""Three kinds of description, and charges that tell them apart.

The first version of this experiment was unfair to descriptions: the
ones I wrote restated the name ("Tithing: ten percent of gross pay"),
which carries nothing the word "Tithing" did not. Unsurprisingly they
did not help, and on one opaque input the extra words invited a
rationalisation.

What Neil actually meant is nuance a name cannot hold: WHO the payee is,
and where the boundary with a neighbouring category falls. So there are
now three arms, and cases that only the third can get right.

  plain    name and kind only
  generic  descriptions that restate the name — the straw man
  nuanced  descriptions naming real payees and real boundaries

Ground truth per case:
  clear       one right answer from the merchant name alone
  needs_desc  only answerable if the description names the payee or the
              boundary. The point of the whole exercise.
  conflict    the obvious guess is WRONG and the description says so.
              Tests whether a description actually steers the model or
              is just decoration.
  ambiguous   no answer is available. Declining is correct; guessing is
              the failure.
"""

_BASE = [
    ("Tithing", "obligation"),
    ("Rent", "obligation"),
    ("Health", "obligation"),
    ("Car Insurance", "obligation"),
    ("Food & Groceries", "spending"),
    ("Short-Term Savings", "savings_bucket"),
    ("New Car Fund", "savings_bucket"),
    ("Gas", "spending"),
    ("Date Fund", "spending"),
    ("Netflix", "spending"),
]

GENERIC = {
    "Tithing": "Ten percent of gross pay, given to church.",
    "Rent": "Monthly rent payment.",
    "Health": "Medical, dental, prescriptions, copays.",
    "Car Insurance": "The car insurance premium.",
    "Food & Groceries": "Groceries and eating out.",
    "Short-Term Savings": "A buffer for irregular costs.",
    "New Car Fund": "Saving toward a newer car.",
    "Gas": "Fuel and getting around.",
    "Date Fund": "Going out together.",
    "Netflix": "The Netflix subscription.",
}

# Names the payee, and says where the edge is. This is the thing being
# tested: whether nuance a name cannot hold actually earns its place.
NUANCED = {
    "Tithing": "Always goes to Church of Jesus Christ Donation — that is "
               "the only thing that comes out of here.",
    "Rent": "Paid by ACH to Lakeview Property Management on the 1st. "
            "Nothing else.",
    "Health": "Copays, prescriptions and dental. Dr Ramirez, Quest, and "
              "pharmacy counters. Vitamins bought with the groceries are "
              "Food, not here.",
    "Car Insurance": "The GEICO premium only.",
    "Food & Groceries": "Groceries from a shop, and weekday lunches on my "
                        "own. Eating out together comes out of Date Fund.",
    "Short-Term Savings": "A buffer for irregular costs — car repairs, "
                          "vet bills, replacing something that broke.",
    "New Car Fund": "Saving toward a newer car. Also bike parts and "
                    "cycling gear, since that is the same goal of driving "
                    "less.",
    "Gas": "Fuel at a pump, nothing else. Uber and Lyft rides come out of "
           "Date Fund.",
    "Date Fund": "Going out together — restaurants, films, outings, and "
                 "the rideshare to get there.",
    "Netflix": "The Netflix subscription. Other streaming is Date Fund.",
}


def categories(arm: str) -> list[dict]:
    """Build the category list for one arm."""
    table = {"plain": None, "generic": GENERIC, "nuanced": NUANCED}[arm]
    return [
        dict(name=n, kind=k, description=("" if table is None else table[n]))
        for n, k in _BASE
    ]


NAMES = [n for n, _ in _BASE]

# (merchant, cents, accepted answers, difficulty)
CASES = [
    # --- answerable from the merchant name ----------------------------- #
    ("NETFLIX.COM",                 1599, ["Netflix"], "clear"),
    ("CHEVRON 0093821",             5204, ["Gas"], "clear"),
    ("TRADER JOE'S #412",           6713, ["Food & Groceries"], "clear"),
    ("SAFEWAY #1842",               8930, ["Food & Groceries"], "clear"),
    ("QUEST DIAGNOSTICS",          11200, ["Health"], "clear"),
    ("GEICO AUTO PAYMENT",         14300, ["Car Insurance"], "clear"),

    # --- only the payee-naming description gets these ------------------ #
    ("CHURCH OF JESUS CHRIST DONATION", 55000, ["Tithing"], "needs_desc"),
    ("LAKEVIEW PROPERTY MGMT ACH",      35000, ["Rent"], "needs_desc"),
    ("DR RAMIREZ DDS",                   9500, ["Health"], "needs_desc"),
    ("MADISON BICYCLE CENTER",          18500, ["New Car Fund"], "needs_desc"),

    # --- the obvious guess is wrong, and the description says so ------- #
    ("UBER TRIP 4KX2M",             1875, ["Date Fund"], "conflict"),
    ("LYFT RIDE SUN 11PM",          2240, ["Date Fund"], "conflict"),
    ("HULU SUBSCRIPTION",           1799, ["Date Fund"], "conflict"),
    ("OLIVE GARDEN 00492",          6820, ["Date Fund"], "conflict"),

    # --- declining is correct ------------------------------------------ #
    ("SQ *MERCHANT 4821",           4200, [], "ambiguous"),
    ("PAYPAL *TRANSFER",            7500, [], "ambiguous"),
    ("ACH DEBIT 88291002",          9100, [], "ambiguous"),
    ("AMZN MKTP US*2K4JD",          3499, [], "ambiguous"),
    ("VENMO PAYMENT 110392",        2000, [], "ambiguous"),
    ("POS PURCHASE TERMINAL 02",    1560, [], "ambiguous"),
]

# Clean-slate walkthrough

A fresh account, categories added one at a time, a paycheck split, then
expenses filed — noting everything that is harder than it should be.

Findings are numbered as they are found and judged at the end, so the
ordering reflects what a new user hits first.

## 1. Sign out has no confirmation

One tap on the icon in the app bar and you are signed out, with no
"are you sure". It sits next to two other icons you tap often (History,
New category), so a mis-tap logs you out and you have to find your
password again. Low cost to fix, and the icon is in the wrong
neighbourhood regardless — signing out is not a peer of "add a
category".

## 2. Validation errors are shown as raw backend dumps

Registering with an address the server rejects shows this, verbatim, in
the form:

```
[{type: value_error, loc: [body, email], msg: value is not a valid
email address: The part after the @-sign is not valid. It should have
a period..., input: ..., ctx: {...}}]
```

That is FastAPI's 422 body rendered as a string. A user cannot read it,
and it leaks the shape of the API. Every 422 in the app will do this,
not just registration — the client does not translate validation
errors, it stringifies them.

Fix: the client should unpack a 422 into its `msg` fields, and the form
should show the first one against the field it names. Worth doing once
in `ApiException` so every screen benefits.

## 3. The empty account leads with a meaningless number

"Allocated $0.00" occupies the largest, highest position on a brand new
account. It is the first thing a new user reads and it tells them
nothing — there is nothing to allocate and no categories to allocate
into. The empty state below it is doing the real work.

On an account with no categories the summary card should not be there
at all; the empty state should own the screen.

## 4. The Paycheck button is offered before it can work

The floating Paycheck button is enabled on an empty account. There are
no categories and no split plan, so it can only fail. Either disable it
until a plan exists, or have it explain what is missing and offer to
fix it.

## 5. Three unlabelled icons, one of them destructive

History, New category and Sign out sit together as bare icons. The
first two are things you do often; the third logs you out with no
confirmation (finding 1). They do not belong in the same cluster.

## 6. New categories all land in the same place

Categories created through the app are listed alphabetically, not in the
order they were added: Tithing, Rent, Groceries, Gas came back as Gas,
Groceries, Rent, Tithing. The app never sends a `sort_order`, so every
category is created with 0 and the name breaks the tie.

A list that reorders itself while you are building it is disorienting,
and it quietly undoes the Reorder feature — drag something, add a
category, and the new one does not go to the end, it goes wherever its
name falls.

Fix: default `sort_order` to the current category count on create.

## 7. You cannot say how a category is funded while creating it

The create form has name, kind, both descriptions, target and alert —
but no funding rule. You create the category, then reopen it, then go
to Settings, then set the rule. For a user building a budget from
nothing that is four extra steps per category, and nothing on the
create screen hints that funding is still to come.

## 8. The funding rule is the last thing on the screen

Setting how a category is funded takes: tap the category, tap Settings,
then **six scrolls** past name, kind, two descriptions, target and alert
threshold before Funding appears.

It is the most consequential setting on the page — it decides whether
the category gets money at all — and it is below the one that only
changes a label colour.

Order should be roughly: name and kind, funding, what gets charged here,
target and alert, what it is for. Right now it is almost exactly
reversed.

## 9. BLOCKING — a new user cannot set up funding by hand at all

Setting a funding rule on a fresh account fails:

```
409  There is no active split plan yet. Create one before setting how a
     category is funded.
```

There is no way to create a split plan from the app except by accepting
a whole-budget proposal from the assistant. So the flow Neil asked to
test — add categories one at a time, give each a good description, set
each one's funding — **cannot be completed**. Every Save silently did
nothing; six categories exist and not one of them is funded.

The chicken-and-egg: you need a plan before a category can be funded,
and the only way to get a plan is to let the AI write the whole thing.

Worse, the app does not make the failure obvious. The Save button
returns you to the list as though it worked, and nothing on the category
screen says it is unfunded.

Fix: setting funding on the first category should CREATE the plan.
A plan needs exactly one remainder rule, so the new plan gets the rule
the user just set plus a remainder pointing at Unallocated — which
already exists for precisely this purpose, holding money that has no
home yet. The user can move the remainder later.

This also means the whole manual path has never been exercised. It was
built, tested at the endpoint level with a plan already present, and
never walked from zero.
## 10. The paycheck preview buries the split under explanation

Above the first category row there is: the amount field, its helper, the
"Show me what it does" button, a "Set aside first" heading with Add
button, two lines explaining set-asides, and two more explaining
dragging. That is roughly half a phone screen of prose before the thing
you opened the screen to look at.

The split itself is good — clear rows, before → after, a drag handle and
an edit affordance each.

Suggestions: collapse "Set aside first" to a single line until used;
move the dragging explanation to a one-time hint or drop it (the drag
handles say it); and once a preview exists, "Show me what it does" is
spent — it should become a quiet "Recalculate" or disappear.

## 11. The amount field does not format what you typed

Typing `2000` leaves `$2000`. Everywhere else in the app shows
`$2,000.00`. It should format on blur, both to confirm what was
understood and to match the rest of the app.

## 12. The Unallocated scaffold lingers at $0.00

Creating the first funding rule adds an Unallocated category to hold the
remainder. When a real category later takes the remainder — Savings, here
— Unallocated is left in the list showing $0.00 forever.

It is a real system category and reconciliation needs it, so it should
not be deleted. But a $0.00 row that the user never chose and cannot
meaningfully act on is noise in the one list they read most.

Hide it from the budget list while it is empty and holds no rule.

## 13. Careful descriptions do nothing for automatic filing

Six categories, each with a specific "what gets charged here" naming
real merchants — *"Groceries from a supermarket — Trader Joe's,
Safeway, Kroger"*, *"Fuel at a pump — Chevron, Shell, Costco Gas"*,
*"Always goes to Church of Jesus Christ Donation"*.

Then twelve charges, including TRADER JOE'S, SAFEWAY, SHELL OIL,
CHEVRON and CHURCH OF JESUS CHRIST DONATION.

**All twelve went to review. Not one was filed automatically.**

The deterministic categoriser only consults matcher rules
(`category_rules`), and a new account has none. Descriptions are read by
the AI and by nothing else, so a user who writes careful boundaries gets
exactly the same result as one who writes nothing.

The feeling this produces is the worst kind: you did the work the app
asked for, and the app ignored it.

Two ways out, and they are not exclusive:

- **Offer matchers from what was written.** When a description names
  merchants, propose them as matcher rules to confirm. One tap turns
  *"Trader Joe's, Safeway, Kroger"* into three rules that file
  deterministically forever.
- **Say what will happen.** If the app is going to send everything to
  review on day one, the category screen should say so rather than
  implying the description does the filing.

The assisted review does use descriptions and does work — but a new user
has to discover it, and until they do the app looks like it ignored
everything they typed.
## 14. Edits are lost silently if you leave without scrolling to Save

The Settings tab is taller than the screen and Save is at the bottom.
Edit the description, tap back, and the change is gone — no prompt, no
warning, no indication anything was pending.

This is how three categories ended up with no "what gets charged here"
during this walkthrough: the text was typed and visible on screen, and
then thrown away.

It is worse than an ordinary unsaved-changes bug because the field that
is easiest to lose is the one furthest down, and because the screen
gives no sign that Save exists until you scroll past the funding
editor.

Fix, in order of preference:
- save on change, like the funding editor already effectively does, and
  drop the button entirely; or
- a sticky Save bar at the bottom that appears once anything is dirty;
  and either way
- warn on back-navigation when there are unsaved changes.

## 15. With good descriptions, the assistant is genuinely good

Once all six categories had a "what gets charged here", the assistant
got **9 of 12** right, each with visible, checkable evidence:

```
TRADER JOE'S #412        Groceries   Groceries mentions "trader"
CHEVRON 0093821          Gas         quote: "Fuel at a pump - Chevron, Shell..."
SHELL OIL 57445208       Gas         quote: "Fuel at a pump - Chevron, Shell..."
CHURCH OF JESUS CHRIST   Tithing     quote: "Always goes to Church of Jesus..."
LAKEVIEW PROPERTY MGMT   Rent        quote: "Paid by ACH to Lakeview Property..."
VCA ANIMAL HOSPITAL      Savings     quote: "Vet bills"
UBER TRIP 8832K          Date Fund   Date Fund mentions "uber"
OLIVE GARDEN 00492       Date Fund   Date Fund mentions "garden"
SAFEWAY #1842            Groceries   Groceries mentions "safeway"
```

Both hard classes worked. `LAKEVIEW PROPERTY MGMT ACH` is unguessable
from the name and landed on Rent because the description names the
payee. `UBER TRIP` went to Date Fund rather than Gas because the
description says so — overriding the obvious answer.

This is the payoff for the descriptions, and it is large.

## 16. But it stopped declining entirely

All three opaque charges got confident answers:

```
SQ *MERCHANT 4821        Date Fund   quote: "Eating out together, films..."
VENMO PAYMENT 110392     Date Fund   merchant name alone
AMZN MKTP US*2K4JD       Date Fund   quote: "Eating out together, films..."
```

**Declined 0 of 3.** In `experiments/categorise-arms` the same model
declined 4–6 of 6 on the same kind of input. The difference is the
category set: six categories, every one with a rich description, and one
of them ("outings", "films", "going out together") broad enough to
rationalise anything into. UNSURE gets crowded out when every option
looks plausible.

## 17. A quote is not evidence that the quote is relevant

Worse, two of those three were presented as *backed by a quote*. The
verification only checks that the quoted words really appear in that
category's description — not that they have anything to do with the
charge. "Eating out together, films, outings" is a real sentence from
Date Fund, and it says nothing about `SQ *MERCHANT 4821`.

So the UI shows them as confident-with-evidence, which is exactly the
reassurance they do not deserve.

The computed `mentions` path does not have this problem: it only fires
when a word from the charge actually appears in a description. The fix
is to hold quotes to the same standard — a quote should only count as
evidence if it ALSO names something in the charge. Otherwise show it as
"the assistant's own reasoning", which is what it is.

That single change would mark all three of these as unbacked, and the
"confident, but a guess" chip would do its job.

---

# What to fix, in order

**Blocking — the manual path did not work at all**

1. **(9) Funding could not be set on a fresh account.** 409, every time,
   with no plan to attach to and no way to make one except the AI.
   *Fixed during this walkthrough* — the first rule now creates the plan
   with a remainder into Unallocated.

**Silent data loss**

2. **(14) Settings discards edits if you leave without scrolling to
   Save.** Cost three categories their descriptions during this
   session, twice, and I was watching for it.

**Misleading**

3. **(17) A quote counts as evidence even when irrelevant.** Three wrong
   suggestions were presented as backed by a description.
4. **(2) Raw FastAPI validation dumps shown to the user.** Affects every
   422 in the app, not just registration.
5. **(16) The assistant stopped declining** once every category had a
   rich description. Worth re-measuring with a broad catch-all category
   present, because that is what real budgets have.

**The work you did is ignored**

6. **(13) Descriptions do nothing for automatic filing.** Name
   Trader Joe's in a description and a Trader Joe's charge still goes to
   review. Offer to turn named merchants into matcher rules.

**Friction**

7. **(7, 8) Funding is unreachable while creating a category and buried
   when editing one** — six scrolls, below the alert threshold.
8. **(10) The paycheck preview puts half a screen of prose above the
   split.**
9. **(6) New categories sort alphabetically**, so the list reorders
   itself while you build it.
10. **(3, 4) The empty account leads with "$0.00"** and offers a
    Paycheck button that cannot work.

**Small**

11. **(1) Sign out has no confirmation** and sits beside two buttons you
    tap often.
12. **(11) The amount field does not format what you typed.**
13. **(12) The Unallocated scaffold stays visible at $0.00.**

# What already works well

- The paycheck preview itself: clear rows, before → after, drag handles,
  an edit affordance, and the totals reconciling exactly.
- Direction of money is unmistakable — the arrow, sign and colour.
- The assisted review, when descriptions exist, with honest attribution.
- The category screen opening on Activity rather than a form.
- "Funded 1 of 6" with the explanation of what position means.

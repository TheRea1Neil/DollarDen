# 01 — Overview & Goals

## Problem

Commercial budgeting apps are built around *reporting*: they show where money
went. The mental model actually in use here is *allocation*: income arrives in
irregular lumps every ~2 weeks and is immediately divided into purposes. The
useful question is not "how much did I spend on food in October" but "how much
is left in Food before the next paycheck, and when is that paycheck".

A second problem is privacy. Handing a full transaction history to a
third-party LLM is a meaningful disclosure. All inference here runs on hardware
the user controls.

## What it does

1. **Connects to real accounts** via Plaid — credit and debit — and keeps
   transactions in sync.
2. **Models categories as balances, not reports.** Food is not "spent $212 this
   month"; Food is "$88 remaining of $300". Categories are created, renamed, and
   deleted freely as circumstances change.
3. **Splits each paycheck by ordered rules** the user writes and edits over
   time. Rules are expressive enough for: percentage-first (tithing), fixed
   amounts (rent), conditional top-ups ("if car insurance is under $450, add
   $50"), refill-to-target ("bring Food back to $300"), and a remainder sink.
4. **Draws spending back out of the right category**, automatically where
   confident, and via a review queue where not.
5. **Splits receipts** — photograph a receipt, and line items are allocated
   across categories rather than dumping the whole total into one.
6. **Warns early** — threshold alerts ("Food is under $200") paired with a
   predicted next-paycheck date, so the warning is actionable.
7. **Analyses against stated goals** — the user writes what they are trying to
   achieve; summaries and suggestions are generated against those goals rather
   than generic advice.
8. **Designs categories on request** — describe a goal, get a proposed set of
   categories and paycheck rules, then edit them.

## Users

- **Primary:** one user, one or more linked institutions.
- **Secondary:** a second user (brother) with his own bank connections and his
  own data, on the same deployment.

Multi-tenancy is built in from the first schema migration. Retrofitting
`user_id` onto a single-user schema later is the kind of change that quietly
corrupts financial data, and it is nearly free to do now.

## Non-goals

Explicitly out of scope, to keep the build finite:

- Investment tracking, net worth, or portfolio analysis.
- Bill payment, money movement, or any write access to accounts. **Read-only.**
- Tax preparation or reporting.
- Shared/joint budgets. Each user's data is their own.
- Public multi-tenant SaaS. This is a self-hosted app for a known handful of
  people, and the security model assumes that.
- Web frontend. Mobile only, though the API is HTTP and a web client could be
  added later.

## Constraints that shape the design

| Constraint | Consequence |
|---|---|
| 10 Plaid production connections, consumed per link | Sandbox-first development is mandatory; token durability is a first-class requirement (docs/04) |
| Pi has ~3.7 GB RAM, arm64 | Cannot host an LLM. Inference is offloaded (docs/05) |
| AI host is asleep by default | Every AI call must tolerate a ~10 s cold start, and be async |
| Residential dynamic IP | DuckDNS + a webhook endpoint that tolerates IP changes |
| Phone may be offline | SQLite mirror, with an explicit sync contract (docs/03) |
| Money | Integer cents everywhere. Never floats. Non-negotiable |

## Definition of done for v1

A paycheck lands; the app splits it per the user's rules without being asked;
spending over the next two weeks draws down the right categories; anything
ambiguous appears in a review queue; a threshold alert fires before Food runs
out; and an end-of-period summary is generated locally against stated goals.

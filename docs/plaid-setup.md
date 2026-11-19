# Connecting a bank

Plaid is how this reaches your bank. Read this before signing up for
anything: one step spends a resource you cannot get back, and the
order matters more than it looks.

## The one thing to understand first

Plaid's **Trial** plan gives you a small number of real bank
connections. That number is a **lifetime allowance, not a monthly
one**.

Every attempt against a real bank spends one. Removing a connection
afterwards does **not** give it back. A typo in your Plaid dashboard —
a wrong redirect URI, a missing package name — costs one permanently,
and you will not find out until the link silently dead-ends at the
bank's login page.

This is why the whole of step 1 exists.

## 1. Everything in sandbox first

Plaid's sandbox is free, unlimited, and behaves like the real thing
with fake banks. Nothing you do here costs anything.

1. Sign up at [plaid.com](https://plaid.com) and open the dashboard.
   You get a **client ID** and a **sandbox secret** immediately.
2. In your admin portal, under **Bank**, set:
   - Environment: `sandbox`
   - Client ID and Secret from the dashboard
3. Create a **sandbox account** for yourself under **Accounts** —
   separate from the account holding your real budget. Sign into the
   app with it.
4. Connect a fake bank. Plaid's test institutions are called things
   like "First Platypus Bank"; the sandbox credentials are
   `user_good` / `pass_good`.
5. **Use it properly.** Let transactions arrive. Categorise some.
   Split a paycheck. Check the budget adds up. Break things.

Do not move on until that whole loop works, including webhooks if you
have set up HTTPS. Every problem you find here is free; the same
problem found later costs a connection.

## 2. Then apply for Trial — not production

In the Plaid dashboard, request the **Trial** plan.

**Do not apply for production access.** Production is a commercial
agreement with per-connection billing, meant for businesses. Trial is
what a household wants: a handful of real connections, no cost.

You will get a **production secret**, separate from your sandbox one.
Your client ID stays the same.

## 3. Connect your real bank

1. In the admin portal, enter the production secret. Sandbox and
   production secrets are stored separately, so the sandbox setup you
   just did keeps working.
2. Create a **production** account under **Accounts**. An account's
   environment is fixed when it is created and cannot be changed
   afterwards — a sandbox connection means nothing in production, so
   an account that could switch would strand every bank it had linked.
3. Sign into the app with that account and connect your bank.

**Check the details before you press connect.** That is the moment a
connection is spent.

Keep the sandbox account. It costs nothing and it is where to try
anything risky from now on.

## Mobile banks and OAuth

Most large banks use OAuth, which sends you to the bank's own app or
website and back. Getting back requires exactly one of two settings,
and they are **mutually exclusive**:

| Platform | Setting |
|---|---|
| Android | `plaid_android_package_name` — your app's package name |
| iOS | `plaid_redirect_uri` — a registered universal link |

Sending both, or the wrong one, does not produce an error. The user
picks their accounts, lands back at the bank's login page, and no
connection is created — while a connection has already been spent.

Set the matching one in the admin portal, and register it in the Plaid
dashboard too.

## Webhooks

Plaid tells your server when something has changed. It will only post
to an **https** address reachable from the internet.

Without one, the server pulls on a schedule instead — hourly by
default. Plaid itself only reads most banks a few times a day, so an
hourly pull is close to what webhooks would have told you anyway. You
lose immediacy, not data.

If you add HTTPS later: set the webhook address in the admin portal
and press **re-point webhooks**. That moves connections you already
have, without re-linking them, which is the difference between free
and spending your allowance.

The environment is appended automatically, so
`https://budget.example.org/plaid/webhook` becomes
`.../plaid/webhook/production` for real accounts and `.../sandbox` for
sandbox ones. Your reverse proxy must pass the whole path through: a
rule matching only the exact path will 404 the rest, and a 404 is
invisible from Plaid's side — the connection stays healthy and
transactions simply stop arriving.

## If a connection breaks

Banks expire access periodically, and the app will say a connection
needs attention. Reconnecting an existing bank uses Plaid's **update
mode**, which does **not** spend a connection. Only new banks do.

## What this costs

| | |
|---|---|
| Sandbox | Free, unlimited, forever |
| Trial | Free, a small lifetime number of real connections |
| Reconnecting an existing bank | Free |
| Re-pointing webhooks | Free |
| Connecting a new real bank | One connection, permanently |

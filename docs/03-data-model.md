# 03 — Data Model

## Ground rules

1. **Money is `BIGINT` cents.** Never `FLOAT`, never `DOUBLE`. `0.1 + 0.2`
   is not `0.3`, and a budgeting app that drifts by a cent per operation is
   worthless. Postgres `NUMERIC` would also be correct; integer cents is chosen
   because it survives the trip through JSON and SQLite unchanged.
2. **Every user-owned table carries `user_id`.** From migration 0001.
3. **Transactions from Plaid are immutable facts.** Allocation decisions are
   separate rows. Re-categorising never edits the transaction.
4. **UTC timestamptz everywhere**, with a per-user IANA timezone for display
   and for deciding what "this pay period" means.

## Postgres schema (source of truth)

### Identity

```sql
users (
  id              uuid primary key,
  email           citext unique not null,
  password_hash   text not null,          -- argon2id
  display_name    text,
  timezone        text not null default 'UTC',
  created_at      timestamptz not null default now()
)

sessions (
  id              uuid primary key,
  user_id         uuid not null references users on delete cascade,
  refresh_token_hash text not null,       -- store the hash, never the token
  expires_at      timestamptz not null,
  revoked_at      timestamptz,
  user_agent      text
)
```

### Plaid

```sql
plaid_items (
  id                      uuid primary key,
  user_id                 uuid not null references users on delete cascade,
  plaid_item_id           text unique not null,
  institution_id          text,
  institution_name        text,
  access_token_encrypted  bytea not null,   -- see docs/07
  sync_cursor             text,             -- /transactions/sync position
  status                  text not null default 'healthy',
                            -- healthy | login_required | pending_expiration | error
  last_synced_at          timestamptz,
  consent_expires_at      timestamptz,
  created_at              timestamptz not null default now()
)

accounts (
  id               uuid primary key,
  user_id          uuid not null references users on delete cascade,
  item_id          uuid not null references plaid_items on delete cascade,
  plaid_account_id text unique not null,
  name             text,
  official_name    text,
  mask             text,
  type             text,        -- depository | credit | loan | investment
  subtype          text,        -- checking | savings | credit card | ...
  current_cents    bigint,
  available_cents  bigint,
  is_tracked       boolean not null default true,
  balance_as_of    timestamptz
)

transactions (
  id                     uuid primary key,
  user_id                uuid not null references users on delete cascade,
  account_id             uuid not null references accounts on delete cascade,
  plaid_transaction_id   text unique not null,   -- idempotency key
  amount_cents           bigint not null,        -- Plaid sign: positive = money out
  iso_currency_code      text default 'USD',
  date                   date not null,
  datetime               timestamptz,
  name                   text not null,
  merchant_name          text,
  pending                boolean not null default false,
  pending_replaced_by    text,     -- plaid_transaction_id of the posted version
  pfc_primary            text,     -- Plaid personal_finance_category
  pfc_detailed           text,
  removed_at             timestamptz,            -- soft delete from sync
  created_at             timestamptz not null default now()
)
create index on transactions (user_id, date desc);
create index on transactions (user_id, pending) where removed_at is null;
```

> **Sign convention.** Plaid returns *positive* amounts for money leaving the
> account. This is the opposite of most people's intuition and the single
> easiest place to introduce a sign bug. Normalise once at the sync boundary
> into an internal convention (`outflow_cents` positive, `inflow_cents`
> positive, never one signed column), and write a test that asserts it.

### Budget

```sql
categories (
  id               uuid primary key,
  user_id          uuid not null references users on delete cascade,
  name             text not null,
  kind             text not null,   -- spending | savings_bucket | obligation
  balance_cents    bigint not null default 0,
  target_cents     bigint,          -- refill target: Food -> 30000
  alert_at_cents   bigint,          -- notify when balance drops below
  sort_order       int not null default 0,
  archived_at      timestamptz,     -- soft delete: keeps history intact
  created_at       timestamptz not null default now(),
  unique (user_id, name) where archived_at is null
)
```

Categories are **archived, never deleted**. Deleting one would orphan the
allocation history that explains past balances.

```sql
-- Double-entry-ish ledger. A category's balance is the sum of its entries.
-- Never update categories.balance_cents directly outside this ledger.
allocations (
  id             uuid primary key,
  user_id        uuid not null references users on delete cascade,
  category_id    uuid not null references categories,
  transaction_id uuid references transactions,   -- null for paycheck splits
  paycheck_id    uuid references paychecks,
  amount_cents   bigint not null,   -- positive = into category, negative = out
  source         text not null,     -- paycheck_rule | auto_categorise | manual
                                    -- | receipt_split | correction
  confidence     numeric(3,2),      -- null for deterministic sources
  note           text,
  created_at     timestamptz not null default now()
)
create index on allocations (user_id, category_id, created_at desc);
```

A single transaction may produce several allocations — that is how receipt
splitting works.

```sql
category_rules (
  id           uuid primary key,
  user_id      uuid not null references users on delete cascade,
  category_id  uuid not null references categories on delete cascade,
  matcher      jsonb not null,   -- {merchant_contains, pfc_detailed, amount_between, account_id}
  priority     int not null default 100,
  enabled      boolean not null default true
)

paychecks (
  id              uuid primary key,
  user_id         uuid not null references users on delete cascade,
  transaction_id  uuid references transactions,  -- the deposit, if matched
  gross_cents     bigint not null,
  received_on     date not null,
  split_plan_id   uuid references split_plans,
  applied_at      timestamptz,
  created_at      timestamptz not null default now()
)

-- Versioned so historical paychecks still explain themselves after the
-- user edits their rules.
split_plans (
  id          uuid primary key,
  user_id     uuid not null references users on delete cascade,
  name        text not null,
  version     int not null default 1,
  is_active   boolean not null default true,
  created_at  timestamptz not null default now()
)

split_rules (
  id            uuid primary key,
  plan_id       uuid not null references split_plans on delete cascade,
  category_id   uuid not null references categories,
  order_index   int not null,          -- evaluation order matters: tithing first
  rule_type     text not null,         -- percent | fixed | refill_to | conditional | remainder
  params        jsonb not null,
  enabled       boolean not null default true
)
```

See docs/06 for rule semantics.

### Review queue, receipts, AI

```sql
review_items (
  id              uuid primary key,
  user_id         uuid not null references users on delete cascade,
  transaction_id  uuid not null references transactions on delete cascade,
  reason          text not null,      -- low_confidence | multi_category | unknown_merchant
  suggestions     jsonb,              -- [{category_id, amount_cents, confidence, rationale}]
  resolved_at     timestamptz,
  resolution      jsonb
)

receipts (
  id              uuid primary key,
  user_id         uuid not null references users on delete cascade,
  transaction_id  uuid references transactions,
  image_path      text not null,      -- on-disk, not in the DB
  ocr_text        text,
  line_items      jsonb,              -- [{description, amount_cents, category_id}]
  status          text not null default 'pending',
  created_at      timestamptz not null default now()
)

ai_jobs (
  id            uuid primary key,
  user_id       uuid not null references users on delete cascade,
  kind          text not null,   -- categorise | summary | advise | design_categories | receipt_parse
  status        text not null,   -- queued | waking | running | done | failed
  provider      text,            -- deck | custom
  request       jsonb not null,
  result        jsonb,
  error         text,
  created_at    timestamptz not null default now(),
  completed_at  timestamptz
)

user_goals (
  id          uuid primary key,
  user_id     uuid not null references users on delete cascade,
  body        text not null,     -- free text; fed to the model as context
  updated_at  timestamptz not null default now()
)
```

## SQLite mirror (on the phone)

**Purpose: read-anywhere plus resilience, not independent operation.** The
phone is a cache with an outbox. It does not run the rule engine, does not talk
to Plaid, and does not resolve conflicts on its own.

Mirrored read-only: `categories`, recent `transactions` (rolling ~180 days),
`allocations` for those transactions, `paychecks`, `split_plans`,
`split_rules`, `review_items`, `user_goals`.

Not mirrored, ever: `plaid_items`, anything token-shaped, other users' rows.

```sql
sync_state ( table_name text primary key, last_server_seq integer, synced_at integer )

outbox (
  id integer primary key autoincrement,
  op_id text unique not null,      -- client-generated uuid = idempotency key
  entity text not null,
  payload text not null,           -- json
  created_at integer not null,
  attempts integer not null default 0,
  last_error text
)
```

### Sync contract

- Every mutable server row carries a monotonic `seq` (bigint, per user).
  Pull is `GET /sync?since=<seq>`, which is cheap and resumable.
- Writes go to the `outbox` first, then to `POST /sync/ops` carrying `op_id`.
  The server deduplicates on `op_id`, so retries are safe.
- **Conflicts resolve server-side, last-write-wins per field**, with one
  exception: allocations are append-only, so two offline edits produce two
  correction entries rather than a lost update. This is deliberate — losing a
  financial edit silently is worse than showing the user two corrections.
- The app shows a clear "offline — N pending changes" state. Silent queues
  erode trust in a money app.

## Migrations

Alembic, forward-only, one concern per migration. The first migration creates
the full multi-user schema. No migration ever drops a column containing
financial history without an explicit archive step.

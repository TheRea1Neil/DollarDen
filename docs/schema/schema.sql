-- =====================================================================
-- dollarden -- full database schema
--
-- Dumped from the LIVE database on the Pi, not written by hand, so it
-- is what actually exists rather than what was intended. Regenerate
-- after any migration:
--
--     scripts/dump-schema.sh
--
-- Read docs/14-system-map.md for how the tables relate and which
-- process writes each one. Money is INTEGER CENTS everywhere (ADR
-- 0002) -- there is no float or numeric column holding an amount.
-- =====================================================================


--========================================================================
-- accounts
--========================================================================
-- One account inside a connection: a checking account, a credit card.
-- is_tracked is the user's choice of which accounts this budget covers --
-- loans, pensions and investments are usually left out, and untracked
-- accounts contribute nothing to the allocatable total.
--
--   is_tracked: Whether this account counts toward the budget. Untracked
--               accounts are still synced and visible but contribute nothing
--               to the allocatable total.

CREATE TABLE public.accounts (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    item_id uuid NOT NULL,
    plaid_account_id character varying(128) NOT NULL,
    name character varying(200),
    official_name character varying(200),
    mask character varying(16),
    type character varying(32),
    subtype character varying(48),
    current_cents bigint,
    available_cents bigint,
    is_tracked boolean NOT NULL,
    balance_as_of timestamp with time zone
);

--========================================================================
-- ai_jobs
--========================================================================
-- The queue. Every AI call is a persisted job rather than a blocking
-- request, because the model host is a gaming PC that sleeps and may be
-- busy. A job carries its own request and result, so what was asked and
-- what came back are both still inspectable afterwards. status 'deferred'
-- plus not_before is how a job waits for the GPU instead of failing.
--
--   not_before: Set when a job is deferred because the GPU was busy or it is
--               inside the 01:00-08:00 America/Denver quiet window. The
--               worker that already polls for queued work picks it up again
--               after this time.
--
--   attempts: Counts deferrals so a job cannot wait forever. Past the
--             deadline it runs anyway: a stuttered frame costs less than a
--             transaction that never gets categorised.

CREATE TABLE public.ai_jobs (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    kind character varying(32) NOT NULL,
    status character varying(16) NOT NULL,
    provider character varying(16),
    request jsonb NOT NULL,
    result jsonb,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    not_before timestamp with time zone,
    defer_reason text,
    attempts integer DEFAULT 0 NOT NULL
);

--========================================================================
-- alembic_version
--========================================================================
-- Alembic's own bookkeeping: which migration this database is at. Not
-- application data.

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);

--========================================================================
-- allocations
--========================================================================
-- The ledger, and the single source of truth for every category balance --
-- a category's balance is the SUM of its rows here, never a stored number.
-- APPEND-ONLY: a mistake is corrected with an opposing entry, never by
-- editing or deleting, so history always explains itself. Which pair of
-- foreign keys is set determines what the row MEANS; see the comments on
-- paycheck_id and transaction_id.
--
--   transaction_id: Set, with paycheck_id null: this row is SPENDING, a real
--                   charge filed against the envelope. Both null means a
--                   TRANSFER between envelopes -- which is movement, not
--                   spending, and analytics must not count it as such.
--
--   paycheck_id: Set, with transaction_id null: this row is FUNDING, money
--                arriving in the envelope from a paycheck.
--
--   amount_cents: CATEGORY-RELATIVE and signed: positive means this envelope
--                 GAINED money, negative means it lost it. This is the
--                 opposite convention from the review queue, which is
--                 budget-relative. Exactly one place translates between the
--                 two -- Activity.budgetSignedCents in the app. Do not add a
--                 second.
--
--   expense_type_id: Optional second axis: what the charge was, as opposed
--                    to which envelope paid. Null means not yet labelled,
--                    which is reported honestly as 'Untyped' rather than
--                    hidden.

CREATE TABLE public.allocations (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    category_id uuid NOT NULL,
    transaction_id uuid,
    paycheck_id uuid,
    amount_cents bigint NOT NULL,
    source character varying(32) NOT NULL,
    confidence numeric(3,2),
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expense_type_id uuid
);

--========================================================================
-- categories
--========================================================================
-- An envelope: a balance, not a report line. 'Groceries' holds a number of
-- dollars that goes up when funded and down when spent. Archived rather
-- than deleted, so historical allocations still name something. Every user
-- has exactly one system 'Unallocated' sink that cannot be archived.
--
--   target_cents: What this envelope is meant to hold, in cents. Used by
--                 refill_to rules and by the alert threshold, not enforced
--                 as a limit.

CREATE TABLE public.categories (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    kind character varying(32) NOT NULL,
    description text,
    balance_cents bigint NOT NULL,
    target_cents bigint,
    alert_at_cents bigint,
    sort_order integer NOT NULL,
    archived_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    is_unallocated boolean DEFAULT false NOT NULL,
    spend_criteria text
);

--========================================================================
-- category_rules
--========================================================================
-- Deterministic merchant matchers that file a charge automatically,
-- skipping the review queue. No model involved: a matcher either matches
-- the merchant string or it does not. priority breaks ties when two rules
-- both match.

CREATE TABLE public.category_rules (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    category_id uuid NOT NULL,
    matcher jsonb NOT NULL,
    priority integer NOT NULL,
    enabled boolean NOT NULL
);

--========================================================================
-- expense_types
--========================================================================
-- The second, orthogonal taxonomy: what a charge WAS, independent of which
-- envelope paid for it. Lunch from 'Personal Discretionary' and lunch from
-- 'Date Fund' are different envelopes and the same expense type, which is
-- how 'how much on fast food this month' is answerable across pots. Merged
-- rather than deleted, via merged_into_id, so old allocations keep
-- resolving.

CREATE TABLE public.expense_types (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    slug character varying(80) NOT NULL,
    description text,
    created_by character varying(16) DEFAULT 'user'::character varying NOT NULL,
    reviewed_at timestamp with time zone,
    merged_into_id uuid,
    archived_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--========================================================================
-- item_memory
--========================================================================
-- Where one line item off a receipt was filed last time, so the next
-- receipt can pre-fill it. Keyed per shop with a global fallback: cottage
-- cheese at one supermarket may belong somewhere different from cottage
-- cheese at another.

CREATE TABLE public.item_memory (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    merchant_slug character varying(120) NOT NULL,
    description_slug character varying(120) NOT NULL,
    description character varying(200) NOT NULL,
    category_id uuid NOT NULL,
    expense_type_id uuid,
    times_seen integer DEFAULT 1 NOT NULL,
    last_seen_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--========================================================================
-- paychecks
--========================================================================
-- An income event that has been divided across envelopes. Links to the
-- transaction it came from (null for a hand-entered one) and to the exact
-- split_plan VERSION that was applied, which is what lets a historical
-- paycheck still explain itself. The money it moved is in allocations, not
-- here.

CREATE TABLE public.paychecks (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    transaction_id uuid,
    gross_cents bigint NOT NULL,
    received_on date NOT NULL,
    split_plan_id uuid,
    applied_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--========================================================================
-- plaid_items
--========================================================================
-- One connection to one financial institution. THIS is the row that
-- consumes a Plaid credit, and on the Trial plan the cap is 10 for the
-- lifetime of the account -- /item/remove does NOT give a slot back. Never
-- delete a row here to 'clean up'. The access_token is encrypted at rest
-- with TOKEN_ENCRYPTION_KEY, which lives in .env and not in this database,
-- so a stolen dump yields nothing usable. Re-authentication goes through
-- Link's update mode, which reuses this row and costs nothing.
--
--   access_token_encrypted: Fernet ciphertext, never a usable token. The key
--                           is TOKEN_ENCRYPTION_KEY in .env, deliberately
--                           NOT in this database, so restoring a dump
--                           without that key gives back a budget and zero
--                           working bank connections.
--
--   sync_cursor: Opaque Plaid cursor for /transactions/sync. Clearing it
--                replays history; the sync is idempotent, so that is
--                recoverable but slow.

CREATE TABLE public.plaid_items (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    plaid_item_id character varying(128) NOT NULL,
    institution_id character varying(64),
    institution_name character varying(200),
    access_token_encrypted bytea NOT NULL,
    sync_cursor text,
    status character varying(32) NOT NULL,
    last_synced_at timestamp with time zone,
    consent_expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--========================================================================
-- receipts
--========================================================================
-- An itemisation of a single transaction: one bank charge split across
-- several envelopes. Entirely optional -- the whole charge is categorised
-- first and itemising is a later refinement. line_items holds the
-- extracted lines; they are reconciled against the bank's amount so every
-- cent on the receipt is accounted for.
--
--   line_items: Extracted lines as JSON. Reconciled against the
--               transaction's own amount, with any residue attached to the
--               dominant category and flagged, so the split is right even
--               when a line is misread.

CREATE TABLE public.receipts (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    transaction_id uuid,
    image_path text NOT NULL,
    line_items jsonb,
    status character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--========================================================================
-- review_items
--========================================================================
-- The review queue: transactions that no deterministic matcher could file,
-- waiting on a decision. reason says why it landed here and suggestions
-- holds any AI proposal, which is advisory only -- nothing is filed
-- without a matcher hit or an explicit choice. Resolved rows are kept, not
-- deleted, so a past decision can teach the matcher later.

CREATE TABLE public.review_items (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    transaction_id uuid NOT NULL,
    reason character varying(48) NOT NULL,
    suggestions jsonb,
    resolved_at timestamp with time zone,
    resolution jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--========================================================================
-- sessions
--========================================================================
-- A refresh-token session, one row per signed-in device. Only a HASH of
-- the refresh token is stored, never the token. Signing in on a second
-- phone adds a row; it does not move anything, which is why both devices
-- see the same budget.
--
--   refresh_token_hash: A hash. The refresh token itself is never stored, so
--                       this table leaking does not let anyone sign in.

CREATE TABLE public.sessions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    refresh_token_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    user_agent character varying(300),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--========================================================================
-- split_plans
--========================================================================
-- A named, VERSIONED set of funding rules for dividing a paycheck. Editing
-- a plan writes a new version rather than mutating the old one, so a
-- paycheck recorded six months ago can still explain exactly how it was
-- split. Exactly one version is active.

CREATE TABLE public.split_plans (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    version integer NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--========================================================================
-- split_rules
--========================================================================
-- One rule inside a plan: percent, fixed, refill_to, conditional or
-- remainder. order_index IS the funding order, and it matters -- a large
-- refill_to placed above rent will take the whole paycheck and starve
-- everything below it. Exactly one remainder rule, and it must be last.
-- params is rule-type specific and its required keys are enforced by the
-- engine, not by the column.
--
--   order_index: The funding order, and it is load-bearing. Rules run top to
--                bottom against a finite paycheck; whatever sits last may
--                get nothing.
--
--   params: Rule-type specific JSON. percent needs rate; fixed needs
--           amount_cents; refill_to needs target_cents; conditional needs
--           when and then. Missing keys are rejected by the split engine
--           with a named error -- a model will omit any property a schema
--           does not mark required, so this is expected input rather than an
--           impossible state.

CREATE TABLE public.split_rules (
    id uuid NOT NULL,
    plan_id uuid NOT NULL,
    category_id uuid NOT NULL,
    order_index integer NOT NULL,
    rule_type character varying(32) NOT NULL,
    params jsonb NOT NULL,
    enabled boolean NOT NULL
);

--========================================================================
-- transactions
--========================================================================
-- An immutable fact from the bank, mirrored from Plaid and never edited.
-- What it MEANS for the budget is not stored here -- that lives in
-- allocations, so a charge can be refiled without rewriting history.
-- Direction is stored as two unsigned columns, outflow_cents and
-- inflow_cents, rather than one signed amount.
--
--   outflow_cents: Money leaving, unsigned. Direction is two columns rather
--                  than one signed amount so a refund is never a negative
--                  expense by accident; exactly one of outflow_cents and
--                  inflow_cents is non-zero.
--
--   inflow_cents: Money arriving, unsigned: a deposit, a refund, a paycheck.
--
--   pending_replaced_by: A pending charge is later replaced by a settled one
--                        with a different id. This points at the replacement
--                        so the pair is not counted twice.
--
--   treatment: What this charge MEANS for the budget. 'spend' allocates
--              against a category; a credit-card payment or an internal
--              transfer must NOT, because the card purchase already drew the
--              category down and counting the payment too would double-count
--              the same money. The consequence worth stating plainly:
--              category balances are COMMITTED money, not cash in the bank.

CREATE TABLE public.transactions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    account_id uuid NOT NULL,
    plaid_transaction_id character varying(128) NOT NULL,
    outflow_cents bigint NOT NULL,
    inflow_cents bigint NOT NULL,
    iso_currency_code character varying(8) NOT NULL,
    date date NOT NULL,
    datetime timestamp with time zone,
    name text NOT NULL,
    merchant_name character varying(300),
    pending boolean NOT NULL,
    pending_replaced_by character varying(128),
    pfc_primary character varying(64),
    pfc_detailed character varying(96),
    removed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    treatment character varying(16) DEFAULT 'spend'::character varying NOT NULL
);

--========================================================================
-- user_goals
--========================================================================
-- What the user says they are trying to do with their money, in their own
-- words. Free prose, read as context by five AI tasks -- the budget
-- summary, the advice agent, and the three that design or refine
-- categories.

CREATE TABLE public.user_goals (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    body text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--========================================================================
-- users
--========================================================================
-- One person with a login. Everything else in this database hangs off a
-- user_id and cascades on delete, so removing a user removes their entire
-- budget. Also holds that user's own AI provider settings, so two users on
-- the same server need not share a model host.

CREATE TABLE public.users (
    id uuid NOT NULL,
    email character varying(320) NOT NULL,
    password_hash text NOT NULL,
    display_name character varying(120),
    timezone character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    ai_provider character varying(16),
    ai_base_url character varying(300)
);

-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);

-- Name: accounts pk_accounts; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT pk_accounts PRIMARY KEY (id);

-- Name: ai_jobs pk_ai_jobs; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.ai_jobs
    ADD CONSTRAINT pk_ai_jobs PRIMARY KEY (id);

-- Name: allocations pk_allocations; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.allocations
    ADD CONSTRAINT pk_allocations PRIMARY KEY (id);

-- Name: categories pk_categories; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT pk_categories PRIMARY KEY (id);

-- Name: category_rules pk_category_rules; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.category_rules
    ADD CONSTRAINT pk_category_rules PRIMARY KEY (id);

-- Name: expense_types pk_expense_types; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.expense_types
    ADD CONSTRAINT pk_expense_types PRIMARY KEY (id);

-- Name: item_memory pk_item_memory; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.item_memory
    ADD CONSTRAINT pk_item_memory PRIMARY KEY (id);

-- Name: paychecks pk_paychecks; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.paychecks
    ADD CONSTRAINT pk_paychecks PRIMARY KEY (id);

-- Name: plaid_items pk_plaid_items; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.plaid_items
    ADD CONSTRAINT pk_plaid_items PRIMARY KEY (id);

-- Name: receipts pk_receipts; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.receipts
    ADD CONSTRAINT pk_receipts PRIMARY KEY (id);

-- Name: review_items pk_review_items; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.review_items
    ADD CONSTRAINT pk_review_items PRIMARY KEY (id);

-- Name: sessions pk_sessions; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT pk_sessions PRIMARY KEY (id);

-- Name: split_plans pk_split_plans; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.split_plans
    ADD CONSTRAINT pk_split_plans PRIMARY KEY (id);

-- Name: split_rules pk_split_rules; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.split_rules
    ADD CONSTRAINT pk_split_rules PRIMARY KEY (id);

-- Name: transactions pk_transactions; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT pk_transactions PRIMARY KEY (id);

-- Name: user_goals pk_user_goals; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.user_goals
    ADD CONSTRAINT pk_user_goals PRIMARY KEY (id);

-- Name: users pk_users; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);

-- Name: accounts uq_accounts_plaid_account_id; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT uq_accounts_plaid_account_id UNIQUE (plaid_account_id);

-- Name: item_memory uq_item_memory_user_merchant_desc; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.item_memory
    ADD CONSTRAINT uq_item_memory_user_merchant_desc UNIQUE (user_id, merchant_slug, description_slug);

-- Name: plaid_items uq_plaid_items_plaid_item_id; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.plaid_items
    ADD CONSTRAINT uq_plaid_items_plaid_item_id UNIQUE (plaid_item_id);

-- Name: transactions uq_transactions_plaid_transaction_id; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT uq_transactions_plaid_transaction_id UNIQUE (plaid_transaction_id);

-- Name: user_goals uq_user_goals_user_id; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.user_goals
    ADD CONSTRAINT uq_user_goals_user_id UNIQUE (user_id);

-- Name: ix_accounts_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_accounts_user_id ON public.accounts USING btree (user_id);

-- Name: ix_ai_jobs_status_not_before; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_ai_jobs_status_not_before ON public.ai_jobs USING btree (status, not_before);

-- Name: ix_ai_jobs_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_ai_jobs_user_id ON public.ai_jobs USING btree (user_id);

-- Name: ix_allocations_user_cat_time; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_allocations_user_cat_time ON public.allocations USING btree (user_id, category_id, created_at);

-- Name: ix_allocations_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_allocations_user_id ON public.allocations USING btree (user_id);

-- Name: ix_allocations_user_type_time; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_allocations_user_type_time ON public.allocations USING btree (user_id, expense_type_id, created_at);

-- Name: ix_categories_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_categories_user_id ON public.categories USING btree (user_id);

-- Name: ix_category_rules_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_category_rules_user_id ON public.category_rules USING btree (user_id);

-- Name: ix_expense_types_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_expense_types_user_id ON public.expense_types USING btree (user_id);

-- Name: ix_item_memory_user_desc; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_item_memory_user_desc ON public.item_memory USING btree (user_id, description_slug);

-- Name: ix_item_memory_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_item_memory_user_id ON public.item_memory USING btree (user_id);

-- Name: ix_paychecks_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_paychecks_user_id ON public.paychecks USING btree (user_id);

-- Name: ix_plaid_items_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_plaid_items_user_id ON public.plaid_items USING btree (user_id);

-- Name: ix_receipts_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_receipts_user_id ON public.receipts USING btree (user_id);

-- Name: ix_review_items_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_review_items_user_id ON public.review_items USING btree (user_id);

-- Name: ix_sessions_refresh_token_hash; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_sessions_refresh_token_hash ON public.sessions USING btree (refresh_token_hash);

-- Name: ix_sessions_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_sessions_user_id ON public.sessions USING btree (user_id);

-- Name: ix_split_plans_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_split_plans_user_id ON public.split_plans USING btree (user_id);

-- Name: ix_transactions_user_date; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_transactions_user_date ON public.transactions USING btree (user_id, date);

-- Name: ix_transactions_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_transactions_user_id ON public.transactions USING btree (user_id);

-- Name: ix_transactions_user_pending; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_transactions_user_pending ON public.transactions USING btree (user_id, pending);

-- Name: ix_transactions_user_treatment; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_transactions_user_treatment ON public.transactions USING btree (user_id, treatment);

-- Name: ix_user_goals_user_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_user_goals_user_id ON public.user_goals USING btree (user_id);

-- Name: ix_users_email; Type: INDEX; Schema: public; Owner: -

CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);

-- Name: uq_categories_one_unallocated; Type: INDEX; Schema: public; Owner: -

CREATE UNIQUE INDEX uq_categories_one_unallocated ON public.categories USING btree (user_id) WHERE (is_unallocated AND (archived_at IS NULL));

-- Name: uq_categories_user_name_active; Type: INDEX; Schema: public; Owner: -

CREATE UNIQUE INDEX uq_categories_user_name_active ON public.categories USING btree (user_id, name) WHERE (archived_at IS NULL);

-- Name: uq_expense_types_user_slug_active; Type: INDEX; Schema: public; Owner: -

CREATE UNIQUE INDEX uq_expense_types_user_slug_active ON public.expense_types USING btree (user_id, slug) WHERE (archived_at IS NULL);

-- Name: accounts fk_accounts_item_id_plaid_items; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT fk_accounts_item_id_plaid_items FOREIGN KEY (item_id) REFERENCES public.plaid_items(id) ON DELETE CASCADE;

-- Name: accounts fk_accounts_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT fk_accounts_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: ai_jobs fk_ai_jobs_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.ai_jobs
    ADD CONSTRAINT fk_ai_jobs_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: allocations fk_allocations_category_id_categories; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.allocations
    ADD CONSTRAINT fk_allocations_category_id_categories FOREIGN KEY (category_id) REFERENCES public.categories(id) ON DELETE CASCADE;

-- Name: allocations fk_allocations_expense_type_id_expense_types; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.allocations
    ADD CONSTRAINT fk_allocations_expense_type_id_expense_types FOREIGN KEY (expense_type_id) REFERENCES public.expense_types(id) ON DELETE SET NULL;

-- Name: allocations fk_allocations_paycheck_id_paychecks; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.allocations
    ADD CONSTRAINT fk_allocations_paycheck_id_paychecks FOREIGN KEY (paycheck_id) REFERENCES public.paychecks(id) ON DELETE CASCADE;

-- Name: allocations fk_allocations_transaction_id_transactions; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.allocations
    ADD CONSTRAINT fk_allocations_transaction_id_transactions FOREIGN KEY (transaction_id) REFERENCES public.transactions(id) ON DELETE CASCADE;

-- Name: allocations fk_allocations_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.allocations
    ADD CONSTRAINT fk_allocations_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: categories fk_categories_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT fk_categories_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: category_rules fk_category_rules_category_id_categories; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.category_rules
    ADD CONSTRAINT fk_category_rules_category_id_categories FOREIGN KEY (category_id) REFERENCES public.categories(id) ON DELETE CASCADE;

-- Name: category_rules fk_category_rules_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.category_rules
    ADD CONSTRAINT fk_category_rules_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: expense_types fk_expense_types_merged_into_id_expense_types; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.expense_types
    ADD CONSTRAINT fk_expense_types_merged_into_id_expense_types FOREIGN KEY (merged_into_id) REFERENCES public.expense_types(id) ON DELETE SET NULL;

-- Name: expense_types fk_expense_types_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.expense_types
    ADD CONSTRAINT fk_expense_types_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: item_memory fk_item_memory_category_id_categories; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.item_memory
    ADD CONSTRAINT fk_item_memory_category_id_categories FOREIGN KEY (category_id) REFERENCES public.categories(id) ON DELETE CASCADE;

-- Name: item_memory fk_item_memory_expense_type_id_expense_types; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.item_memory
    ADD CONSTRAINT fk_item_memory_expense_type_id_expense_types FOREIGN KEY (expense_type_id) REFERENCES public.expense_types(id) ON DELETE SET NULL;

-- Name: item_memory fk_item_memory_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.item_memory
    ADD CONSTRAINT fk_item_memory_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: paychecks fk_paychecks_split_plan_id_split_plans; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.paychecks
    ADD CONSTRAINT fk_paychecks_split_plan_id_split_plans FOREIGN KEY (split_plan_id) REFERENCES public.split_plans(id) ON DELETE SET NULL;

-- Name: paychecks fk_paychecks_transaction_id_transactions; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.paychecks
    ADD CONSTRAINT fk_paychecks_transaction_id_transactions FOREIGN KEY (transaction_id) REFERENCES public.transactions(id) ON DELETE SET NULL;

-- Name: paychecks fk_paychecks_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.paychecks
    ADD CONSTRAINT fk_paychecks_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: plaid_items fk_plaid_items_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.plaid_items
    ADD CONSTRAINT fk_plaid_items_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: receipts fk_receipts_transaction_id_transactions; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.receipts
    ADD CONSTRAINT fk_receipts_transaction_id_transactions FOREIGN KEY (transaction_id) REFERENCES public.transactions(id) ON DELETE SET NULL;

-- Name: receipts fk_receipts_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.receipts
    ADD CONSTRAINT fk_receipts_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: review_items fk_review_items_transaction_id_transactions; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.review_items
    ADD CONSTRAINT fk_review_items_transaction_id_transactions FOREIGN KEY (transaction_id) REFERENCES public.transactions(id) ON DELETE CASCADE;

-- Name: review_items fk_review_items_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.review_items
    ADD CONSTRAINT fk_review_items_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: sessions fk_sessions_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT fk_sessions_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: split_plans fk_split_plans_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.split_plans
    ADD CONSTRAINT fk_split_plans_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: split_rules fk_split_rules_category_id_categories; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.split_rules
    ADD CONSTRAINT fk_split_rules_category_id_categories FOREIGN KEY (category_id) REFERENCES public.categories(id) ON DELETE CASCADE;

-- Name: split_rules fk_split_rules_plan_id_split_plans; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.split_rules
    ADD CONSTRAINT fk_split_rules_plan_id_split_plans FOREIGN KEY (plan_id) REFERENCES public.split_plans(id) ON DELETE CASCADE;

-- Name: transactions fk_transactions_account_id_accounts; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT fk_transactions_account_id_accounts FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;

-- Name: transactions fk_transactions_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT fk_transactions_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

-- Name: user_goals fk_user_goals_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.user_goals
    ADD CONSTRAINT fk_user_goals_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;



--========================================================================
-- The same descriptions, as statements, so restoring this file
-- puts them back in the database. Edit them in a migration, not
-- here: this file is regenerated by scripts/dump-schema.sh.
--========================================================================

COMMENT ON TABLE public.accounts IS 'One account inside a connection: a checking account, a credit card. is_tracked is the user''s choice of which accounts this budget covers -- loans, pensions and investments are usually left out, and untracked accounts contribute nothing to the allocatable total.';

COMMENT ON COLUMN public.accounts.is_tracked IS 'Whether this account counts toward the budget. Untracked accounts are still synced and visible but contribute nothing to the allocatable total.';

COMMENT ON TABLE public.ai_jobs IS 'The queue. Every AI call is a persisted job rather than a blocking request, because the model host is a gaming PC that sleeps and may be busy. A job carries its own request and result, so what was asked and what came back are both still inspectable afterwards. status ''deferred'' plus not_before is how a job waits for the GPU instead of failing.';

COMMENT ON COLUMN public.ai_jobs.not_before IS 'Set when a job is deferred because the GPU was busy or it is inside the 01:00-08:00 America/Denver quiet window. The worker that already polls for queued work picks it up again after this time.';

COMMENT ON COLUMN public.ai_jobs.attempts IS 'Counts deferrals so a job cannot wait forever. Past the deadline it runs anyway: a stuttered frame costs less than a transaction that never gets categorised.';

COMMENT ON TABLE public.alembic_version IS 'Alembic''s own bookkeeping: which migration this database is at. Not application data.';

COMMENT ON TABLE public.allocations IS 'The ledger, and the single source of truth for every category balance -- a category''s balance is the SUM of its rows here, never a stored number. APPEND-ONLY: a mistake is corrected with an opposing entry, never by editing or deleting, so history always explains itself. Which pair of foreign keys is set determines what the row MEANS; see the comments on paycheck_id and transaction_id.';

COMMENT ON COLUMN public.allocations.transaction_id IS 'Set, with paycheck_id null: this row is SPENDING, a real charge filed against the envelope. Both null means a TRANSFER between envelopes -- which is movement, not spending, and analytics must not count it as such.';

COMMENT ON COLUMN public.allocations.paycheck_id IS 'Set, with transaction_id null: this row is FUNDING, money arriving in the envelope from a paycheck.';

COMMENT ON COLUMN public.allocations.amount_cents IS 'CATEGORY-RELATIVE and signed: positive means this envelope GAINED money, negative means it lost it. This is the opposite convention from the review queue, which is budget-relative. Exactly one place translates between the two -- Activity.budgetSignedCents in the app. Do not add a second.';

COMMENT ON COLUMN public.allocations.expense_type_id IS 'Optional second axis: what the charge was, as opposed to which envelope paid. Null means not yet labelled, which is reported honestly as ''Untyped'' rather than hidden.';

COMMENT ON TABLE public.categories IS 'An envelope: a balance, not a report line. ''Groceries'' holds a number of dollars that goes up when funded and down when spent. Archived rather than deleted, so historical allocations still name something. Every user has exactly one system ''Unallocated'' sink that cannot be archived.';

COMMENT ON COLUMN public.categories.target_cents IS 'What this envelope is meant to hold, in cents. Used by refill_to rules and by the alert threshold, not enforced as a limit.';

COMMENT ON TABLE public.category_rules IS 'Deterministic merchant matchers that file a charge automatically, skipping the review queue. No model involved: a matcher either matches the merchant string or it does not. priority breaks ties when two rules both match.';

COMMENT ON TABLE public.expense_types IS 'The second, orthogonal taxonomy: what a charge WAS, independent of which envelope paid for it. Lunch from ''Personal Discretionary'' and lunch from ''Date Fund'' are different envelopes and the same expense type, which is how ''how much on fast food this month'' is answerable across pots. Merged rather than deleted, via merged_into_id, so old allocations keep resolving.';

COMMENT ON TABLE public.item_memory IS 'Where one line item off a receipt was filed last time, so the next receipt can pre-fill it. Keyed per shop with a global fallback: cottage cheese at one supermarket may belong somewhere different from cottage cheese at another.';

COMMENT ON TABLE public.paychecks IS 'An income event that has been divided across envelopes. Links to the transaction it came from (null for a hand-entered one) and to the exact split_plan VERSION that was applied, which is what lets a historical paycheck still explain itself. The money it moved is in allocations, not here.';

COMMENT ON TABLE public.plaid_items IS 'One connection to one financial institution. THIS is the row that consumes a Plaid credit, and on the Trial plan the cap is 10 for the lifetime of the account -- /item/remove does NOT give a slot back. Never delete a row here to ''clean up''. The access_token is encrypted at rest with TOKEN_ENCRYPTION_KEY, which lives in .env and not in this database, so a stolen dump yields nothing usable. Re-authentication goes through Link''s update mode, which reuses this row and costs nothing.';

COMMENT ON COLUMN public.plaid_items.access_token_encrypted IS 'Fernet ciphertext, never a usable token. The key is TOKEN_ENCRYPTION_KEY in .env, deliberately NOT in this database, so restoring a dump without that key gives back a budget and zero working bank connections.';

COMMENT ON COLUMN public.plaid_items.sync_cursor IS 'Opaque Plaid cursor for /transactions/sync. Clearing it replays history; the sync is idempotent, so that is recoverable but slow.';

COMMENT ON TABLE public.receipts IS 'An itemisation of a single transaction: one bank charge split across several envelopes. Entirely optional -- the whole charge is categorised first and itemising is a later refinement. line_items holds the extracted lines; they are reconciled against the bank''s amount so every cent on the receipt is accounted for.';

COMMENT ON COLUMN public.receipts.line_items IS 'Extracted lines as JSON. Reconciled against the transaction''s own amount, with any residue attached to the dominant category and flagged, so the split is right even when a line is misread.';

COMMENT ON TABLE public.review_items IS 'The review queue: transactions that no deterministic matcher could file, waiting on a decision. reason says why it landed here and suggestions holds any AI proposal, which is advisory only -- nothing is filed without a matcher hit or an explicit choice. Resolved rows are kept, not deleted, so a past decision can teach the matcher later.';

COMMENT ON TABLE public.sessions IS 'A refresh-token session, one row per signed-in device. Only a HASH of the refresh token is stored, never the token. Signing in on a second phone adds a row; it does not move anything, which is why both devices see the same budget.';

COMMENT ON COLUMN public.sessions.refresh_token_hash IS 'A hash. The refresh token itself is never stored, so this table leaking does not let anyone sign in.';

COMMENT ON TABLE public.split_plans IS 'A named, VERSIONED set of funding rules for dividing a paycheck. Editing a plan writes a new version rather than mutating the old one, so a paycheck recorded six months ago can still explain exactly how it was split. Exactly one version is active.';

COMMENT ON TABLE public.split_rules IS 'One rule inside a plan: percent, fixed, refill_to, conditional or remainder. order_index IS the funding order, and it matters -- a large refill_to placed above rent will take the whole paycheck and starve everything below it. Exactly one remainder rule, and it must be last. params is rule-type specific and its required keys are enforced by the engine, not by the column.';

COMMENT ON COLUMN public.split_rules.order_index IS 'The funding order, and it is load-bearing. Rules run top to bottom against a finite paycheck; whatever sits last may get nothing.';

COMMENT ON COLUMN public.split_rules.params IS 'Rule-type specific JSON. percent needs rate; fixed needs amount_cents; refill_to needs target_cents; conditional needs when and then. Missing keys are rejected by the split engine with a named error -- a model will omit any property a schema does not mark required, so this is expected input rather than an impossible state.';

COMMENT ON TABLE public.transactions IS 'An immutable fact from the bank, mirrored from Plaid and never edited. What it MEANS for the budget is not stored here -- that lives in allocations, so a charge can be refiled without rewriting history. Direction is stored as two unsigned columns, outflow_cents and inflow_cents, rather than one signed amount.';

COMMENT ON COLUMN public.transactions.outflow_cents IS 'Money leaving, unsigned. Direction is two columns rather than one signed amount so a refund is never a negative expense by accident; exactly one of outflow_cents and inflow_cents is non-zero.';

COMMENT ON COLUMN public.transactions.inflow_cents IS 'Money arriving, unsigned: a deposit, a refund, a paycheck.';

COMMENT ON COLUMN public.transactions.pending_replaced_by IS 'A pending charge is later replaced by a settled one with a different id. This points at the replacement so the pair is not counted twice.';

COMMENT ON COLUMN public.transactions.treatment IS 'What this charge MEANS for the budget. ''spend'' allocates against a category; a credit-card payment or an internal transfer must NOT, because the card purchase already drew the category down and counting the payment too would double-count the same money. The consequence worth stating plainly: category balances are COMMITTED money, not cash in the bank.';

COMMENT ON TABLE public.user_goals IS 'What the user says they are trying to do with their money, in their own words. Free prose, read as context by five AI tasks -- the budget summary, the advice agent, and the three that design or refine categories.';

COMMENT ON TABLE public.users IS 'One person with a login. Everything else in this database hangs off a user_id and cascades on delete, so removing a user removes their entire budget. Also holds that user''s own AI provider settings, so two users on the same server need not share a model host.';
  
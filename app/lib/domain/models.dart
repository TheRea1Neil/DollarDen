/// Domain models. Every monetary field is integer cents and named `*Cents`.
library;

import '../core/money.dart';

class Category {
  final String id;
  final String name;
  final String kind; // spending | savings_bucket | obligation

  /// What this category is FOR. Read by the assistant when it writes
  /// summaries; often restates the funding rule.
  final String? description;

  /// What gets CHARGED here — the only half the categoriser is shown.
  ///
  /// Not an accuracy fix: sending the funding sentence too measured
  /// identically. It is a field whose label asks for the thing that
  /// can actually decide where a charge goes.
  final String? spendCriteria;
  final int balanceCents;
  final int? targetCents;
  final int? alertAtCents;
  final int sortOrder;
  final bool archived;
  final bool belowAlert;
  final bool overspent;

  /// The system's sink for money with no purpose yet. Not something
  /// the user made, so it does not count as having built a budget.
  final bool isUnallocated;

  const Category({
    required this.id,
    required this.name,
    required this.kind,
    required this.balanceCents,
    required this.sortOrder,
    required this.archived,
    required this.belowAlert,
    required this.overspent,
    this.isUnallocated = false,
    this.description,
    this.spendCriteria,
    this.targetCents,
    this.alertAtCents,
  });

  factory Category.fromJson(Map<String, dynamic> j) => Category(
        id: j['id'] as String,
        name: j['name'] as String,
        kind: j['kind'] as String,
        description: j['description'] as String?,
        spendCriteria: j['spend_criteria'] as String?,
        balanceCents: j['balance_cents'] as int,
        targetCents: j['target_cents'] as int?,
        alertAtCents: j['alert_at_cents'] as int?,
        sortOrder: j['sort_order'] as int? ?? 0,
        archived: j['archived'] as bool? ?? false,
        belowAlert: j['below_alert'] as bool? ?? false,
        overspent: j['overspent'] as bool? ?? false,
        isUnallocated: j['is_unallocated'] as bool? ?? false,
      );

  /// How full this category is against its target, clamped to 0..1.
  /// Null when there is no target to measure against.
  double? get fillRatio {
    final t = targetCents;
    if (t == null || t <= 0) return null;
    return (balanceCents / t).clamp(0.0, 1.0);
  }
}

class ReviewItem {
  final String id;
  final String transactionId;
  final String reason;
  final String date;
  final String name;
  final String? merchantName;
  final int amountCents; // signed: positive == money out

  const ReviewItem({
    required this.id,
    required this.transactionId,
    required this.reason,
    required this.date,
    required this.name,
    required this.amountCents,
    this.merchantName,
  });

  factory ReviewItem.fromJson(Map<String, dynamic> j) => ReviewItem(
        id: j['id'] as String,
        transactionId: j['transaction_id'] as String,
        reason: j['reason'] as String,
        date: j['date'] as String,
        name: j['name'] as String,
        merchantName: j['merchant_name'] as String?,
        amountCents: j['amount_cents'] as int,
      );

  String get title => (merchantName?.trim().isNotEmpty ?? false) ? merchantName! : name;
}

class NextPayday {
  final bool known;
  final String? reason;
  final int? cadenceDays;
  final String? earliest;
  final String? expected;
  final String? latest;

  const NextPayday({
    required this.known,
    this.reason,
    this.cadenceDays,
    this.earliest,
    this.expected,
    this.latest,
  });

  factory NextPayday.fromJson(Map<String, dynamic> j) => NextPayday(
        known: j['known'] as bool? ?? false,
        reason: j['reason'] as String?,
        cadenceDays: j['cadence_days'] as int?,
        earliest: j['earliest'] as String?,
        expected: j['expected'] as String?,
        latest: j['latest'] as String?,
      );
}

class BudgetSummary {
  final List<Category> categories;
  final int totalAllocatedCents;
  final List<Category> alerts;
  final List<Category> overspent;
  final int pendingReview;
  final NextPayday nextPayday;
  final List<dynamic> ledgerDrift;
  final List<CreditCard> cards;
  final int cardDebtCents;
  final List<CashAccount> cashAccounts;
  final int cashCents;
  final int excludedCount;

  const BudgetSummary({
    required this.categories,
    required this.totalAllocatedCents,
    required this.alerts,
    required this.overspent,
    required this.pendingReview,
    required this.nextPayday,
    required this.ledgerDrift,
    required this.cards,
    required this.cardDebtCents,
    required this.cashAccounts,
    required this.cashCents,
    required this.excludedCount,
  });

  factory BudgetSummary.fromJson(Map<String, dynamic> j) => BudgetSummary(
        categories: (j['categories'] as List)
            .map((e) => Category.fromJson(e as Map<String, dynamic>))
            .toList(),
        totalAllocatedCents: j['total_allocated_cents'] as int? ?? 0,
        alerts: (j['alerts'] as List? ?? [])
            .map((e) => Category.fromJson(e as Map<String, dynamic>))
            .toList(),
        overspent: (j['overspent'] as List? ?? [])
            .map((e) => Category.fromJson(e as Map<String, dynamic>))
            .toList(),
        pendingReview: j['pending_review'] as int? ?? 0,
        nextPayday: NextPayday.fromJson(
            (j['next_payday'] as Map?)?.cast<String, dynamic>() ?? const {}),
        ledgerDrift: j['ledger_drift'] as List? ?? const [],
        cards: (j['cards'] as List? ?? [])
            .map((e) => CreditCard.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        cardDebtCents: j['card_debt_cents'] as int? ?? 0,
        cashAccounts: (j['cash_accounts'] as List? ?? [])
            .map((e) => CashAccount.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        cashCents: j['cash_cents'] as int? ?? 0,
        excludedCount: j['excluded_count'] as int? ?? 0,
      );
}


/// One connection to one institution. This is the unit that consumes a
/// Plaid connection credit, so the UI never offers a "disconnect" action --
/// removing an Item means paying to re-link it (docs/04).
class PlaidItem {
  final String id;
  final String? institutionName;
  final String status; // healthy | login_required | pending_expiration | error
  final int accounts;

  const PlaidItem({
    required this.id,
    required this.status,
    required this.accounts,
    this.institutionName,
  });

  factory PlaidItem.fromJson(Map<String, dynamic> j) => PlaidItem(
        id: j['id'] as String,
        institutionName: j['institution_name'] as String?,
        status: j['status'] as String? ?? 'healthy',
        accounts: j['accounts'] as int? ?? 0,
      );

  String get displayName => institutionName ?? 'Bank connection';

  /// Needs re-authentication. The fix is update mode, which repairs the
  /// existing Item in place and does NOT consume a credit.
  bool get needsReauth => status == 'login_required' || status == 'pending_expiration';
  bool get isHealthy => status == 'healthy';
}

class SyncResult {
  final int added;
  final int modified;
  final int removed;
  final Map<String, dynamic> categorised;

  const SyncResult({
    required this.added,
    required this.modified,
    required this.removed,
    required this.categorised,
  });

  factory SyncResult.fromJson(Map<String, dynamic> j) => SyncResult(
        added: j['added'] as int? ?? 0,
        modified: j['modified'] as int? ?? 0,
        removed: j['removed'] as int? ?? 0,
        categorised: (j['categorised'] as Map?)?.cast<String, dynamic>() ?? const {},
      );

  int get autoCategorised => (categorised['rule'] as int? ?? 0) +
      (categorised['prior_decision'] as int? ?? 0);
  int get needsReview => categorised['review'] as int? ?? 0;
}


/// An outstanding credit card balance.
///
/// Not part of the budget: card spending already drew down categories at
/// purchase time. This answers a different question -- how much needs
/// paying -- and updates itself once the payment posts.
class CreditCard {
  final String accountId;
  final String name;
  final String? mask;
  final int balanceCents;

  const CreditCard({
    required this.accountId,
    required this.name,
    required this.balanceCents,
    this.mask,
  });

  factory CreditCard.fromJson(Map<String, dynamic> j) => CreditCard(
        accountId: j['account_id'] as String,
        name: j['name'] as String? ?? 'Credit card',
        mask: j['mask'] as String?,
        balanceCents: j['balance_cents'] as int? ?? 0,
      );

  String get label => mask == null ? name : '$name ••$mask';
  bool get owes => balanceCents > 0;
}

/// A cash account — checking, savings. Real money in the bank.
///
/// Shown apart from category balances because the two answer different
/// questions and will not match: card purchases draw categories down before
/// the cash actually leaves.
class CashAccount {
  final String id;
  final String name;
  final String? mask;
  final String? subtype;
  final int balanceCents;
  final int? availableCents;

  const CashAccount({
    required this.id,
    required this.name,
    required this.balanceCents,
    this.mask,
    this.subtype,
    this.availableCents,
  });

  factory CashAccount.fromJson(Map<String, dynamic> j) => CashAccount(
        id: j['id'] as String,
        name: j['name'] as String? ?? 'Account',
        mask: j['mask'] as String?,
        subtype: j['subtype'] as String?,
        balanceCents: j['balance_cents'] as int? ?? 0,
        availableCents: j['available_cents'] as int?,
      );

  String get label => mask == null ? name : '$name ••$mask';
}


/// A queued AI job. Inference runs on a host that sleeps, so every call is
/// a job with a persisted result rather than a blocking request.
class AiJob {
  final String id;
  final String kind;
  final String status; // queued | waking | running | deferred | done | failed

  /// Why this job is waiting, in words meant for the user — the GPU is
  /// busy, or it is the middle of the night. Null unless deferred.
  final String? deferReason;

  /// When it would next be attempted on its own. The user does not have
  /// to wait for it: see [ApiClient.retryJob].
  final String? retryAt;
  final int attempts;
  final Map<String, dynamic>? result;
  final String? error;

  const AiJob({
    required this.id,
    required this.kind,
    required this.status,
    this.result,
    this.error,
    this.deferReason,
    this.retryAt,
    this.attempts = 0,
  });

  factory AiJob.fromJson(Map<String, dynamic> j) => AiJob(
        id: j['id'] as String,
        kind: j['kind'] as String,
        status: j['status'] as String,
        result: (j['result'] as Map?)?.cast<String, dynamic>(),
        error: j['error'] as String?,
        deferReason: j['defer_reason'] as String?,
        retryAt: j['retry_at'] as String?,
        attempts: j['attempts'] as int? ?? 0,
      );

  /// Deferred is NOT finished, but polling must stop: it could be an
  /// hour away. The caller surfaces the reason and offers to override.
  bool get finished => status == 'done' || status == 'failed';
  bool get ok => status == 'done';
  bool get waitingForGpu => status == 'deferred';
  bool get settled => finished || waitingForGpu;

  /// What to tell the user while they wait. "Waking" is worth saying out
  /// loud: a ten second pause looks like a hang otherwise.
  String get progressLabel => switch (status) {
        'queued' => 'Queued…',
        'waking' => 'Waking the AI server…',
        'running' => 'Thinking…',
        'failed' => 'Failed',
        _ => 'Done',
      };
}

/// A proposed category from the assistant. Editable before it is applied.
class ProposedCategory {
  String name;
  String kind;
  String description;
  int? targetCents;
  final bool alreadyExists;

  ProposedCategory({
    required this.name,
    required this.kind,
    required this.description,
    required this.alreadyExists,
    this.targetCents,
  });

  factory ProposedCategory.fromJson(Map<String, dynamic> j) => ProposedCategory(
        name: j['name'] as String,
        kind: j['kind'] as String? ?? 'spending',
        description: j['description'] as String? ?? '',
        targetCents: j['target_cents'] as int?,
        alreadyExists: j['already_exists'] as bool? ?? false,
      );

  Map<String, dynamic> toJson() => {
        'name': name,
        'kind': kind,
        if (description.isNotEmpty) 'description': description,
        if (targetCents != null) 'target_cents': targetCents,
      };
}

/// A proposed split rule, with the model's own one-line reasoning.
class ProposedRule {
  String categoryName;
  int orderIndex;
  String ruleType;
  Map<String, dynamic> params;
  final String why;

  ProposedRule({
    required this.categoryName,
    required this.orderIndex,
    required this.ruleType,
    required this.params,
    required this.why,
  });

  factory ProposedRule.fromJson(Map<String, dynamic> j) => ProposedRule(
        categoryName: j['category_name'] as String,
        orderIndex: j['order_index'] as int? ?? 0,
        ruleType: j['rule_type'] as String,
        params: (j['params'] as Map?)?.cast<String, dynamic>() ?? {},
        why: j['why'] as String? ?? '',
      );

  Map<String, dynamic> toJson() => {
        'category_name': categoryName,
        'order_index': orderIndex,
        'rule_type': ruleType,
        'params': params,
      };

  /// Plain-English rendering, so the plan is readable without knowing the
  /// rule vocabulary.
  String describe() {
    switch (ruleType) {
      case 'percent':
        final rate = double.tryParse('${params['rate'] ?? 0}') ?? 0;
        final of = params['of'] == 'remaining' ? "what's left" : 'gross pay';
        return '${(rate * 100).toStringAsFixed(0)}% of $of';
      case 'fixed':
        return _money(params['amount_cents']);
      case 'refill_to':
        return 'top up to ${_money(params['target_cents'])}';
      case 'conditional':
        final when = (params['when'] as Map?)?.cast<String, dynamic>() ?? {};
        final then = (params['then'] as Map?)?.cast<String, dynamic>() ?? {};
        final amount = (then['params'] as Map?)?['amount_cents'];
        return 'if below ${_money(when['value_cents'])}, '
            'add ${_money(amount)}';
      case 'remainder':
        return 'everything left over';
      default:
        return ruleType;
    }
  }

  static String _money(dynamic cents) {
    if (cents is! int) return '—';
    return '\$${(cents / 100).toStringAsFixed(2)}';
  }
}

class Proposal {
  final List<ProposedCategory> categories;
  final List<ProposedRule> rules;
  final String notes;
  final List<String> problems;
  final List<String> notices;
  /// Legal but probably unintended — e.g. a savings goal ordered above
  /// rent, which starves it. Worth showing prominently; not a blocker.
  final List<String> warnings;
  final bool applicable;

  const Proposal({
    required this.categories,
    required this.rules,
    required this.notes,
    required this.problems,
    required this.notices,
    required this.warnings,
    required this.applicable,
  });

  factory Proposal.fromJson(Map<String, dynamic> j) => Proposal(
        categories: (j['categories'] as List? ?? [])
            .map((e) => ProposedCategory.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        rules: (j['split_rules'] as List? ?? [])
            .map((e) => ProposedRule.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        notes: j['notes'] as String? ?? '',
        problems: (j['problems'] as List? ?? []).map((e) => '$e').toList(),
        notices: (j['notices'] as List? ?? []).map((e) => '$e').toList(),
        warnings: (j['warnings'] as List? ?? []).map((e) => '$e').toList(),
        applicable: j['applicable'] as bool? ?? false,
      );
}

/// One AI-designed category, before the user has accepted it.
///
/// Mutable because the whole point is that the user corrects it on screen:
/// the model's output is a draft, not a decision.
class SuggestedCategory {
  String name;
  String kind;
  String description;
  int? targetCents;
  int? alertAtCents;

  /// How a paycheck should fund it. Null means "create it unfunded" --
  /// a perfectly reasonable outcome the user can also choose directly.
  String? ruleType;
  Map<String, dynamic> params;
  final String why;

  final List<String> problems;
  final List<String> notices;
  final bool applicable;

  /// A word the model expects on matching transactions, e.g. "vet".
  ///
  /// Only becomes a real matching rule if the user says yes: a bad guess
  /// here silently swallows unrelated spending into the wrong envelope,
  /// which is worse than reviewing a few transactions by hand.
  String? merchantHint;
  bool autoMatch = false;

  SuggestedCategory({
    required this.name,
    required this.kind,
    required this.description,
    required this.applicable,
    this.targetCents,
    this.alertAtCents,
    this.ruleType,
    this.params = const {},
    this.why = '',
    this.problems = const [],
    this.notices = const [],
    this.merchantHint,
  });

  factory SuggestedCategory.fromJson(Map<String, dynamic> j) {
    final rule = (j['suggested_rule'] as Map?)?.cast<String, dynamic>();
    final hint = (j['matcher_hint'] as Map?)?.cast<String, dynamic>();
    return SuggestedCategory(
      name: j['name'] as String? ?? '',
      kind: j['kind'] as String? ?? 'spending',
      description: j['description'] as String? ?? '',
      targetCents: j['target_cents'] as int?,
      alertAtCents: j['alert_at_cents'] as int?,
      ruleType: rule?['rule_type'] as String?,
      params: (rule?['params'] as Map?)?.cast<String, dynamic>() ?? {},
      why: rule?['why'] as String? ?? '',
      problems: ((j['problems'] as List?) ?? const []).cast<String>(),
      notices: ((j['notices'] as List?) ?? const []).cast<String>(),
      applicable: j['applicable'] as bool? ?? false,
      merchantHint: hint?['merchant_contains'] as String?,
    );
  }

  ProposedCategory toProposed() => ProposedCategory(
        name: name,
        kind: kind,
        description: description,
        targetCents: targetCents,
        alreadyExists: false,
      );

  /// A plain-English reading of the funding rule, so the user can tell
  /// whether it is right without knowing what `refill_to` means.
  String get fundingSummary {
    switch (ruleType) {
      case null:
        return 'Not funded automatically — you allocate to it by hand.';
      case 'percent':
        final rate = params['rate']?.toString() ?? '?';
        final pct = (double.tryParse(rate) ?? 0) * 100;
        final of = params['of'] == 'remaining' ? "what's left" : 'gross pay';
        return '${pct.toStringAsFixed(pct % 1 == 0 ? 0 : 1)}% of $of each paycheck';
      case 'fixed':
        final c = params['amount_cents'];
        return c is int ? '${Money.format(c)} every paycheck' : 'A fixed amount';
      case 'refill_to':
        final c = params['target_cents'];
        return c is int
            ? 'Topped back up to ${Money.format(c)} each paycheck'
            : 'Refilled to a target';
      case 'conditional':
        return 'Funded only when a condition is met';
      default:
        return ruleType!;
    }
  }
}

/// How a category is funded from a paycheck, as the API describes it.
class Funding {
  final String planId;
  final int planVersion;
  final String ruleType;
  final Map<String, dynamic> params;
  final bool enabled;

  /// Where it sits in the funding order, and out of how many. Position is
  /// what decides who loses when a paycheck is short, so it is shown.
  final int position;
  final int ofTotal;

  /// The rule in plain English, rendered server-side so the app and the
  /// API cannot drift into describing the same rule differently.
  final String summary;

  const Funding({
    required this.planId,
    required this.planVersion,
    required this.ruleType,
    required this.params,
    required this.enabled,
    required this.position,
    required this.ofTotal,
    required this.summary,
  });

  factory Funding.fromJson(Map<String, dynamic> j) => Funding(
        planId: j['plan_id'] as String,
        planVersion: j['plan_version'] as int,
        ruleType: j['rule_type'] as String,
        params: (j['params'] as Map?)?.cast<String, dynamic>() ?? {},
        enabled: j['enabled'] as bool? ?? true,
        position: j['position'] as int,
        ofTotal: j['of_total'] as int,
        summary: j['summary'] as String? ?? '',
      );

  int? get amountCents => params['amount_cents'] as int?;
  int? get targetCents => params['target_cents'] as int?;
  int? get maxContributionCents => params['max_contribution_cents'] as int?;
  String? get rate => params['rate']?.toString();
  String? get of => params['of'] as String?;

  Map<String, dynamic> get _when =>
      (params['when'] as Map?)?.cast<String, dynamic>() ?? {};
  String? get whenOp => _when['op'] as String?;
  int? get whenValueCents => _when['value_cents'] as int?;
  int? get thenAmountCents {
    final then = (params['then'] as Map?)?.cast<String, dynamic>() ?? {};
    final p = (then['params'] as Map?)?.cast<String, dynamic>() ?? {};
    return p['amount_cents'] as int?;
  }
}

/// A rule deciding which transactions come out of a category.
class Matcher {
  final String id;
  final Map<String, dynamic> matcher;
  final int priority;
  final bool enabled;
  final String summary;

  const Matcher({
    required this.id,
    required this.matcher,
    required this.priority,
    required this.enabled,
    required this.summary,
  });

  factory Matcher.fromJson(Map<String, dynamic> j) => Matcher(
        id: j['id'] as String,
        matcher: (j['matcher'] as Map?)?.cast<String, dynamic>() ?? {},
        priority: j['priority'] as int? ?? 100,
        enabled: j['enabled'] as bool? ?? true,
        summary: j['summary'] as String? ?? '',
      );
}

/// One line of a category's ledger: what moved, when, and which way.
class Activity {
  final int amountCents;   // +into the category, -out of it
  final String source;
  final String? note;
  final String? merchant;
  final String? at;
  final String? transactionId;

  const Activity({
    required this.amountCents,
    required this.source,
    this.note,
    this.merchant,
    this.at,
    this.transactionId,
  });

  factory Activity.fromJson(Map<String, dynamic> j) => Activity(
        amountCents: j['amount_cents'] as int? ?? 0,
        source: j['source'] as String? ?? '',
        note: j['note'] as String?,
        merchant: j['merchant'] as String?,
        at: j['at'] as String?,
        transactionId: j['transaction_id'] as String?,
      );

  /// NOTE the inversion. A ledger entry is stored from the CATEGORY's
  /// point of view: positive means the category gained. A review item is
  /// stored from the BUDGET's point of view: positive means money left.
  /// Two opposite conventions for the same idea, so this is the one place
  /// the ledger sign is translated for display.
  int get budgetSignedCents => -amountCents;

  String get title => merchant ?? _sourceLabel;

  String get _sourceLabel => switch (source) {
        'paycheck_split' => 'From a paycheck',
        'auto_categorise' => 'Filed automatically',
        'manual' => 'Filed by hand',
        'opening_balance' => 'Opening balance',
        'reconcile' => 'Reconciliation',
        'unallocated_sweep' => 'Swept to unallocated',
        _ => source.isEmpty ? '—' : source.replaceAll('_', ' '),
      };

  /// What to show under the title: the note if there is one, else how it
  /// got here.
  String get subtitle =>
      (note != null && note!.isNotEmpty) ? note! : _sourceLabel;

  String get shortDate {
    final v = at;
    if (v == null || v.length < 10) return '';
    return v.substring(0, 10);
  }
}

/// Everything about one category, for the screen that edits it.
class CategoryDetail {
  final Category category;
  final Funding? funding;
  final List<Matcher> matchers;
  final List<Activity> recent;

  const CategoryDetail({
    required this.category,
    required this.matchers,
    required this.recent,
    this.funding,
  });

  factory CategoryDetail.fromJson(Map<String, dynamic> j) => CategoryDetail(
        category: Category.fromJson((j['category'] as Map).cast<String, dynamic>()),
        funding: j['funding'] == null
            ? null
            : Funding.fromJson((j['funding'] as Map).cast<String, dynamic>()),
        matchers: ((j['matchers'] as List?) ?? const [])
            .map((e) => Matcher.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        recent: ((j['recent'] as List?) ?? const [])
            .map((e) => Activity.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
      );
}

/// An AI-proposed change to one existing category, before it is accepted.
class RefinedCategory {
  String name;
  String kind;
  String description;
  int? targetCents;
  int? alertAtCents;
  Map<String, dynamic> funding;
  final List<Map<String, dynamic>> addMatchers;
  final String whatChanged;
  final List<String> problems;
  final List<String> notices;
  final bool applicable;

  RefinedCategory({
    required this.name,
    required this.kind,
    required this.description,
    required this.funding,
    required this.applicable,
    this.targetCents,
    this.alertAtCents,
    this.addMatchers = const [],
    this.whatChanged = '',
    this.problems = const [],
    this.notices = const [],
  });

  factory RefinedCategory.fromJson(Map<String, dynamic> j) => RefinedCategory(
        name: j['name'] as String? ?? '',
        kind: j['kind'] as String? ?? 'spending',
        description: j['description'] as String? ?? '',
        targetCents: j['target_cents'] as int?,
        alertAtCents: j['alert_at_cents'] as int?,
        funding: (j['funding'] as Map?)?.cast<String, dynamic>() ??
            {'rule_type': 'none'},
        addMatchers: ((j['add_matchers'] as List?) ?? const [])
            .map((e) => (e as Map).cast<String, dynamic>())
            .toList(),
        whatChanged: j['what_changed'] as String? ?? '',
        problems: ((j['problems'] as List?) ?? const []).cast<String>(),
        notices: ((j['notices'] as List?) ?? const []).cast<String>(),
        applicable: j['applicable'] as bool? ?? false,
      );

  /// Translate the AI's flat funding shape into what the API expects.
  ///
  /// The model answers with `when_op`/`when_value_cents` because a flat
  /// schema is far more reliable for it than a nested one; the engine
  /// wants `when: {op, value_cents}`. One of them has to bend, and it
  /// should not be the engine.
  Map<String, dynamic>? get apiParams {
    switch (funding['rule_type']) {
      case 'percent':
        return {'rate': funding['rate']?.toString() ?? '0',
                'of': funding['of'] ?? 'gross'};
      case 'fixed':
        return {'amount_cents': funding['amount_cents']};
      case 'refill_to':
        return {
          'target_cents': funding['target_cents'],
          if (funding['max_contribution_cents'] != null)
            'max_contribution_cents': funding['max_contribution_cents'],
        };
      case 'conditional':
        return {
          'when': {
            'field': 'balance',
            'op': funding['when_op'],
            'value_cents': funding['when_value_cents'],
          },
          'then': {
            'rule_type': 'fixed',
            'params': {'amount_cents': funding['then_amount_cents']},
          },
        };
      default:
        return null;
    }
  }

  String? get apiRuleType {
    final t = funding['rule_type'];
    return (t == null || t == 'none') ? null : t as String;
  }
}

/// One line of a paycheck preview: what a category holds, gets, and ends at.
class PreviewLine {
  final String categoryId;
  final String name;
  final int orderIndex;
  final String ruleType;
  final String ruleSummary;
  final Map<String, dynamic> params;
  final int balanceCents;
  final int allocatedCents;
  /// Set aside by hand this paycheck, before the rules ran.
  final int manualCents;
  final int afterCents;
  final int shortCents;

  const PreviewLine({
    required this.categoryId,
    required this.name,
    required this.orderIndex,
    required this.ruleType,
    required this.ruleSummary,
    required this.params,
    required this.balanceCents,
    required this.allocatedCents,
    required this.afterCents,
    required this.shortCents,
    this.manualCents = 0,
  });

  factory PreviewLine.fromJson(Map<String, dynamic> j) => PreviewLine(
        categoryId: j['category_id'] as String,
        name: j['name'] as String,
        orderIndex: j['order_index'] as int? ?? 0,
        ruleType: j['rule_type'] as String? ?? '',
        ruleSummary: j['rule_summary'] as String? ?? '',
        params: (j['params'] as Map?)?.cast<String, dynamic>() ?? {},
        balanceCents: j['balance_cents'] as int? ?? 0,
        allocatedCents: j['allocated_cents'] as int? ?? 0,
        manualCents: j['manual_cents'] as int? ?? 0,
        afterCents: j['after_cents'] as int? ?? 0,
        shortCents: j['short_cents'] as int? ?? 0,
      );

  bool get gotNothing => allocatedCents == 0 && manualCents == 0;

  /// A row that exists only because money was set aside by hand; it has
  /// no rule in the plan.
  bool get isManualOnly => ruleType == 'manual';

  /// The shape the preview endpoint wants back when proposing a change.
  Map<String, dynamic> toRuleJson(int orderIndex) => {
        'category_id': categoryId,
        'order_index': orderIndex,
        'rule_type': ruleType,
        'params': params,
      };

  PreviewLine copyWithRule(String newType, Map<String, dynamic> newParams) =>
      PreviewLine(
        categoryId: categoryId,
        name: name,
        orderIndex: orderIndex,
        ruleType: newType,
        ruleSummary: ruleSummary,
        params: newParams,
        balanceCents: balanceCents,
        allocatedCents: allocatedCents,
        manualCents: manualCents,
        afterCents: afterCents,
        shortCents: shortCents,
      );
}

/// What a paycheck would do, before it does it.
class PaycheckPreview {
  final int grossCents;
  final String planId;
  final int planVersion;
  final List<PreviewLine> lines;
  final int totalAllocatedCents;
  final int manualTotalCents;
  /// What the rules actually divided: gross minus the set-asides.
  final int rulesGrossCents;
  final int leftoverCents;
  final List<String> starved;
  final List<String> warnings;

  /// False when these numbers come from an unsaved what-if. The screen
  /// must not let that read as the real plan.
  final bool isSavedPlan;

  const PaycheckPreview({
    required this.grossCents,
    required this.planId,
    required this.planVersion,
    required this.lines,
    required this.totalAllocatedCents,
    required this.leftoverCents,
    this.manualTotalCents = 0,
    this.rulesGrossCents = 0,
    required this.starved,
    required this.warnings,
    this.isSavedPlan = true,
  });

  factory PaycheckPreview.fromJson(Map<String, dynamic> j) => PaycheckPreview(
        grossCents: j['gross_cents'] as int,
        planId: j['plan_id'] as String,
        planVersion: j['plan_version'] as int,
        lines: ((j['lines'] as List?) ?? const [])
            .map((e) => PreviewLine.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        totalAllocatedCents: j['total_allocated_cents'] as int? ?? 0,
        manualTotalCents: j['manual_total_cents'] as int? ?? 0,
        rulesGrossCents: j['rules_gross_cents'] as int? ?? 0,
        leftoverCents: j['leftover_cents'] as int? ?? 0,
        starved: ((j['starved'] as List?) ?? const []).cast<String>(),
        warnings: ((j['warnings'] as List?) ?? const []).cast<String>(),
        isSavedPlan: j['is_saved_plan'] as bool? ?? true,
      );
}

/// Where one transaction's money went.
class HistorySplit {
  final String categoryId;
  final String categoryName;

  /// Budget-signed, same as everywhere else: positive means the category
  /// was reduced.
  final int amountCents;

  const HistorySplit({
    required this.categoryId,
    required this.categoryName,
    required this.amountCents,
  });

  factory HistorySplit.fromJson(Map<String, dynamic> j) => HistorySplit(
        categoryId: j['category_id'] as String,
        categoryName: j['category_name'] as String,
        amountCents: j['amount_cents'] as int? ?? 0,
      );
}

/// A transaction with where it landed.
class HistoryItem {
  final String id;
  final String date;
  final String name;
  final String? merchantName;
  final String? accountName;
  final int amountCents;   // budget-signed: positive == money out
  final bool pending;
  final String treatment;
  final String treatmentLabel;
  final List<HistorySplit> splits;

  /// Deliberately left out of the budget (a card payment or transfer) --
  /// different from simply not having been reviewed yet, and the two
  /// must not look alike.
  final bool excluded;
  final bool awaitingReview;

  /// Set when this deposit was recorded as a paycheck.
  final String? paycheckId;
  final bool paycheckApplied;

  const HistoryItem({
    required this.id,
    required this.date,
    required this.name,
    required this.amountCents,
    required this.pending,
    required this.treatment,
    required this.treatmentLabel,
    required this.splits,
    required this.excluded,
    required this.awaitingReview,
    this.merchantName,
    this.accountName,
    this.paycheckId,
    this.paycheckApplied = false,
  });

  factory HistoryItem.fromJson(Map<String, dynamic> j) => HistoryItem(
        id: j['id'] as String,
        date: j['date'] as String,
        name: j['name'] as String,
        merchantName: j['merchant_name'] as String?,
        accountName: j['account_name'] as String?,
        amountCents: j['amount_cents'] as int? ?? 0,
        pending: j['pending'] as bool? ?? false,
        treatment: j['treatment'] as String? ?? 'spend',
        treatmentLabel: j['treatment_label'] as String? ?? '',
        splits: ((j['splits'] as List?) ?? const [])
            .map((e) => HistorySplit.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        excluded: j['excluded'] as bool? ?? false,
        awaitingReview: j['awaiting_review'] as bool? ?? false,
        paycheckId: j['paycheck_id'] as String?,
        paycheckApplied: j['paycheck_applied'] as bool? ?? false,
      );

  String get title => merchantName ?? name;

  /// One line saying where it went, without needing the splits expanded.
  String get destination {
    if (paycheckId != null) {
      return paycheckApplied
          ? 'Split across ${splits.length} categories'
          : 'Recorded, not split';
    }
    if (excluded) return treatmentLabel;
    if (awaitingReview) return 'Not filed yet';
    if (splits.length == 1) return splits.first.categoryName;
    return '${splits.length} categories';
  }
}

class HistoryPage {
  final List<HistoryItem> items;
  final int total;
  final bool hasMore;

  const HistoryPage({
    required this.items,
    required this.total,
    required this.hasMore,
  });

  factory HistoryPage.fromJson(Map<String, dynamic> j) => HistoryPage(
        items: ((j['items'] as List?) ?? const [])
            .map((e) => HistoryItem.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        total: j['total'] as int? ?? 0,
        hasMore: j['has_more'] as bool? ?? false,
      );
}


/// What undoing a paycheck would do to one category.
class UndoImpact {
  final String categoryId;
  final String name;
  final int balanceCents;
  final int reversesCents;
  final int afterCents;
  final bool goesNegative;

  const UndoImpact({
    required this.categoryId,
    required this.name,
    required this.balanceCents,
    required this.reversesCents,
    required this.afterCents,
    required this.goesNegative,
  });

  factory UndoImpact.fromJson(Map<String, dynamic> j) => UndoImpact(
        categoryId: j['category_id'] as String,
        name: j['name'] as String,
        balanceCents: j['balance_cents'] as int? ?? 0,
        reversesCents: j['reverses_cents'] as int? ?? 0,
        afterCents: j['after_cents'] as int? ?? 0,
        goesNegative: j['goes_negative'] as bool? ?? false,
      );
}

/// Undoing a paycheck, before it is undone.
class UndoPreview {
  final String paycheckId;
  final int grossCents;
  final String receivedOn;
  final bool applied;
  final List<UndoImpact> impacts;
  final int totalReversedCents;

  /// Categories whose money has since been spent. Reversing does not
  /// un-spend it, so they would simply go negative.
  final List<String> wouldGoNegative;
  final bool safe;

  const UndoPreview({
    required this.paycheckId,
    required this.grossCents,
    required this.receivedOn,
    required this.applied,
    required this.impacts,
    required this.totalReversedCents,
    required this.wouldGoNegative,
    required this.safe,
  });

  factory UndoPreview.fromJson(Map<String, dynamic> j) => UndoPreview(
        paycheckId: j['paycheck_id'] as String,
        grossCents: j['gross_cents'] as int? ?? 0,
        receivedOn: j['received_on'] as String? ?? '',
        applied: j['applied'] as bool? ?? false,
        impacts: ((j['impacts'] as List?) ?? const [])
            .map((e) => UndoImpact.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        totalReversedCents: j['total_reversed_cents'] as int? ?? 0,
        wouldGoNegative:
            ((j['would_go_negative'] as List?) ?? const []).cast<String>(),
        safe: j['safe'] as bool? ?? true,
      );
}

/// A description that names something in the charge being filed.
///
/// Computed server-side by string match, not reported by the model —
/// asked what decided its answer, the model quotes only sometimes and
/// is silent exactly when a description quietly shifted its pick. This
/// is checkable: the word is in the charge and in the description.
class DescriptionMention {
  final String categoryName;
  final String word;
  final String description;

  const DescriptionMention({
    required this.categoryName,
    required this.word,
    required this.description,
  });

  factory DescriptionMention.fromJson(Map<String, dynamic> j) =>
      DescriptionMention(
        categoryName: j['category_name'] as String? ?? '',
        word: j['word'] as String? ?? '',
        description: j['description'] as String? ?? '',
      );
}

/// A matcher rule that already claims a charge.
///
/// Surfaced with the suggestion because a matcher silently outranks the
/// assistant — the charge would never reach it again. The moment one
/// misfires is the only moment the user will think to remove it.
class ExistingMatcher {
  final String ruleId;
  final String categoryId;
  final String categoryName;
  final String matchesOn;

  const ExistingMatcher({
    required this.ruleId,
    required this.categoryId,
    required this.categoryName,
    required this.matchesOn,
  });

  factory ExistingMatcher.fromJson(Map<String, dynamic> j) => ExistingMatcher(
        ruleId: j['rule_id'] as String,
        categoryId: j['category_id'] as String,
        categoryName: j['category_name'] as String? ?? '',
        matchesOn: j['matches_on'] as String? ?? '',
      );
}

/// Where the assistant thinks a charge belongs, and why.
class CategorySuggestion {
  final String? categoryId;
  final String? categoryName;
  final String confidence;
  final String why;

  /// A verified quote from the chosen category's description, when the
  /// model cited one and the words really are in there.
  final String? matchedPhrase;
  final String? chosenDescription;
  final List<DescriptionMention> mentions;

  /// Rules that already claim this merchant. Non-empty means the
  /// assistant was never going to be consulted for the next one.
  final List<ExistingMatcher> existingMatchers;
  final bool unsure;

  const CategorySuggestion({
    required this.confidence,
    required this.why,
    required this.unsure,
    required this.mentions,
    this.existingMatchers = const [],
    this.categoryId,
    this.categoryName,
    this.matchedPhrase,
    this.chosenDescription,
  });

  factory CategorySuggestion.fromJson(Map<String, dynamic> j) =>
      CategorySuggestion(
        categoryId: j['category_id'] as String?,
        categoryName: j['category_name'] as String?,
        confidence: j['confidence'] as String? ?? 'low',
        why: j['why'] as String? ?? '',
        matchedPhrase: j['matched_phrase'] as String?,
        chosenDescription: j['chosen_description'] as String?,
        mentions: ((j['mentions'] as List?) ?? const [])
            .map((e) =>
                DescriptionMention.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        existingMatchers: ((j['existing_matchers'] as List?) ?? const [])
            .map((e) =>
                ExistingMatcher.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        unsure: j['unsure'] as bool? ?? true,
      );

  /// True when something checkable backs the suggestion, rather than it
  /// being the model's general sense of the merchant. A high-confidence
  /// pick with nothing behind it is the failure mode worth showing.
  bool get hasEvidence => matchedPhrase != null || mentions.isNotEmpty;
}

/// One line from a receipt, and where the user has decided it goes.
class ReceiptItem {
  final String description;
  final int amountCents;
  final int quantity;

  /// Assigned by the user (or pre-filled by a suggestion). Null until
  /// someone decides.
  String? categoryId;
  String? categoryName;

  ReceiptItem({
    required this.description,
    required this.amountCents,
    this.quantity = 1,
    this.categoryId,
    this.categoryName,
  });

  factory ReceiptItem.fromJson(Map<String, dynamic> j) => ReceiptItem(
        description: j['description'] as String? ?? '',
        amountCents: j['amount_cents'] as int? ?? 0,
        quantity: j['quantity'] as int? ?? 1,
      );

  bool get isCredit => amountCents < 0;
}

/// A receipt, read but not yet filed.
class ParsedReceipt {
  final String merchant;
  final List<ReceiptItem> items;
  final int subtotalCents;
  final int taxCents;
  final int tipCents;
  final int totalCents;
  final int itemsSumCents;

  /// What the bank actually charged, when this is being matched to a
  /// transaction. The receipt and the charge can legitimately differ —
  /// a tip added afterwards is the usual reason.
  final int? expectedCents;
  final List<String> problems;
  final List<String> notices;

  const ParsedReceipt({
    required this.merchant,
    required this.items,
    required this.subtotalCents,
    required this.taxCents,
    required this.tipCents,
    required this.totalCents,
    required this.itemsSumCents,
    required this.problems,
    required this.notices,
    this.expectedCents,
  });

  factory ParsedReceipt.fromJson(Map<String, dynamic> j) => ParsedReceipt(
        merchant: j['merchant'] as String? ?? '',
        items: ((j['items'] as List?) ?? const [])
            .map((e) => ReceiptItem.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        subtotalCents: j['subtotal_cents'] as int? ?? 0,
        taxCents: j['tax_cents'] as int? ?? 0,
        tipCents: j['tip_cents'] as int? ?? 0,
        totalCents: j['total_cents'] as int? ?? 0,
        itemsSumCents: j['items_sum_cents'] as int? ?? 0,
        expectedCents: j['expected_cents'] as int?,
        problems: ((j['problems'] as List?) ?? const []).cast<String>(),
        notices: ((j['notices'] as List?) ?? const []).cast<String>(),
      );

  /// What the split has to add up to: the real charge if we know it,
  /// otherwise what the receipt claims.
  int get targetCents => expectedCents ?? totalCents;
}

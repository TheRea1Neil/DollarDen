/// Domain models. Every monetary field is integer cents and named `*Cents`.
library;

class Category {
  final String id;
  final String name;
  final String kind; // spending | savings_bucket | obligation
  final String? description;
  final int balanceCents;
  final int? targetCents;
  final int? alertAtCents;
  final int sortOrder;
  final bool archived;
  final bool belowAlert;
  final bool overspent;

  const Category({
    required this.id,
    required this.name,
    required this.kind,
    required this.balanceCents,
    required this.sortOrder,
    required this.archived,
    required this.belowAlert,
    required this.overspent,
    this.description,
    this.targetCents,
    this.alertAtCents,
  });

  factory Category.fromJson(Map<String, dynamic> j) => Category(
        id: j['id'] as String,
        name: j['name'] as String,
        kind: j['kind'] as String,
        description: j['description'] as String?,
        balanceCents: j['balance_cents'] as int,
        targetCents: j['target_cents'] as int?,
        alertAtCents: j['alert_at_cents'] as int?,
        sortOrder: j['sort_order'] as int? ?? 0,
        archived: j['archived'] as bool? ?? false,
        belowAlert: j['below_alert'] as bool? ?? false,
        overspent: j['overspent'] as bool? ?? false,
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

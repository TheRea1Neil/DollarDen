/// Analytics models.
///
/// The cards arrive from the server with every figure already computed
/// (see backend/app/analytics). Nothing here does arithmetic on money
/// beyond reading a field, so the screen cannot disagree with the
/// ledger.
library;

/// A resolved date range, as the server understood it.
///
/// Carried into the UI and shown, because "last 3 months" can
/// reasonably mean two different things and a question answered for
/// the wrong range reads exactly like one answered correctly.
class AnalyticsPeriod {
  final String start;
  final String end;
  final String label;

  const AnalyticsPeriod({
    required this.start,
    required this.end,
    required this.label,
  });

  factory AnalyticsPeriod.fromJson(Map<String, dynamic> j) => AnalyticsPeriod(
        start: j['start'] as String? ?? '',
        end: j['end'] as String? ?? '',
        label: j['label'] as String? ?? '',
      );
}

/// One row of a breakdown: a kind, an envelope, a shop or a month.
class SpendGroup {
  final String label;
  final int cents;
  final int count;
  final String? key;
  final int previousCents;
  final double sharePct;

  const SpendGroup({
    required this.label,
    required this.cents,
    required this.count,
    this.key,
    this.previousCents = 0,
    this.sharePct = 0,
  });

  factory SpendGroup.fromJson(Map<String, dynamic> j) => SpendGroup(
        label: j['label'] as String? ?? '',
        cents: j['cents'] as int? ?? 0,
        count: j['count'] as int? ?? 0,
        key: j['key'] as String?,
        previousCents: j['previous_cents'] as int? ?? 0,
        sharePct: (j['share_pct'] as num?)?.toDouble() ?? 0,
      );

  int get deltaCents => cents - previousCents;
}

class SpendResult {
  final String metric;
  final AnalyticsPeriod period;
  final int totalCents;
  final int count;
  final List<SpendGroup> groups;
  final int? previousTotalCents;
  final AnalyticsPeriod? previousPeriod;

  const SpendResult({
    required this.metric,
    required this.period,
    required this.totalCents,
    required this.count,
    this.groups = const [],
    this.previousTotalCents,
    this.previousPeriod,
  });

  factory SpendResult.fromJson(Map<String, dynamic> j) => SpendResult(
        metric: j['metric'] as String? ?? 'spend',
        period: AnalyticsPeriod.fromJson(
            ((j['period'] as Map?) ?? const {}).cast<String, dynamic>()),
        totalCents: j['total_cents'] as int? ?? 0,
        count: j['count'] as int? ?? 0,
        groups: ((j['groups'] as List?) ?? const [])
            .map((e) => SpendGroup.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        previousTotalCents: j['previous_total_cents'] as int?,
        previousPeriod: j['previous_period'] == null
            ? null
            : AnalyticsPeriod.fromJson(
                (j['previous_period'] as Map).cast<String, dynamic>()),
      );

  int? get deltaCents =>
      previousTotalCents == null ? null : totalCents - previousTotalCents!;
}

/// An insight card. Kept as a loose map on purpose.
///
/// The server decides which cards exist and in what order; a typed
/// class per kind would mean shipping an app update to show a new one,
/// and the shapes are read-only.
class InsightCard {
  final String kind;
  final String title;
  final Map<String, dynamic> data;

  const InsightCard({required this.kind, required this.title, required this.data});

  factory InsightCard.fromJson(Map<String, dynamic> j) => InsightCard(
        kind: j['kind'] as String? ?? '',
        title: j['title'] as String? ?? '',
        data: j,
      );

  int intAt(String key, [int fallback = 0]) => data[key] as int? ?? fallback;
  double dblAt(String key, [double fallback = 0]) =>
      (data[key] as num?)?.toDouble() ?? fallback;
  String? strAt(String key) => data[key] as String?;
  List<Map<String, dynamic>> listAt(String key) =>
      ((data[key] as List?) ?? const [])
          .map((e) => (e as Map).cast<String, dynamic>())
          .toList();
}

class Insights {
  final AnalyticsPeriod period;
  final List<InsightCard> cards;

  const Insights({required this.period, required this.cards});

  factory Insights.fromJson(Map<String, dynamic> j) => Insights(
        period: AnalyticsPeriod.fromJson(
            ((j['period'] as Map?) ?? const {}).cast<String, dynamic>()),
        cards: ((j['cards'] as List?) ?? const [])
            .map((e) => InsightCard.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
      );

  InsightCard? card(String kind) =>
      cards.where((c) => c.kind == kind).cast<InsightCard?>().firstOrNull;
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}

/// A kind of spending: the reporting axis, separate from the envelope.
class ExpenseType {
  final String id;
  final String name;
  final String slug;
  final String? description;
  final String createdBy;
  final bool reviewed;
  final int spendCents;
  final int txnCount;

  const ExpenseType({
    required this.id,
    required this.name,
    required this.slug,
    this.description,
    this.createdBy = 'user',
    this.reviewed = true,
    this.spendCents = 0,
    this.txnCount = 0,
  });

  factory ExpenseType.fromJson(Map<String, dynamic> j) => ExpenseType(
        id: j['id'] as String,
        name: j['name'] as String? ?? '',
        slug: j['slug'] as String? ?? '',
        description: j['description'] as String?,
        createdBy: j['created_by'] as String? ?? 'user',
        reviewed: j['reviewed'] as bool? ?? true,
        spendCents: j['spend_cents'] as int? ?? 0,
        txnCount: j['txn_count'] as int? ?? 0,
      );

  /// Coined by the assistant and not yet confirmed by the user.
  bool get needsReview => !reviewed;
}

/// A name offered in a picker: either already in use, or a standard
/// one that becomes real the first time something is filed under it.
class TypeSuggestion {
  final String name;
  final String description;
  final String status; // existing | available

  const TypeSuggestion({
    required this.name,
    required this.description,
    required this.status,
  });

  factory TypeSuggestion.fromJson(Map<String, dynamic> j) => TypeSuggestion(
        name: j['name'] as String? ?? '',
        description: j['description'] as String? ?? '',
        status: j['status'] as String? ?? 'available',
      );

  bool get inUse => status == 'existing';
}

class RecurringCharge {
  final String merchant;
  final String cadence;
  final int typicalCents;
  final int chargeCount;
  final String firstSeen;
  final String lastSeen;
  final String? nextExpected;
  final int annualCents;
  final double confidence;
  final bool likelyEnded;

  const RecurringCharge({
    required this.merchant,
    required this.cadence,
    required this.typicalCents,
    required this.chargeCount,
    required this.firstSeen,
    required this.lastSeen,
    this.nextExpected,
    required this.annualCents,
    required this.confidence,
    required this.likelyEnded,
  });

  factory RecurringCharge.fromJson(Map<String, dynamic> j) => RecurringCharge(
        merchant: j['merchant'] as String? ?? '',
        cadence: j['cadence'] as String? ?? '',
        typicalCents: j['typical_cents'] as int? ?? 0,
        chargeCount: j['charge_count'] as int? ?? 0,
        firstSeen: j['first_seen'] as String? ?? '',
        lastSeen: j['last_seen'] as String? ?? '',
        nextExpected: j['next_expected'] as String?,
        annualCents: j['annual_cents'] as int? ?? 0,
        confidence: (j['confidence'] as num?)?.toDouble() ?? 0,
        likelyEnded: j['likely_ended'] as bool? ?? false,
      );
}

class RecurringReport {
  final int monthlyCents;
  final int annualCents;
  final int activeCount;
  final List<RecurringCharge> items;

  const RecurringReport({
    required this.monthlyCents,
    required this.annualCents,
    required this.activeCount,
    required this.items,
  });

  factory RecurringReport.fromJson(Map<String, dynamic> j) => RecurringReport(
        monthlyCents: j['monthly_cents'] as int? ?? 0,
        annualCents: j['annual_cents'] as int? ?? 0,
        activeCount: j['active_count'] as int? ?? 0,
        items: ((j['items'] as List?) ?? const [])
            .map((e) => RecurringCharge.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
      );

  List<RecurringCharge> get live =>
      items.where((r) => !r.likelyEnded).toList(growable: false);
  List<RecurringCharge> get ended =>
      items.where((r) => r.likelyEnded).toList(growable: false);
}

/// One spending row behind a figure.
class SpendRow {
  final String transactionId;
  final String date;
  final String merchant;
  final int amountCents;
  final String category;
  final String expenseType;

  const SpendRow({
    required this.transactionId,
    required this.date,
    required this.merchant,
    required this.amountCents,
    required this.category,
    required this.expenseType,
  });

  factory SpendRow.fromJson(Map<String, dynamic> j) => SpendRow(
        transactionId: j['transaction_id'] as String? ?? '',
        date: j['date'] as String? ?? '',
        merchant: j['merchant'] as String? ?? '',
        amountCents: j['amount_cents'] as int? ?? 0,
        category: j['category'] as String? ?? '',
        expenseType: j['expense_type'] as String? ?? '',
      );
}

/// An answer to a plain-English question.
///
/// [query] is what the server actually asked the data, and the screen
/// shows it. Without that, a question the model read differently from
/// the way it was meant produces an answer that looks perfectly
/// confident and is about something else.
class SpendingAnswer {
  final String question;
  final String answer;
  final String metric;
  final AnalyticsPeriod period;
  final List<String> expenseTypes;
  final List<String> categories;
  final String? merchant;
  final SpendResult result;
  final List<String> unresolvedFilters;
  final bool periodNotUnderstood;

  const SpendingAnswer({
    required this.question,
    required this.answer,
    required this.metric,
    required this.period,
    required this.expenseTypes,
    required this.categories,
    this.merchant,
    required this.result,
    required this.unresolvedFilters,
    required this.periodNotUnderstood,
  });

  factory SpendingAnswer.fromJson(Map<String, dynamic> j) {
    final q = ((j['query'] as Map?) ?? const {}).cast<String, dynamic>();
    return SpendingAnswer(
      question: j['question'] as String? ?? '',
      answer: j['answer'] as String? ?? '',
      metric: q['metric'] as String? ?? 'spend',
      period: AnalyticsPeriod.fromJson(
          ((q['period'] as Map?) ?? const {}).cast<String, dynamic>()),
      expenseTypes:
          ((q['expense_types'] as List?) ?? const []).map((e) => '$e').toList(),
      categories:
          ((q['categories'] as List?) ?? const []).map((e) => '$e').toList(),
      merchant: q['merchant'] as String?,
      result: SpendResult.fromJson(
          ((j['result'] as Map?) ?? const {}).cast<String, dynamic>()),
      unresolvedFilters:
          ((j['unresolved_filters'] as List?) ?? const []).map((e) => '$e').toList(),
      periodNotUnderstood: j['period_not_understood'] as bool? ?? false,
    );
  }

  /// What the query was narrowed to, for the "asked as" line.
  String get filterSummary {
    final parts = <String>[
      ...expenseTypes,
      ...categories,
      if (merchant != null && merchant!.isNotEmpty) '"$merchant"',
    ];
    final what = parts.isEmpty ? 'everything' : parts.join(', ');
    return '$what · ${period.label}';
  }
}

/// What the assistant thinks a charge was, before anyone accepts it.
class TypeSuggestionResult {
  final String? name;
  final bool isNew;
  final String? description;
  final String confidence;
  final String why;
  final bool unsure;

  /// Set when the model proposed a new name that collapsed onto an
  /// existing type. Worth showing: it is evidence the guard is working.
  final String? collapsedFrom;

  const TypeSuggestionResult({
    this.name,
    required this.isNew,
    this.description,
    required this.confidence,
    required this.why,
    required this.unsure,
    this.collapsedFrom,
  });

  factory TypeSuggestionResult.fromJson(Map<String, dynamic> j) =>
      TypeSuggestionResult(
        name: j['expense_type_name'] as String?,
        isNew: j['is_new'] as bool? ?? false,
        description: j['new_description'] as String?,
        confidence: j['confidence'] as String? ?? 'low',
        why: j['why'] as String? ?? '',
        unsure: j['unsure'] as bool? ?? false,
        collapsedFrom: j['collapsed_from'] as String?,
      );
}

/// One line read off a receipt photo.
class ScannedLine {
  final String description;
  final int amountCents;
  final String category;
  final String? categoryId;

  /// The model could not account for this, or named a category that no
  /// longer exists. Shown so a bad line is visible rather than buried.
  final bool needsReview;

  const ScannedLine({
    required this.description,
    required this.amountCents,
    required this.category,
    this.categoryId,
    this.needsReview = false,
  });

  factory ScannedLine.fromJson(Map<String, dynamic> j) => ScannedLine(
        description: j['description'] as String? ?? '',
        amountCents: j['amount_cents'] as int? ?? 0,
        category: j['category'] as String? ?? 'Unsure',
        categoryId: j['category_id'] as String?,
        needsReview: j['needs_review'] as bool? ?? false,
      );

  ScannedLine copyWith({int? amountCents, String? category, String? categoryId}) =>
      ScannedLine(
        description: description,
        amountCents: amountCents ?? this.amountCents,
        category: category ?? this.category,
        categoryId: categoryId ?? this.categoryId,
        needsReview: needsReview,
      );
}

/// How a scanned receipt divides across envelopes — the number that
/// actually matters, as opposed to any individual line.
class CategorySplit {
  final String category;
  final String? categoryId;
  final int amountCents;

  const CategorySplit({
    required this.category,
    this.categoryId,
    required this.amountCents,
  });

  factory CategorySplit.fromJson(Map<String, dynamic> j) => CategorySplit(
        category: j['category'] as String? ?? 'Unsure',
        categoryId: j['category_id'] as String?,
        amountCents: j['amount_cents'] as int? ?? 0,
      );
}

class ReceiptScan {
  final List<ScannedLine> items;
  final List<CategorySplit> split;
  final int totalCents;
  final int targetCents;

  /// True when the model's own reading added up. False means a
  /// remainder line was needed — the total still matches the bank.
  final bool closedWithoutRemainder;
  final int remainderCents;
  final int toolCalls;
  final double seconds;
  final String model;

  const ReceiptScan({
    required this.items,
    required this.split,
    required this.totalCents,
    required this.targetCents,
    required this.closedWithoutRemainder,
    required this.remainderCents,
    required this.toolCalls,
    required this.seconds,
    required this.model,
  });

  factory ReceiptScan.fromJson(Map<String, dynamic> j) => ReceiptScan(
        items: ((j['items'] as List?) ?? const [])
            .map((e) => ScannedLine.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        split: ((j['category_split'] as List?) ?? const [])
            .map((e) => CategorySplit.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        totalCents: j['total_cents'] as int? ?? 0,
        targetCents: j['target_cents'] as int? ?? 0,
        closedWithoutRemainder: j['closed_without_remainder'] as bool? ?? false,
        remainderCents: j['remainder_cents'] as int? ?? 0,
        toolCalls: j['tool_calls'] as int? ?? 0,
        seconds: (j['seconds'] as num?)?.toDouble() ?? 0,
        model: j['model'] as String? ?? '',
      );

  /// Always true by construction — the backend reconciles before
  /// returning. Asserted here so a protocol change cannot slip past.
  bool get balances => totalCents == targetCents;

  int get reviewCount => items.where((i) => i.needsReview).length;
}

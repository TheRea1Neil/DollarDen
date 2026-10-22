/// A Dart port of `backend/app/budget/split.py`.
///
/// This exists so a paycheck preview is instant and local: dragging a
/// category should recalculate as fast as the finger moves, and a round
/// trip per drag is both slow and a source of desync between what is on
/// screen and what the next request assumes.
///
/// It is a PREVIEW engine. The server stays the source of truth and
/// recomputes authoritatively when the paycheck is applied — ADR 0006.
/// Two implementations of money logic can drift, so `split_engine_test`
/// asserts this one matches the Python engine's outputs on the same
/// fixtures used in experiments/allocation-arms.
///
/// Every amount is integer cents. There is no double arithmetic here.
library;

enum SplitRuleType { percent, fixed, refillTo, conditional, remainder }

SplitRuleType? splitRuleTypeFrom(String? wire) => switch (wire) {
      'percent' => SplitRuleType.percent,
      'fixed' => SplitRuleType.fixed,
      'refill_to' => SplitRuleType.refillTo,
      'conditional' => SplitRuleType.conditional,
      'remainder' => SplitRuleType.remainder,
      _ => null,
    };

class SplitPlanError implements Exception {
  final String message;
  const SplitPlanError(this.message);
  @override
  String toString() => message;
}

class SplitRule {
  final String categoryId;
  final int orderIndex;
  final SplitRuleType ruleType;
  final Map<String, dynamic> params;
  final bool enabled;

  const SplitRule({
    required this.categoryId,
    required this.orderIndex,
    required this.ruleType,
    this.params = const {},
    this.enabled = true,
  });
}

class SplitAllocation {
  final String categoryId;
  final int amountCents;
  final SplitRuleType ruleType;
  const SplitAllocation(this.categoryId, this.amountCents, this.ruleType);
}

class SplitResult {
  final List<SplitAllocation> allocations;

  /// (categoryId, shortfall) for rules that asked for more than was left.
  final List<(String, int)> unfunded;
  final int remainingCents;

  const SplitResult(this.allocations, this.unfunded, this.remainingCents);

  int get totalCents =>
      allocations.fold(0, (sum, a) => sum + a.amountCents);

  Map<String, int> get byCategory {
    final out = <String, int>{};
    for (final a in allocations) {
      out[a.categoryId] = (out[a.categoryId] ?? 0) + a.amountCents;
    }
    return out;
  }
}

/// `amount * rate`, rounded half-up to the nearest cent.
///
/// The rate arrives as a decimal string ("0.10"). It is turned into an
/// exact integer fraction rather than a double, because 10% of $1,234.57
/// has to be the same stable number here as it is on the server.
int percentOf(int amountCents, String rate) {
  final text = rate.trim();
  if (text.isEmpty) throw const SplitPlanError('percent rule has no rate');

  final negative = text.startsWith('-');
  final body = negative ? text.substring(1) : text;
  final dot = body.indexOf('.');
  final digits = dot < 0 ? body : body.replaceFirst('.', '');
  final decimals = dot < 0 ? 0 : body.length - dot - 1;

  final numerator = int.tryParse(digits);
  if (numerator == null) {
    throw SplitPlanError('percent rate is not a number: $rate');
  }
  var denominator = 1;
  for (var i = 0; i < decimals; i++) {
    denominator *= 10;
  }
  if (negative || numerator > denominator) {
    throw SplitPlanError('percent rate out of range: $rate');
  }

  final product = amountCents * numerator;
  // Half-up: add half the denominator before the integer division.
  return (product + denominator ~/ 2) ~/ denominator;
}

int _asInt(dynamic v) {
  if (v is int) return v;
  if (v is num) return v.round();
  if (v is String) return int.tryParse(v) ?? 0;
  throw const SplitPlanError('expected a whole number of cents');
}

bool _predicateHolds(
    Map<String, dynamic> when, String categoryId, Map<String, int> balances) {
  final target = (when['category_id'] as String?) ?? categoryId;
  final field = (when['field'] as String?) ?? 'balance';
  if (field != 'balance') {
    throw SplitPlanError('unsupported predicate field: $field');
  }
  // Mirrors the server: a predicate missing its operator or threshold is
  // a malformed plan, not something to guess at.
  for (final key in ['value_cents', 'op']) {
    final v = when[key];
    if (v == null || v == '') {
      throw SplitPlanError(
          "conditional rule is missing '$key'; it cannot be evaluated");
    }
  }
  final current = balances[target] ?? 0;
  final value = _asInt(when['value_cents']);
  return switch (when['op'] as String) {
    '<' => current < value,
    '<=' => current <= value,
    '>' => current > value,
    '>=' => current >= value,
    '==' => current == value,
    _ => throw SplitPlanError(
        'unsupported predicate operator: ${when['op']}'),
  };
}

int _desiredAmount(
    SplitRule rule, int gross, int remaining, Map<String, int> balances) {
  final p = rule.params;
  switch (rule.ruleType) {
    case SplitRuleType.percent:
      final base = p['of'] == 'remaining' ? remaining : gross;
      return percentOf(base, '${p['rate'] ?? ''}');

    case SplitRuleType.fixed:
      if (p['amount_cents'] == null) {
        throw const SplitPlanError('fixed rule has no amount');
      }
      return _asInt(p['amount_cents']);

    case SplitRuleType.refillTo:
      if (p['target_cents'] == null) {
        throw const SplitPlanError('refill rule has no target');
      }
      final target = _asInt(p['target_cents']);
      final current = balances[rule.categoryId] ?? 0;
      final need = target - current;
      final want = need > 0 ? need : 0;
      final cap = p['max_contribution_cents'];
      if (cap == null) return want;
      final capped = _asInt(cap);
      return want < capped ? want : capped;

    case SplitRuleType.conditional:
      final when = (p['when'] as Map?)?.cast<String, dynamic>() ?? const {};
      if (!_predicateHolds(when, rule.categoryId, balances)) return 0;
      final then = (p['then'] as Map?)?.cast<String, dynamic>() ?? const {};
      final innerType = splitRuleTypeFrom(then['rule_type'] as String?);
      if (innerType == null) {
        throw const SplitPlanError('conditional rule has no inner rule');
      }
      if (innerType == SplitRuleType.conditional) {
        throw const SplitPlanError('conditional rules may not nest');
      }
      return _desiredAmount(
        SplitRule(
          categoryId: rule.categoryId,
          orderIndex: rule.orderIndex,
          ruleType: innerType,
          params: (then['params'] as Map?)?.cast<String, dynamic>() ?? const {},
        ),
        gross,
        remaining,
        balances,
      );

    case SplitRuleType.remainder:
      return remaining;
  }
}

/// Split [grossCents] across categories per [rules].
///
/// [balances] maps category id to current balance, which `refill_to` and
/// `conditional` rules need.
SplitResult applySplit(
  int grossCents,
  List<SplitRule> rules,
  Map<String, int> balances,
) {
  if (grossCents < 0) {
    throw const SplitPlanError('gross must not be negative');
  }

  final active = [...rules.where((r) => r.enabled)]
    ..sort((a, b) => a.orderIndex.compareTo(b.orderIndex));
  if (active.isEmpty) {
    throw const SplitPlanError('a plan needs at least one rule');
  }

  final remainders =
      active.where((r) => r.ruleType == SplitRuleType.remainder).length;
  if (remainders != 1) {
    // Unallocated money is a correctness hole, so this is fatal.
    throw SplitPlanError(
        'a split plan needs exactly one remainder rule, found $remainders');
  }
  if (active.last.ruleType != SplitRuleType.remainder) {
    throw const SplitPlanError('the remainder rule must be last');
  }

  final allocations = <SplitAllocation>[];
  final unfunded = <(String, int)>[];
  var remaining = grossCents;

  for (final rule in active) {
    final wanted = _desiredAmount(rule, grossCents, remaining, balances);
    if (wanted <= 0) continue;

    final granted = wanted < remaining ? wanted : remaining;
    if (granted < wanted) {
      unfunded.add((rule.categoryId, wanted - granted));
    }
    if (granted > 0) {
      allocations.add(SplitAllocation(rule.categoryId, granted, rule.ruleType));
      remaining -= granted;
    }
  }

  return SplitResult(allocations, unfunded, remaining);
}

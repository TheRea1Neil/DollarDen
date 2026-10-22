import 'package:flutter_test/flutter_test.dart';
import 'package:nnf/domain/models.dart';

Map<String, dynamic> _cat({
  int balance = 0,
  int? target,
  bool overspent = false,
  bool belowAlert = false,
}) =>
    {
      'id': 'c1',
      'name': 'Food',
      'kind': 'spending',
      'balance_cents': balance,
      'target_cents': target,
      'alert_at_cents': null,
      'sort_order': 0,
      'archived': false,
      'below_alert': belowAlert,
      'overspent': overspent,
    };

void main() {
  group('Category.fillRatio', () {
    test('is null without a target to measure against', () {
      expect(Category.fromJson(_cat(balance: 5000)).fillRatio, isNull);
    });

    test('is the fraction of target held', () {
      expect(Category.fromJson(_cat(balance: 15000, target: 30000)).fillRatio, 0.5);
    });

    test('clamps rather than overflowing the bar', () {
      expect(Category.fromJson(_cat(balance: 60000, target: 30000)).fillRatio, 1.0);
      expect(Category.fromJson(_cat(balance: -1200, target: 30000)).fillRatio, 0.0);
    });
  });

  test('ReviewItem prefers the merchant name when there is one', () {
    final withMerchant = ReviewItem.fromJson({
      'id': 'r', 'transaction_id': 't', 'reason': 'unknown_merchant',
      'date': '2026-10-21', 'name': 'SQ *COFFEE 1234',
      'merchant_name': 'Blue Bottle', 'amount_cents': 550,
    });
    expect(withMerchant.title, 'Blue Bottle');

    final withoutMerchant = ReviewItem.fromJson({
      'id': 'r', 'transaction_id': 't', 'reason': 'unknown_merchant',
      'date': '2026-10-21', 'name': 'SQ *COFFEE 1234',
      'merchant_name': null, 'amount_cents': 550,
    });
    expect(withoutMerchant.title, 'SQ *COFFEE 1234');
  });

  _proposalTests();

  test('summary tolerates missing optional sections', () {
    final s = BudgetSummary.fromJson({
      'categories': [_cat(balance: 100)],
      'total_allocated_cents': 100,
      'next_payday': {'known': false, 'reason': 'not enough data'},
    });
    expect(s.categories, hasLength(1));
    expect(s.alerts, isEmpty);
    expect(s.overspent, isEmpty);
    expect(s.pendingReview, 0);
    expect(s.nextPayday.known, isFalse);
  });
}

void _proposalTests() {
  group('ProposedRule.describe', () {
    ProposedRule rule(String type, Map<String, dynamic> params) => ProposedRule(
          categoryName: 'X', orderIndex: 1, ruleType: type, params: params, why: '');

    test('renders each rule type in plain English', () {
      expect(rule('percent', {'rate': '0.10', 'of': 'gross'}).describe(),
          '10% of gross pay');
      expect(rule('percent', {'rate': '0.05', 'of': 'remaining'}).describe(),
          "5% of what's left");
      expect(rule('fixed', {'amount_cents': 35000}).describe(), r'$350.00');
      expect(rule('refill_to', {'target_cents': 30000}).describe(),
          r'top up to $300.00');
      expect(rule('remainder', {}).describe(), 'everything left over');
    });

    test('renders a conditional rule', () {
      final r = rule('conditional', {
        'when': {'field': 'balance', 'op': '<', 'value_cents': 45000},
        'then': {'rule_type': 'fixed', 'params': {'amount_cents': 5000}},
      });
      expect(r.describe(), r'if below $450.00, add $50.00');
    });

    test('degrades gracefully on missing params rather than crashing', () {
      // The model does sometimes omit params; the review screen must still
      // render so the user can see and fix it.
      expect(rule('fixed', {}).describe(), '—');
      expect(rule('conditional', {}).describe(), contains('—'));
    });
  });

  group('Proposal', () {
    test('separates blocking problems from informational notices', () {
      final p = Proposal.fromJson({
        'categories': [
          {'name': 'Food', 'kind': 'spending', 'description': 'x',
           'already_exists': true},
        ],
        'split_rules': [],
        'notes': 'n',
        'problems': ['missing remainder'],
        'notices': ["'Food' already exists and will be reused"],
        'applicable': false,
      });
      expect(p.applicable, isFalse);
      expect(p.problems, hasLength(1));
      expect(p.notices, hasLength(1));
      expect(p.categories.first.alreadyExists, isTrue);
    });

    test('round-trips an edited category back to JSON', () {
      final c = ProposedCategory(
          name: 'Food', kind: 'spending', description: 'd',
          alreadyExists: false, targetCents: 30000);
      c.name = 'Groceries';
      c.targetCents = 25000;
      expect(c.toJson(), {
        'name': 'Groceries', 'kind': 'spending',
        'description': 'd', 'target_cents': 25000,
      });
    });
  });

  group('AiJob', () {
    test('names the waking stage, since a silent 10s looks like a hang', () {
      AiJob j(String s) => AiJob(id: 'i', kind: 'k', status: s);
      expect(j('waking').progressLabel, contains('Waking'));
      expect(j('queued').finished, isFalse);
      expect(j('done').ok, isTrue);
      expect(j('failed').finished, isTrue);
      expect(j('failed').ok, isFalse);
    });
  });
}

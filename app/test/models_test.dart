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

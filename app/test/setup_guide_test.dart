import 'package:flutter_test/flutter_test.dart';
import 'package:nnf/domain/models.dart';
import 'package:nnf/ui/widgets/setup_guide.dart';

/// The order is the point: you cannot divide money before you know how
/// much there is, and you cannot divide it into envelopes that do not
/// exist. Each test pins one rung of that ladder.
Category _cat(String name, int cents, {bool unallocated = false}) => Category(
      id: name,
      name: name,
      kind: 'spending',
      balanceCents: cents,
      sortOrder: 0,
      archived: false,
      belowAlert: false,
      overspent: false,
      isUnallocated: unallocated,
    );

BudgetSummary _summary({
  List<Category> categories = const [],
  int cash = 0,
  int debt = 0,
  int allocated = 0,
  bool hasAccounts = false,
}) =>
    BudgetSummary(
      categories: categories,
      totalAllocatedCents: allocated,
      alerts: const [],
      overspent: const [],
      pendingReview: 0,
      nextPayday: const NextPayday(known: false),
      ledgerDrift: const [],
      cards: const [],
      cardDebtCents: debt,
      cashAccounts: hasAccounts
          ? [const CashAccount(id: 'a', name: 'Checking', balanceCents: 0)]
          : const [],
      cashCents: cash,
      excludedCount: 0,
    );

void main() {
  _pendingTests();
  test('a brand new budget is told to connect a bank', () {
    expect(SetupState.from(_summary()).stage, SetupStage.connectBank);
  });

  test('accounts with no balance means none are counted yet', () {
    final s = SetupState.from(_summary(hasAccounts: true));
    expect(s.stage, SetupStage.chooseAccounts);
  });

  test('money but no envelopes asks for categories first', () {
    final s = SetupState.from(_summary(hasAccounts: true, cash: 200000));
    expect(s.stage, SetupStage.makeCategories);
    expect(s.availableCents, 200000);
  });

  test('the Unallocated sink does not count as having made a budget', () {
    final s = SetupState.from(_summary(
      hasAccounts: true,
      cash: 200000,
      categories: [_cat('Unallocated', 0, unallocated: true)],
    ));
    expect(s.stage, SetupStage.makeCategories,
        reason: 'the system made that one, not the user');
  });

  test('envelopes and undivided money asks for allocation', () {
    final s = SetupState.from(_summary(
      hasAccounts: true,
      cash: 200000,
      categories: [_cat('Rent', 0)],
    ));
    expect(s.stage, SetupStage.allocate);
    expect(s.unallocatedCents, 200000);
  });

  test('card debt is subtracted from what there is to divide', () {
    final s = SetupState.from(_summary(
      hasAccounts: true,
      cash: 200000,
      debt: 50000,
      categories: [_cat('Rent', 0)],
    ));
    expect(s.availableCents, 150000,
        reason: 'money already owed on a card is not yours to budget');
    expect(s.unallocatedCents, 150000);
  });

  test('a fully allocated budget shows nothing', () {
    final s = SetupState.from(_summary(
      hasAccounts: true,
      cash: 200000,
      allocated: 200000,
      categories: [_cat('Rent', 200000)],
    ));
    expect(s.stage, SetupStage.done);
  });

  test('over-allocating does not reopen the guide', () {
    final s = SetupState.from(_summary(
      hasAccounts: true,
      cash: 100000,
      allocated: 150000,
      categories: [_cat('Rent', 150000)],
    ));
    expect(s.stage, SetupStage.done,
        reason: 'that is an overspend to fix, not setup to finish');
  });

  test('debt exceeding cash leaves nothing to divide', () {
    final s = SetupState.from(_summary(
      hasAccounts: true,
      cash: 50000,
      debt: 80000,
      categories: [_cat('Rent', 0)],
    ));
    expect(s.availableCents, -30000);
    expect(s.stage, SetupStage.done,
        reason: 'there is no money to allocate; prompting would be absurd');
  });
}

void _pendingTests() {
  group('a pending charge is not an unallocated dollar', () {
    // From the real account: $30 insurance on a credit card, filed
    // correctly so the envelope dropped $30, but the card balance had
    // not risen yet because the charge was pending. The guide told the
    // user to divide up $30 they had already assigned.
    SetupState state({required int pending}) => SetupState(
          stage: SetupStage.done,
          availableCents: 861450,
          allocatedCents: 858450,
          cashCents: 870767,
          cardDebtCents: 9317,
          realCategoryCount: 7,
          pendingCents: pending,
        );

    test('the gap is recognised as pending', () {
      final s = state(pending: 3000);
      expect(s.unallocatedCents, 3000);
      expect(s.gapIsPending, isTrue);
    });

    test('without pending it is a genuine gap', () {
      expect(state(pending: 0).gapIsPending, isFalse);
    });

    test('a gap larger than pending is still the user to close', () {
      final s = SetupState(
        stage: SetupStage.done,
        availableCents: 100000,
        allocatedCents: 50000,
        cashCents: 100000,
        cardDebtCents: 0,
        realCategoryCount: 3,
        pendingCents: 1000,
      );
      expect(s.gapIsPending, isFalse,
          reason: 'money with no home must not be explained away');
    });
  });
}

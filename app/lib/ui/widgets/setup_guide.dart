import 'package:flutter/material.dart';

import '../../core/money.dart';
import '../../domain/models.dart';

/// Where a budget is in getting started, and the one thing to do next.
///
/// The order matters and is not arbitrary: you cannot sensibly divide
/// money before you know how much there is, and you cannot divide it
/// into envelopes that do not exist yet. So the guide refuses to show
/// step four until steps one to three are done, rather than presenting
/// four things at once and letting someone start in the middle.
enum SetupStage {
  /// No accounts at all.
  connectBank,

  /// Accounts exist but none counts toward the budget.
  chooseAccounts,

  /// There is money, but nowhere to put it.
  makeCategories,

  /// Envelopes exist and there is money still undivided.
  allocate,

  /// Nothing to prompt.
  done,
}

class SetupState {
  final SetupStage stage;

  /// Bank balances minus card debt — what there actually is to divide.
  final int availableCents;

  /// Already sitting in envelopes.
  final int allocatedCents;
  final int cashCents;
  final int cardDebtCents;
  final int realCategoryCount;

  /// Already filed, not yet settled at the bank.
  final int pendingCents;

  const SetupState({
    required this.stage,
    required this.availableCents,
    required this.allocatedCents,
    required this.cashCents,
    required this.cardDebtCents,
    required this.realCategoryCount,
    this.pendingCents = 0,
  });

  int get unallocatedCents => availableCents - allocatedCents;

  /// Whether the apparent shortfall is just charges in flight.
  ///
  /// Without this the guide tells someone to divide up $30 they have
  /// already assigned, because a pending card charge lowered the
  /// envelope before it raised the card balance. Sending them to hunt
  /// for a transaction they have already filed is worse than saying
  /// nothing.
  bool get gapIsPending =>
      unallocatedCents != 0 &&
      pendingCents > 0 &&
      unallocatedCents.abs() <= pendingCents;

  /// Reading the budget's state rather than storing a setup flag:
  /// a flag goes stale the moment someone archives their last
  /// category or disconnects a bank, and then the app insists setup is
  /// finished while the budget is empty.
  factory SetupState.from(BudgetSummary s) {
    // The Unallocated sink is created by the system, so it does not
    // count as the user having made an envelope.
    final real = s.categories.where((c) => !c.archived && !c.isUnallocated).length;
    final available = s.cashCents - s.cardDebtCents;

    final SetupStage stage;
    if (s.cashAccounts.isEmpty && s.cards.isEmpty) {
      stage = SetupStage.connectBank;
    } else if (s.cashCents == 0 && s.cardDebtCents == 0) {
      stage = SetupStage.chooseAccounts;
    } else if (real == 0) {
      stage = SetupStage.makeCategories;
    } else if (available - s.totalAllocatedCents > 0 &&
        (available - s.totalAllocatedCents) > s.pendingCents) {
      stage = SetupStage.allocate;
    } else {
      stage = SetupStage.done;
    }

    return SetupState(
      stage: stage,
      availableCents: available,
      allocatedCents: s.totalAllocatedCents,
      cashCents: s.cashCents,
      cardDebtCents: s.cardDebtCents,
      realCategoryCount: real,
      pendingCents: s.pendingCents,
    );
  }
}

/// The card at the top of an unfinished budget.
///
/// Shows the sum that matters — bank minus cards — because that is the
/// number people get wrong. Money sitting in checking that is already
/// owed on a credit card is not yours to budget, and a budget built on
/// the checking balance alone is over-committed from day one.
class SetupGuide extends StatelessWidget {
  final SetupState state;
  final VoidCallback onConnectBank;
  final VoidCallback onChooseAccounts;
  final VoidCallback onNewCategory;
  final VoidCallback onAllocate;

  /// Opens Goals, where a budget is described in plain words and a set
  /// of envelopes is suggested from it.
  final VoidCallback onDescribeBudget;

  const SetupGuide({
    super.key,
    required this.state,
    required this.onConnectBank,
    required this.onChooseAccounts,
    required this.onNewCategory,
    required this.onAllocate,
    required this.onDescribeBudget,
  });

  @override
  Widget build(BuildContext context) {
    if (state.stage == SetupStage.done) return const SizedBox.shrink();
    final theme = Theme.of(context);

    final (String title, String body, String action, VoidCallback onTap) =
        switch (state.stage) {
      SetupStage.connectBank => (
        'Start by connecting your bank',
        'Everything else follows from knowing what you actually have. '
            'Nothing is budgeted until then.',
        'Connect a bank',
        onConnectBank,
      ),
      SetupStage.chooseAccounts => (
        'Choose which accounts count',
        'Pick the current accounts and credit cards this budget should '
            'cover. Loans, pensions and investments are usually left out.',
        'Choose accounts',
        onChooseAccounts,
      ),
      SetupStage.makeCategories => (
        'Now make some envelopes',
        'You have ${Money.format(state.availableCents)} to divide, but '
            'nowhere to put it yet. The quickest way is to describe your '
            'money in your own words — how you are paid, what has to be '
            'paid, what you are saving for — and get a full set of '
            'envelopes suggested, which you can then edit.',
        'Describe your budget',
        onDescribeBudget,
      ),
      SetupStage.allocate => (
        'Divide up ${Money.format(state.unallocatedCents)}',
        '${state.realCategoryCount} envelope'
            '${state.realCategoryCount == 1 ? '' : 's'} ready and '
            '${Money.format(state.unallocatedCents)} still undivided. '
            'Say what each one should hold.',
        'Allocate it',
        onAllocate,
      ),
      SetupStage.done => ('', '', '', onAllocate),
    };

    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      color: theme.colorScheme.secondaryContainer,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.flag_outlined,
                    size: 18, color: theme.colorScheme.onSecondaryContainer),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(title,
                      style: theme.textTheme.titleMedium?.copyWith(
                          color: theme.colorScheme.onSecondaryContainer)),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(body,
                style: theme.textTheme.bodyMedium?.copyWith(
                    color: theme.colorScheme.onSecondaryContainer)),

            // The arithmetic, spelled out. "Available" is the number
            // people get wrong, and showing the subtraction is the
            // only way it explains itself.
            if (state.stage == SetupStage.makeCategories ||
                state.stage == SetupStage.allocate) ...[
              const SizedBox(height: 12),
              _Sum(state: state),
            ],

            const SizedBox(height: 14),
            Row(
              children: [
                FilledButton(onPressed: onTap, child: Text(action)),
                // Describing the budget is offered first because naming
                // envelopes from a blank page is the step people stall
                // on. Building one by hand stays one tap away for anyone
                // who already knows what they want.
                if (state.stage == SetupStage.makeCategories) ...[
                  const SizedBox(width: 8),
                  TextButton(
                    onPressed: onNewCategory,
                    child: const Text('Add one by hand'),
                  ),
                ],
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _Sum extends StatelessWidget {
  final SetupState state;
  const _Sum({required this.state});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final on = theme.colorScheme.onSecondaryContainer;
    Widget row(String label, int cents, {bool strong = false, bool minus = false}) =>
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 2),
          child: Row(
            children: [
              Expanded(
                child: Text(label,
                    style: theme.textTheme.bodySmall?.copyWith(color: on)),
              ),
              Text(
                '${minus ? '− ' : ''}${Money.format(cents.abs())}',
                style: (strong
                        ? theme.textTheme.bodyMedium
                        : theme.textTheme.bodySmall)
                    ?.copyWith(
                        color: on,
                        fontWeight: strong ? FontWeight.w700 : null),
              ),
            ],
          ),
        );

    return Column(
      children: [
        row('In the bank', state.cashCents),
        row('Owed on cards', state.cardDebtCents, minus: true),
        Divider(height: 12, color: on.withValues(alpha: 0.3)),
        row('Yours to divide', state.availableCents, strong: true),
        if (state.allocatedCents != 0) ...[
          row('Already in envelopes', state.allocatedCents, minus: true),
          row('Still undivided', state.unallocatedCents, strong: true),
        ],
      ],
    );
  }
}

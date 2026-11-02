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

  const SetupState({
    required this.stage,
    required this.availableCents,
    required this.allocatedCents,
    required this.cashCents,
    required this.cardDebtCents,
    required this.realCategoryCount,
  });

  int get unallocatedCents => availableCents - allocatedCents;

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
    } else if (available - s.totalAllocatedCents > 0) {
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

  const SetupGuide({
    super.key,
    required this.state,
    required this.onConnectBank,
    required this.onChooseAccounts,
    required this.onNewCategory,
    required this.onAllocate,
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
            'nowhere to put it yet. Make the envelopes first — rent, '
            'groceries, fuel, whatever you actually spend on — then share '
            'the money out between them.',
        'Add a category',
        onNewCategory,
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
            Align(
              alignment: Alignment.centerLeft,
              child: FilledButton(onPressed: onTap, child: Text(action)),
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

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../core/theme.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';
import '../widgets/category_tile.dart';

class HomeScreen extends ConsumerWidget {
  const HomeScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final summary = ref.watch(summaryProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Budget'),
        actions: [
          IconButton(
            tooltip: 'Sign out',
            icon: const Icon(Icons.logout),
            onPressed: () => ref.read(authStateProvider.notifier).logout(),
          ),
        ],
      ),
      body: summary.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => _ErrorView(
          message: '$e',
          onRetry: () => ref.invalidate(summaryProvider),
        ),
        data: (s) => RefreshIndicator(
          onRefresh: () async {
            ref.invalidate(summaryProvider);
            await ref.read(summaryProvider.future);
          },
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
            children: [
              _TotalCard(summary: s),
              if (s.ledgerDrift.isNotEmpty) ...[
                const SizedBox(height: 12),
                _DriftWarning(count: s.ledgerDrift.length),
              ],
              if (s.cards.any((c) => c.owes)) ...[
                const SizedBox(height: 20),
                _CardsSection(cards: s.cards, totalCents: s.cardDebtCents),
              ],
              const SizedBox(height: 20),
              if (s.categories.isEmpty)
                const _EmptyState()
              else
                Card(
                  child: Column(
                    children: [
                      for (var i = 0; i < s.categories.length; i++) ...[
                        if (i > 0) const Divider(height: 1, indent: 16, endIndent: 16),
                        CategoryTile(category: s.categories[i]),
                      ],
                    ],
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _TotalCard extends StatelessWidget {
  final BudgetSummary summary;
  const _TotalCard({required this.summary});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final payday = summary.nextPayday;

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Allocated',
                style: theme.textTheme.labelLarge
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            const SizedBox(height: 4),
            Text(
              Money.format(summary.totalAllocatedCents),
              style: theme.textTheme.displaySmall?.copyWith(
                fontWeight: FontWeight.w700,
                fontFeatures: const [FontFeature.tabularFigures()],
              ),
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                if (payday.known)
                  _Chip(
                    icon: Icons.event,
                    // A range, never a single date: the cadence estimate
                    // cannot support that much precision (docs/06).
                    label: 'Payday ~${_short(payday.expected)}',
                    tone: theme.colorScheme.primary,
                  ),
                if (summary.pendingReview > 0)
                  _Chip(
                    icon: Icons.inbox,
                    label: '${summary.pendingReview} to review',
                    tone: AppTheme.warning,
                  ),
                if (summary.overspent.isNotEmpty)
                  _Chip(
                    icon: Icons.error_outline,
                    label: '${summary.overspent.length} overspent',
                    tone: AppTheme.danger,
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  static String _short(String? iso) {
    if (iso == null) return '?';
    final d = DateTime.tryParse(iso);
    if (d == null) return iso;
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return '${months[d.month - 1]} ${d.day}';
  }
}

class _Chip extends StatelessWidget {
  final IconData icon;
  final String label;
  final Color tone;
  const _Chip({required this.icon, required this.label, required this.tone});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: tone.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 15, color: tone),
          const SizedBox(width: 6),
          Text(label,
              style: Theme.of(context)
                  .textTheme
                  .labelMedium
                  ?.copyWith(color: tone, fontWeight: FontWeight.w600)),
        ],
      ),
    );
  }
}

/// Surfaced, never hidden. If the cached balances disagree with the ledger
/// the user should know the numbers are suspect.
class _DriftWarning extends StatelessWidget {
  final int count;
  const _DriftWarning({required this.count});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Theme.of(context).colorScheme.errorContainer,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        children: [
          const Icon(Icons.warning_amber_rounded),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              '$count categor${count == 1 ? 'y' : 'ies'} disagree with the ledger. '
              'These balances may be wrong.',
              style: TextStyle(color: Theme.of(context).colorScheme.onErrorContainer),
            ),
          ),
        ],
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 48),
      child: Column(
        children: [
          Icon(Icons.account_balance_wallet_outlined,
              size: 48, color: theme.colorScheme.onSurfaceVariant),
          const SizedBox(height: 16),
          Text('No categories yet', style: theme.textTheme.titleMedium),
          const SizedBox(height: 6),
          Text(
            'Create categories to start allocating each paycheck.',
            textAlign: TextAlign.center,
            style: theme.textTheme.bodyMedium
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
          ),
        ],
      ),
    );
  }
}

class _ErrorView extends StatelessWidget {
  final String message;
  final VoidCallback onRetry;
  const _ErrorView({required this.message, required this.onRetry});

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.cloud_off, size: 44),
            const SizedBox(height: 16),
            Text(message, textAlign: TextAlign.center),
            const SizedBox(height: 20),
            FilledButton.tonal(onPressed: onRetry, child: const Text('Retry')),
          ],
        ),
      ),
    );
  }
}


/// Outstanding card balances, shown apart from the budget on purpose.
///
/// Card spending already drew categories down at purchase time. This is a
/// reminder of what needs paying, not money to allocate -- which is why it
/// never joins the category total.
class _CardsSection extends StatelessWidget {
  final List<CreditCard> cards;
  final int totalCents;
  const _CardsSection({required this.cards, required this.totalCents});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final owed = cards.where((c) => c.owes).toList();

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.credit_card,
                    size: 18, color: theme.colorScheme.onSurfaceVariant),
                const SizedBox(width: 8),
                Text('To pay off',
                    style: theme.textTheme.labelLarge
                        ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                const Spacer(),
                Text(
                  Money.format(totalCents),
                  style: theme.textTheme.titleMedium?.copyWith(
                    fontWeight: FontWeight.w700,
                    fontFeatures: const [FontFeature.tabularFigures()],
                  ),
                ),
              ],
            ),
            const SizedBox(height: 10),
            for (final c in owed)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 3),
                child: Row(
                  children: [
                    Expanded(
                      child: Text(c.label,
                          style: theme.textTheme.bodyMedium,
                          overflow: TextOverflow.ellipsis),
                    ),
                    Text(
                      Money.format(c.balanceCents),
                      style: theme.textTheme.bodyMedium?.copyWith(
                        fontFeatures: const [FontFeature.tabularFigures()],
                      ),
                    ),
                  ],
                ),
              ),
            const SizedBox(height: 8),
            Text(
              'Already budgeted when you spent it. Paying the card will not '
              'change your category balances.',
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
          ],
        ),
      ),
    );
  }
}

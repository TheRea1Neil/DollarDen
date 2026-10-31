import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../core/theme.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';
import '../widgets/category_tile.dart';
import 'add_category_screen.dart';
import 'category_editor_screen.dart';
import 'history_screen.dart';
import 'insights_screen.dart';
import 'move_money_screen.dart';
import 'paycheck_screen.dart';

/// Offer both ways to add a category.
///
/// The assistant is genuinely faster for "money for the dog, vet visits
/// come out of nowhere", and genuinely slower than typing four fields when
/// the user already knows exactly what they want.
Future<void> _newCategory(BuildContext context) async {
  final route = await showModalBottomSheet<String>(
    context: context,
    showDragHandle: true,
    builder: (ctx) => SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          ListTile(
            leading: const Icon(Icons.auto_awesome),
            title: const Text('Describe it'),
            subtitle: const Text(
                'Say what it is for in your own words and the assistant '
                'drafts it, including how to fund it.'),
            onTap: () => Navigator.pop(ctx, 'ai'),
          ),
          ListTile(
            leading: const Icon(Icons.edit_outlined),
            title: const Text('Set it up myself'),
            subtitle: const Text('Name, kind, target. No assistant.'),
            onTap: () => Navigator.pop(ctx, 'manual'),
          ),
        ],
      ),
    ),
  );
  if (route == null || !context.mounted) return;
  await Navigator.of(context).push(
    MaterialPageRoute<bool>(
      builder: (_) => route == 'ai'
          ? const AddCategoryScreen()
          : const CategoryEditorScreen(),
    ),
  );
}

/// Entering a paycheck by hand was a testing affordance — real ones
/// arrive from the bank and are picked up by sync. Kept behind a flag
/// rather than deleted, since the screen is still useful for seeding a
/// test account.
const bool _showManualPaycheck = false;


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
            tooltip: 'Move money',
            icon: const Icon(Icons.swap_horiz),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute<void>(builder: (_) => const MoveMoneyScreen()),
            ),
          ),
          IconButton(
            tooltip: 'Insights',
            icon: const Icon(Icons.insights_outlined),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute<void>(builder: (_) => const InsightsScreen()),
            ),
          ),
          IconButton(
            tooltip: 'History',
            icon: const Icon(Icons.receipt_long_outlined),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute<void>(builder: (_) => const HistoryScreen()),
            ),
          ),
          IconButton(
            tooltip: 'New category',
            icon: const Icon(Icons.add),
            onPressed: () => _newCategory(context),
          ),
          IconButton(
            tooltip: 'Sign out',
            icon: const Icon(Icons.logout),
            onPressed: () => ref.read(authStateProvider.notifier).logout(),
          ),
        ],
      ),
      // The Paycheck button is hidden: paychecks arrive from the bank
      // and get picked up by sync, so entering one by hand was only
      // ever for testing. PaycheckScreen still exists and the route
      // still works — flip this to true to get the button back.
      floatingActionButton: _showManualPaycheck
          ? FloatingActionButton.extended(
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute<bool>(builder: (_) => const PaycheckScreen()),
              ),
              icon: const Icon(Icons.add_card),
              label: const Text('Paycheck'),
            )
          : null,
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
              _PaycheckPrompt(),
              _TotalCard(summary: s),
              if (s.ledgerDrift.isNotEmpty) ...[
                const SizedBox(height: 12),
                _DriftWarning(count: s.ledgerDrift.length),
              ],
              if (s.cashAccounts.isNotEmpty) ...[
                const SizedBox(height: 20),
                _BalancesSection(
                  title: 'In the bank',
                  icon: Icons.savings_outlined,
                  totalCents: s.cashCents,
                  rows: [
                    for (final a in s.cashAccounts) (a.label, a.balanceCents),
                  ],
                ),
              ],
              if (s.cards.any((c) => c.owes)) ...[
                const SizedBox(height: 12),
                _CardsSection(cards: s.cards, totalCents: s.cardDebtCents),
              ],
              const SizedBox(height: 20),
              if (s.categories.isEmpty)
                const _EmptyState()
              else
                _CategoryList(categories: s.categories),
            ],
          ),
        ),
      ),
    );
  }
}

/// The category list, with a drag-to-reorder mode.
///
/// Reordering is behind an explicit toggle rather than always-on. Drag
/// handles that are always visible invite accidental drags on a list the
/// user mostly wants to read, and a long-press to drag would fight the
/// tap that opens a category.
class _CategoryList extends ConsumerStatefulWidget {
  final List<Category> categories;
  const _CategoryList({required this.categories});

  @override
  ConsumerState<_CategoryList> createState() => _CategoryListState();
}

class _CategoryListState extends ConsumerState<_CategoryList> {
  bool _reordering = false;
  List<Category>? _draft;
  bool _saving = false;

  List<Category> get _shown => _draft ?? widget.categories;

  @override
  void didUpdateWidget(covariant _CategoryList old) {
    super.didUpdateWidget(old);
    // A refresh that lands mid-drag must not silently discard the order
    // the user is arranging.
    if (!_reordering) _draft = null;
  }

  Future<void> _save() async {
    final order = _shown;
    setState(() => _saving = true);
    try {
      await ref.read(apiProvider).reorderCategories([for (final c in order) c.id]);
      if (!mounted) return;
      setState(() {
        _reordering = false;
        _draft = null;
        _saving = false;
      });
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
    } catch (e) {
      if (!mounted) return;
      setState(() => _saving = false);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text("Could not save the order: $e")),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Text('Categories',
                style: theme.textTheme.titleSmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            const Spacer(),
            if (!_reordering)
              IconButton(
                tooltip: 'Archived',
                icon: const Icon(Icons.archive_outlined, size: 20),
                onPressed: () => _showArchived(context, ref),
              ),
            if (_reordering) ...[
              TextButton(
                onPressed: _saving
                    ? null
                    : () => setState(() {
                          _reordering = false;
                          _draft = null;
                        }),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: _saving ? null : _save,
                child: _saving
                    ? const SizedBox(
                        width: 16, height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Done'),
              ),
            ] else
              TextButton.icon(
                onPressed: widget.categories.length < 2
                    ? null
                    : () => setState(() => _reordering = true),
                icon: const Icon(Icons.swap_vert, size: 18),
                label: const Text('Reorder'),
              ),
          ],
        ),
        if (_reordering) ...[
          const SizedBox(height: 4),
          _ReorderNotice(),
          const SizedBox(height: 8),
        ] else
          const SizedBox(height: 4),
        Card(
          child: _reordering
              ? ReorderableListView.builder(
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  buildDefaultDragHandles: false,
                  itemCount: _shown.length,
                  onReorder: (from, to) => setState(() {
                    final next = [..._shown];
                    if (to > from) to -= 1;
                    next.insert(to, next.removeAt(from));
                    _draft = next;
                  }),
                  itemBuilder: (context, i) {
                    final c = _shown[i];
                    return ListTile(
                      key: ValueKey(c.id),
                      dense: true,
                      leading: ReorderableDragStartListener(
                        index: i,
                        child: const Icon(Icons.drag_handle),
                      ),
                      title: Text(c.name),
                      trailing: Text(Money.format(c.balanceCents),
                          style: theme.textTheme.bodyMedium),
                    );
                  },
                )
              : Column(
                  children: [
                    for (var i = 0; i < widget.categories.length; i++) ...[
                      if (i > 0)
                        const Divider(height: 1, indent: 16, endIndent: 16),
                      CategoryTile(
                        category: widget.categories[i],
                        onTap: () => Navigator.of(context).push(
                          MaterialPageRoute<bool>(
                            builder: (_) => CategoryEditorScreen(
                                existing: widget.categories[i]),
                          ),
                        ),
                      ),
                    ],
                  ],
                ),
        ),
      ],
    );
  }
}

/// Archived categories, with a way back.
///
/// Archiving is reversible by design (categories are never deleted, so
/// transaction history stays explainable), but that is worth nothing if
/// there is no screen that lists them.
Future<void> _showArchived(BuildContext context, WidgetRef ref) async {
  final api = ref.read(apiProvider);
  late List<Category> archived;
  try {
    archived = (await api.categories(includeArchived: true))
        .where((c) => c.archived)
        .toList();
  } catch (e) {
    if (!context.mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text('$e')));
    return;
  }
  if (!context.mounted) return;
  await showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    builder: (ctx) => SafeArea(
      child: archived.isEmpty
          ? const Padding(
              padding: EdgeInsets.fromLTRB(16, 0, 16, 32),
              child: Text('Nothing archived.', textAlign: TextAlign.center),
            )
          : Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                for (final c in archived)
                  ListTile(
                    title: Text(c.name),
                    subtitle: Text(c.balanceCents == 0
                        ? c.kind
                        : 'still holds ${Money.format(c.balanceCents)}'),
                    trailing: TextButton(
                      onPressed: () async {
                        try {
                          await api.restoreCategory(c.id);
                          ref.invalidate(summaryProvider);
                          ref.invalidate(categoriesProvider);
                          if (ctx.mounted) Navigator.pop(ctx);
                        } catch (e) {
                          if (ctx.mounted) {
                            ScaffoldMessenger.of(ctx).showSnackBar(
                                SnackBar(content: Text('$e')));
                          }
                        }
                      },
                      child: const Text('Restore'),
                    ),
                  ),
              ],
            ),
    ),
  );
}

/// Says plainly what reordering does and does not do.
///
/// Users reasonably assume dragging a category up means it gets funded
/// first. It does not -- that is the split plan's job -- and finding out
/// after a paycheck lands is the wrong time to learn it.
class _ReorderNotice extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.info_outline,
              size: 18, color: theme.colorScheme.onSurfaceVariant),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              'This changes the order you see them in only. '
              'To change which category gets funded first from a paycheck, '
              'edit the funding order in your split plan.',
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
          ),
        ],
      ),
    );
  }
}

/// "You got paid — review it."
///
/// An unrecorded deposit that looks like income. Shown rather than
/// acted on: splitting a tax refund as though it were a paycheck would
/// be worse than making the user tap once.
class _PaycheckPrompt extends ConsumerWidget {
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    final pending = ref.watch(unreviewedPaychecksProvider);

    return pending.maybeWhen(
      data: (list) {
        if (list.isEmpty) return const SizedBox.shrink();
        final first = list.first;
        final cents = first['inflow_cents'] as int? ?? 0;
        final label = (first['merchant_name'] as String?) ??
            (first['name'] as String? ?? 'Deposit');
        return Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: Card(
            color: theme.colorScheme.primaryContainer,
            child: InkWell(
              borderRadius: BorderRadius.circular(16),
              onTap: () async {
                await Navigator.of(context).push(
                  MaterialPageRoute<bool>(
                    builder: (_) => PaycheckScreen(
                      initialCents: cents,
                      sourceLabel: '$label · ${first['date']}',
                      transactionId: first['transaction_id'] as String?,
                    ),
                  ),
                );
                ref.invalidate(unreviewedPaychecksProvider);
                ref.invalidate(summaryProvider);
              },
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Row(children: [
                  Icon(Icons.payments_outlined,
                      color: theme.colorScheme.onPrimaryContainer),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          list.length == 1
                              ? 'Review your paycheck'
                              : '${list.length} paychecks to review',
                          style: theme.textTheme.titleMedium?.copyWith(
                            fontWeight: FontWeight.w700,
                            color: theme.colorScheme.onPrimaryContainer,
                          ),
                        ),
                        const SizedBox(height: 2),
                        Text(
                          '${Money.format(cents)} from $label — see how it '
                          'splits before anything moves',
                          style: theme.textTheme.bodySmall?.copyWith(
                              color: theme.colorScheme.onPrimaryContainer),
                        ),
                      ],
                    ),
                  ),
                  Icon(Icons.chevron_right,
                      color: theme.colorScheme.onPrimaryContainer),
                ]),
              ),
            ),
          ),
        );
      },
      orElse: () => const SizedBox.shrink(),
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
          const SizedBox(height: 20),
          // An empty state that only explains the problem leaves the user
          // hunting the app bar for the fix.
          FilledButton.icon(
            onPressed: () => _newCategory(context),
            icon: const Icon(Icons.add),
            label: const Text('Add a category'),
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


/// A labelled group of account balances with a total.
class _BalancesSection extends StatelessWidget {
  final String title;
  final IconData icon;
  final int totalCents;
  final List<(String, int)> rows;

  const _BalancesSection({
    required this.title,
    required this.icon,
    required this.totalCents,
    required this.rows,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, size: 18, color: theme.colorScheme.onSurfaceVariant),
                const SizedBox(width: 8),
                Text(title,
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
            for (final (label, cents) in rows)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 3),
                child: Row(
                  children: [
                    Expanded(
                      child: Text(label,
                          style: theme.textTheme.bodyMedium,
                          overflow: TextOverflow.ellipsis),
                    ),
                    Text(
                      Money.format(cents),
                      style: theme.textTheme.bodyMedium?.copyWith(
                        fontFeatures: const [FontFeature.tabularFigures()],
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}

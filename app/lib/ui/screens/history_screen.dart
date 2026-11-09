
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../core/theme.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';
import '../widgets/transaction_detail_sheet.dart';
import 'scan_receipt_screen.dart';

/// Every transaction and where its money went.
///
/// The review queue only shows what still needs a decision. This is the
/// record: what came in, what went out, and which envelope each one hit.
class HistoryScreen extends ConsumerStatefulWidget {
  /// When set, only transactions touching this category.
  final String? categoryId;
  final String? categoryName;

  const HistoryScreen({super.key, this.categoryId, this.categoryName});

  @override
  ConsumerState<HistoryScreen> createState() => _HistoryScreenState();
}

class _HistoryScreenState extends ConsumerState<HistoryScreen> {
  final _scroll = ScrollController();
  final _search = TextEditingController();

  final List<HistoryItem> _items = [];
  int _total = 0;
  bool _hasMore = true;
  bool _loading = false;
  String? _error;
  String? _treatment;

  static const _pageSize = 50;

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.position.pixels >
              _scroll.position.maxScrollExtent - 400 &&
          !_loading &&
          _hasMore) {
        _load();
      }
    });
    _load();
  }

  @override
  void dispose() {
    _scroll.dispose();
    _search.dispose();
    super.dispose();
  }

  Future<void> _reset() async {
    setState(() {
      _items.clear();
      _hasMore = true;
      _total = 0;
    });
    await _load();
  }

  Future<void> _load() async {
    if (_loading) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final page = await ref.read(apiProvider).history(
            limit: _pageSize,
            offset: _items.length,
            categoryId: widget.categoryId,
            treatment: _treatment,
            search: _search.text.trim(),
          );
      if (!mounted) return;
      setState(() {
        _items.addAll(page.items);
        _total = page.total;
        _hasMore = page.hasMore;
      });
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.categoryName == null
            ? 'History'
            : '${widget.categoryName} history'),
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
            child: TextField(
              controller: _search,
              textInputAction: TextInputAction.search,
              onSubmitted: (_) => _reset(),
              decoration: InputDecoration(
                hintText: 'Search merchant or description',
                prefixIcon: const Icon(Icons.search),
                suffixIcon: _search.text.isEmpty
                    ? null
                    : IconButton(
                        icon: const Icon(Icons.close),
                        onPressed: () {
                          _search.clear();
                          _reset();
                        },
                      ),
                isDense: true,
              ),
            ),
          ),
          SizedBox(
            height: 48,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              children: [
                for (final (value, label) in const [
                  (null, 'Everything'),
                  ('spend', 'Spending'),
                  ('card_payment', 'Card payments'),
                  ('transfer', 'Transfers'),
                  ('ignored', 'Ignored'),
                ])
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ChoiceChip(
                      label: Text(label),
                      selected: _treatment == value,
                      onSelected: (_) {
                        setState(() => _treatment = value);
                        _reset();
                      },
                    ),
                  ),
              ],
            ),
          ),
          if (_total > 0)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: Align(
                alignment: Alignment.centerLeft,
                child: Text('$_total transaction${_total == 1 ? '' : 's'}',
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
              ),
            ),
          Expanded(
            child: _error != null
                ? Center(
                    child: Padding(
                      padding: const EdgeInsets.all(24),
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Text(_error!, textAlign: TextAlign.center),
                          const SizedBox(height: 12),
                          FilledButton(
                              onPressed: _reset, child: const Text('Retry')),
                        ],
                      ),
                    ),
                  )
                : _items.isEmpty && !_loading
                    ? Center(
                        child: Text('Nothing here.',
                            style: theme.textTheme.bodyMedium?.copyWith(
                                color: theme.colorScheme.onSurfaceVariant)),
                      )
                    : RefreshIndicator(
                        onRefresh: _reset,
                        child: ListView.separated(
                          controller: _scroll,
                          padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
                          itemCount: _items.length + (_hasMore ? 1 : 0),
                          separatorBuilder: (_, __) => const SizedBox(height: 8),
                          itemBuilder: (context, i) {
                            if (i >= _items.length) {
                              return const Padding(
                                padding: EdgeInsets.all(16),
                                child: Center(
                                    child: CircularProgressIndicator()),
                              );
                            }
                            return _HistoryCard(
                              item: _items[i],
                              onChanged: _reset,
                            );
                          },
                        ),
                      ),
          ),
        ],
      ),
    );
  }
}

class _HistoryCard extends ConsumerWidget {
  final HistoryItem item;
  final Future<void> Function() onChanged;
  const _HistoryCard({required this.item, required this.onChanged});

  /// Undoing an old paycheck cannot be casual: the money may already
  /// have been spent, and reversing does not bring it back. Show what
  /// it would do, then let the user decide.
  /// Move a filed charge wholesale into a different envelope.
  ///
  /// One split covering the whole amount, which the refile endpoint
  /// handles by REVERSING the old allocations and posting the new
  /// ones. Nothing is edited: history keeps showing where it went
  /// first, that it was undone, and where it went instead.
  Future<void> _changeCategory(BuildContext context, WidgetRef ref) async {
    final cats = await ref.read(categoriesProvider.future);
    final choices = cats
        .where((c) => !c.archived && !c.isUnallocated)
        .toList()
      ..sort((a, b) => a.name.toLowerCase().compareTo(b.name.toLowerCase()));
    if (!context.mounted || choices.isEmpty) return;

    final picked = await showModalBottomSheet<Category>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (ctx) {
        final theme = Theme.of(ctx);
        return SafeArea(
          child: ListView(
            shrinkWrap: true,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 4, 20, 8),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Move ${item.title}',
                        style: theme.textTheme.titleMedium),
                    const SizedBox(height: 2),
                    Text(
                      MoneyFlow.ofSigned(item.amountCents).isOut
                          ? 'Takes it out of where it is now and charges it '
                              'to the envelope you pick.'
                          : 'This is money coming back. It will be taken off '
                              'where it was credited and added to the '
                              'envelope you pick instead.',
                      style: theme.textTheme.bodySmall?.copyWith(
                          color: theme.colorScheme.onSurfaceVariant),
                    ),
                  ],
                ),
              ),
              for (final c in choices)
                ListTile(
                  title: Text(c.name),
                  subtitle: Text(Money.format(c.balanceCents),
                      style: theme.textTheme.bodySmall),
                  onTap: () => Navigator.of(ctx).pop(c),
                ),
            ],
          ),
        );
      },
    );
    if (picked == null || !context.mounted) return;

    try {
      await ref.read(apiProvider).refileTransaction(item.id, [
        (categoryId: picked.id, amountCents: item.amountCents,
         description: null),
      ]);
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      await onChanged();
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text('Moved to ${picked.name}'),
          behavior: SnackBarBehavior.floating,
        ));
      }
    } on ApiException catch (e) {
      if (context.mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(e.message)));
      }
    }
  }

  Future<void> _undo(BuildContext context, WidgetRef ref) async {
    final api = ref.read(apiProvider);
    UndoPreview preview;
    try {
      preview = await api.undoPreview(item.paycheckId!);
    } catch (e) {
      if (context.mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$e')));
      }
      return;
    }
    if (!context.mounted) return;

    final ok = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (ctx) => _UndoSheet(preview: preview),
    );
    if (ok != true) return;

    try {
      await api.unapplyPaycheck(item.paycheckId!, force: !preview.safe);
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      ref.invalidate(unreviewedPaychecksProvider);
      await onChanged();
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('Paycheck undone'),
          behavior: SnackBarBehavior.floating,
        ));
      }
    } on ApiException catch (e) {
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(e.message),
          backgroundColor: Theme.of(context).colorScheme.error,
        ));
      }
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    final flow = MoneyFlow.ofSigned(item.amountCents);
    final colour = switch (flow) {
      MoneyFlow.out => theme.colorScheme.onSurface,
      MoneyFlow.into => AppTheme.positive(context),
      MoneyFlow.none => theme.colorScheme.onSurfaceVariant,
    };

    return Card(
      margin: EdgeInsets.zero,
      // The card itself opens the details. A "Details" button alongside
      // Itemise and Change category made three buttons competing on one
      // row, when the obvious gesture -- tap the thing -- was unused.
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: () => showTransactionDetail(context, ref, item.id),
        child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(flow.isInto ? Icons.south_west : Icons.arrow_outward,
                    size: 18, color: colour),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(item.title,
                          style: theme.textTheme.bodyLarge
                              ?.copyWith(fontWeight: FontWeight.w600),
                          overflow: TextOverflow.ellipsis),
                      const SizedBox(height: 2),
                      Text(
                        [
                          item.date,
                          if (item.accountName != null) item.accountName!,
                          if (item.pending) 'pending',
                        ].join(' · '),
                        style: theme.textTheme.bodySmall?.copyWith(
                            color: theme.colorScheme.onSurfaceVariant),
                        overflow: TextOverflow.ellipsis,
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: 8),
                Text(
                  MoneyFlowFormat.flow(item.amountCents),
                  style: theme.textTheme.titleMedium?.copyWith(
                    fontWeight: FontWeight.w700,
                    color: colour,
                    fontFeatures: const [FontFeature.tabularFigures()],
                  ),
                ),
              ],
            ),
            const SizedBox(height: 10),
            _Destination(item: item),
            // Splitting a charge that already filed. Only offered for
            // spending that is actually in the budget: a transfer moves
            // money already accounted for, and a deposit is not a
            // receipt.
            //
            // One row, both actions. Itemise is offered only for
            // spending that is actually in the budget -- a transfer
            // moves money already accounted for, and a deposit is not
            // a receipt -- so Change category often stands alone, and
            // sits first in that case rather than leaving a gap.
            if (!item.excluded &&
                !item.awaitingReview &&
                item.paycheckId == null) ...[
              const SizedBox(height: 4),
              Row(
                children: [
                  if (MoneyFlow.ofSigned(item.amountCents).isOut) ...[
                    TextButton.icon(
                      onPressed: () async {
                        final changed = await Navigator.of(context).push<bool>(
                          MaterialPageRoute(
                            builder: (_) => ScanReceiptScreen(
                              transactionId: item.id,
                              amountCents: item.amountCents,
                              title: item.title,
                              alreadyFiled: true,
                            ),
                          ),
                        );
                        if (changed == true) await onChanged();
                      },
                      icon: const Icon(Icons.receipt_long_outlined, size: 16),
                      label: const Text('Itemise'),
                      style: TextButton.styleFrom(
                          padding: const EdgeInsets.symmetric(horizontal: 8),
                          visualDensity: VisualDensity.compact),
                    ),
                    const SizedBox(width: 4),
                  ],
                  TextButton.icon(
                    onPressed: () => _changeCategory(context, ref),
                    icon: const Icon(Icons.swap_horiz, size: 16),
                    label: const Text('Change category'),
                    style: TextButton.styleFrom(
                        padding: const EdgeInsets.symmetric(horizontal: 8),
                        visualDensity: VisualDensity.compact),
                  ),
                ],
              ),
            ],
            if (item.paycheckApplied) ...[
              const SizedBox(height: 4),
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton.icon(
                  onPressed: () => _undo(context, ref),
                  icon: const Icon(Icons.undo, size: 16),
                  label: const Text('Undo this split'),
                  style: TextButton.styleFrom(
                      padding: const EdgeInsets.symmetric(horizontal: 8),
                      visualDensity: VisualDensity.compact),
                ),
              ),
            ],
          ],
        ),
      ),
    ));
  }
}

/// What undoing a paycheck would do, before it is done.
class _UndoSheet extends StatelessWidget {
  final UndoPreview preview;
  const _UndoSheet({required this.preview});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Undo this paycheck?',
                style: theme.textTheme.titleLarge
                    ?.copyWith(fontWeight: FontWeight.w700)),
            const SizedBox(height: 4),
            Text(
              '${Money.format(preview.grossCents)} received '
              '${preview.receivedOn} — '
              '${Money.format(preview.totalReversedCents)} would come back '
              'out of these categories.',
              style: theme.textTheme.bodyMedium
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
            if (!preview.safe) ...[
              const SizedBox(height: 12),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: theme.colorScheme.errorContainer,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Icon(Icons.warning_amber_rounded,
                          size: 18,
                          color: theme.colorScheme.onErrorContainer),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          'Some of this has already been spent. Undoing '
                          'does not bring it back — it leaves '
                          '${preview.wouldGoNegative.join(", ")} overspent.',
                          style: TextStyle(
                              color: theme.colorScheme.onErrorContainer),
                        ),
                      ),
                    ]),
              ),
            ],
            const SizedBox(height: 16),
            for (final i in preview.impacts)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Row(children: [
                  Expanded(
                    child: Text(i.name,
                        style: theme.textTheme.bodyMedium,
                        overflow: TextOverflow.ellipsis),
                  ),
                  Text(
                    '${Money.format(i.balanceCents)} → '
                    '${Money.format(i.afterCents)}',
                    style: theme.textTheme.bodyMedium?.copyWith(
                      fontWeight: i.goesNegative ? FontWeight.w700 : null,
                      color: i.goesNegative ? theme.colorScheme.error : null,
                    ),
                  ),
                ]),
              ),
            const SizedBox(height: 16),
            Row(children: [
              Expanded(
                child: OutlinedButton(
                  onPressed: () => Navigator.pop(context, false),
                  child: const Text('Keep it'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: FilledButton(
                  style: preview.safe
                      ? null
                      : FilledButton.styleFrom(
                          backgroundColor: theme.colorScheme.error),
                  onPressed: () => Navigator.pop(context, true),
                  child: Text(preview.safe ? 'Undo' : 'Undo anyway'),
                ),
              ),
            ]),
          ],
        ),
      ),
    );
  }
}

/// Where the money actually landed.
///
/// Three outcomes that must not look alike: filed against categories,
/// deliberately excluded (a card payment is not spending — counting it
/// would double-count the purchases it settles), and simply not reviewed
/// yet.
class _Destination extends StatelessWidget {
  final HistoryItem item;
  const _Destination({required this.item});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    if (item.excluded) {
      return _chip(
        context,
        icon: Icons.block,
        label: item.treatmentLabel,
        note: 'left out of the budget on purpose',
        colour: theme.colorScheme.onSurfaceVariant,
      );
    }
    if (item.awaitingReview) {
      return _chip(
        context,
        icon: Icons.schedule,
        label: 'Not filed yet',
        note: 'waiting for you in Review',
        colour: AppTheme.warning,
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (final s in item.splits)
          Padding(
            padding: const EdgeInsets.only(top: 2),
            child: Row(children: [
              Icon(Icons.subdirectory_arrow_right,
                  size: 14, color: theme.colorScheme.onSurfaceVariant),
              const SizedBox(width: 6),
              Expanded(
                child: Text(
                  MoneyFlow.ofSigned(s.amountCents).isInto
                      ? 'added to ${s.categoryName}'
                      : 'out of ${s.categoryName}',
                  style: theme.textTheme.bodySmall,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              if (item.splits.length > 1)
                Text(MoneyFlowFormat.flow(s.amountCents),
                    style: theme.textTheme.bodySmall?.copyWith(
                        color: theme.colorScheme.onSurfaceVariant)),
            ]),
          ),
      ],
    );
  }

  Widget _chip(BuildContext context,
      {required IconData icon,
      required String label,
      required String note,
      required Color colour}) {
    final theme = Theme.of(context);
    return Row(children: [
      Icon(icon, size: 14, color: colour),
      const SizedBox(width: 6),
      Text(label, style: theme.textTheme.bodySmall?.copyWith(color: colour)),
      const SizedBox(width: 6),
      Expanded(
        child: Text('— $note',
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            overflow: TextOverflow.ellipsis),
      ),
    ]);
  }
}

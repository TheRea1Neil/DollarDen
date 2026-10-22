
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../core/theme.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';

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
                            return _HistoryCard(item: _items[i]);
                          },
                        ),
                      ),
          ),
        ],
      ),
    );
  }
}

class _HistoryCard extends StatelessWidget {
  final HistoryItem item;
  const _HistoryCard({required this.item});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final flow = MoneyFlow.ofSigned(item.amountCents);
    final colour = switch (flow) {
      MoneyFlow.out => theme.colorScheme.onSurface,
      MoneyFlow.into => AppTheme.positive(context),
      MoneyFlow.none => theme.colorScheme.onSurfaceVariant,
    };

    return Card(
      margin: EdgeInsets.zero,
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

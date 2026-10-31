import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';

/// Moving money that is already yours.
///
/// Two jobs that look similar and are not:
///
///   MOVE — take some out of one envelope and put it in another.
///     The everyday correction: groceries ran over, take it from
///     discretionary. Nothing else changes.
///
///   REBALANCE — say what every envelope should hold, in one pass.
///     For starting from a real bank balance, or when the first guess
///     at the split turns out wrong.
///
/// Neither touches spending history. They move allocations, so the
/// ledger can still say where the money went — a balance written
/// directly would leave the other envelope silently wrong.
class MoveMoneyScreen extends ConsumerStatefulWidget {
  const MoveMoneyScreen({super.key});

  @override
  ConsumerState<MoveMoneyScreen> createState() => _MoveMoneyScreenState();
}

class _MoveMoneyScreenState extends ConsumerState<MoveMoneyScreen>
    with SingleTickerProviderStateMixin {
  late final TabController _tabs = TabController(length: 2, vsync: this);

  @override
  void dispose() {
    _tabs.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          title: const Text('Move money'),
          bottom: TabBar(controller: _tabs, tabs: const [
            Tab(text: 'Move'),
            Tab(text: 'Rebalance'),
          ]),
        ),
        body: TabBarView(
          controller: _tabs,
          children: const [_MoveTab(), _RebalanceTab()],
        ),
      );
}

// --------------------------------------------------------------------- //
class _MoveTab extends ConsumerStatefulWidget {
  const _MoveTab();

  @override
  ConsumerState<_MoveTab> createState() => _MoveTabState();
}

/// Two dropdowns and a field, all on one screen.
///
/// The first version listed every envelope twice as chips — fourteen
/// of them on a seven-envelope budget — which pushed the amount and
/// the button below the fold. You could not see what you had picked
/// and what you were about to do at the same time, which is exactly
/// what you need to see before moving money.
class _MoveTabState extends ConsumerState<_MoveTab> {
  String? _from;
  String? _to;
  final _amount = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _amount.dispose();
    super.dispose();
  }

  Future<void> _go() async {
    final cents = Money.parse(_amount.text);
    if (_from == null || _to == null || cents == null || cents <= 0) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(apiProvider).transferBetweenCategories(
            fromCategoryId: _from!, toCategoryId: _to!, amountCents: cents);
      ref.invalidate(categoriesProvider);
      ref.invalidate(summaryProvider);
      if (!mounted) return;
      _amount.clear();
      setState(() {});
      ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Moved ${Money.format(cents)}')));
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ref.watch(categoriesProvider).when(
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => Center(child: Text('$e')),
          data: (all) {
            final cats = all.where((c) => !c.archived).toList();
            final from = cats.where((c) => c.id == _from).firstOrNull;
            final to = cats.where((c) => c.id == _to).firstOrNull;
            final cents = Money.parse(_amount.text) ?? 0;
            final ready = from != null && to != null && cents > 0;
            final short = from != null && cents > from.balanceCents;

            return ListView(
              padding: const EdgeInsets.all(16),
              children: [
                _Dropdown(
                  label: 'Take it out of',
                  cats: cats,
                  value: _from,
                  exclude: _to,
                  onChanged: (v) => setState(() => _from = v),
                ),
                const SizedBox(height: 14),
                _Dropdown(
                  label: 'And put it in',
                  cats: cats,
                  value: _to,
                  exclude: _from,
                  onChanged: (v) => setState(() => _to = v),
                ),
                const SizedBox(height: 14),
                TextField(
                  controller: _amount,
                  keyboardType:
                      const TextInputType.numberWithOptions(decimal: true),
                  onChanged: (_) => setState(() {}),
                  decoration: const InputDecoration(
                    labelText: 'Amount',
                    prefixText: r'$ ',
                    border: OutlineInputBorder(),
                  ),
                ),

                // What it will do, before it does it.
                if (ready) ...[
                  const SizedBox(height: 16),
                  Card(
                    margin: EdgeInsets.zero,
                    child: Padding(
                      padding: const EdgeInsets.all(14),
                      child: Column(
                        children: [
                          _AfterRow(name: from.name,
                              before: from.balanceCents,
                              after: from.balanceCents - cents),
                          const SizedBox(height: 6),
                          _AfterRow(name: to.name,
                              before: to.balanceCents,
                              after: to.balanceCents + cents),
                        ],
                      ),
                    ),
                  ),
                ],
                if (short) ...[
                  const SizedBox(height: 10),
                  Text(
                    // Allowed, not blocked: an envelope CAN go negative,
                    // and hiding that would be worse than showing it.
                    '${from.name} only holds ${Money.format(from.balanceCents)}. '
                    'Moving more will leave it overdrawn.',
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.error),
                  ),
                ],
                if (_error != null) ...[
                  const SizedBox(height: 12),
                  Text(_error!,
                      style: TextStyle(color: theme.colorScheme.error)),
                ],
                const SizedBox(height: 20),
                FilledButton(
                  onPressed: (_busy || !ready) ? null : _go,
                  child: const Text('Move it'),
                ),
              ],
            );
          },
        );
  }
}

class _Dropdown extends StatelessWidget {
  final String label;
  final List<Category> cats;
  final String? value;
  final String? exclude;
  final ValueChanged<String?> onChanged;

  const _Dropdown({
    required this.label,
    required this.cats,
    required this.value,
    required this.exclude,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) => DropdownButtonFormField<String>(
        initialValue: value,
        isExpanded: true,
        decoration: InputDecoration(
          labelText: label,
          border: const OutlineInputBorder(),
        ),
        items: [
          for (final c in cats.where((c) => c.id != exclude))
            DropdownMenuItem(
              value: c.id,
              child: Text('${c.name}  ·  ${Money.format(c.balanceCents)}',
                  overflow: TextOverflow.ellipsis),
            ),
        ],
        onChanged: onChanged,
      );
}

/// "Gas   $49.62 -> $39.62"
class _AfterRow extends StatelessWidget {
  final String name;
  final int before;
  final int after;
  const _AfterRow({required this.name, required this.before, required this.after});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Row(
      children: [
        Expanded(child: Text(name, maxLines: 1, overflow: TextOverflow.ellipsis)),
        Text(Money.format(before), style: theme.textTheme.bodySmall),
        const Padding(padding: EdgeInsets.symmetric(horizontal: 6),
            child: Icon(Icons.arrow_forward, size: 13)),
        Text(
          Money.format(after),
          style: theme.textTheme.bodyMedium?.copyWith(
            fontWeight: FontWeight.w600,
            color: after < 0 ? theme.colorScheme.error : null,
          ),
        ),
      ],
    );
  }
}

// --------------------------------------------------------------------- //
class _RebalanceTab extends ConsumerStatefulWidget {
  const _RebalanceTab();

  @override
  ConsumerState<_RebalanceTab> createState() => _RebalanceTabState();
}

class _RebalanceTabState extends ConsumerState<_RebalanceTab> {
  final Map<String, TextEditingController> _fields = {};
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    for (final c in _fields.values) {
      c.dispose();
    }
    super.dispose();
  }

  TextEditingController _field(Category c) => _fields.putIfAbsent(
      c.id,
      () => TextEditingController(
          text: (c.balanceCents / 100).toStringAsFixed(2)));

  Future<void> _go(List<Category> cats) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    final targets = <({String categoryId, int amountCents})>[];
    for (final c in cats) {
      final v = Money.parse(_field(c).text);
      if (v == null || v < 0) continue;
      targets.add((categoryId: c.id, amountCents: v));
    }
    try {
      await ref.read(apiProvider).reallocate(targets);
      ref.invalidate(categoriesProvider);
      ref.invalidate(summaryProvider);
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Rebalanced')));
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ref.watch(categoriesProvider).when(
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => Center(child: Text('$e')),
          data: (all) {
            final cats = all.where((c) => !c.archived).toList();
            var planned = 0;
            for (final c in cats) {
              planned += Money.parse(_field(c).text) ?? 0;
            }
            return ListView(
              padding: const EdgeInsets.all(16),
              children: [
                Text(
                  'Say what each envelope should hold. Anything you do not '
                  'change stays as it is.',
                  style: theme.textTheme.bodySmall,
                ),
                const SizedBox(height: 16),
                for (final c in cats)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 12),
                    child: Row(
                      children: [
                        Expanded(
                          child: Text(c.name,
                              maxLines: 1, overflow: TextOverflow.ellipsis),
                        ),
                        SizedBox(
                          width: 130,
                          child: TextField(
                            controller: _field(c),
                            textAlign: TextAlign.right,
                            keyboardType: const TextInputType.numberWithOptions(
                                decimal: true),
                            onChanged: (_) => setState(() {}),
                            decoration: const InputDecoration(
                              prefixText: r'$ ',
                              isDense: true,
                              border: OutlineInputBorder(),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                const Divider(height: 24),
                Row(
                  children: [
                    Text('Allocated in total', style: theme.textTheme.bodyMedium),
                    const Spacer(),
                    Text(Money.format(planned),
                        style: theme.textTheme.titleMedium),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  'The server refuses a total above what is actually in the '
                  'bank, minus anything owed on cards.',
                  style: theme.textTheme.bodySmall,
                ),
                if (_error != null) ...[
                  const SizedBox(height: 12),
                  Text(_error!,
                      style: TextStyle(color: theme.colorScheme.error)),
                ],
                const SizedBox(height: 20),
                FilledButton(
                  onPressed: _busy ? null : () => _go(cats),
                  child: const Text('Apply'),
                ),
              ],
            );
          },
        );
  }
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}

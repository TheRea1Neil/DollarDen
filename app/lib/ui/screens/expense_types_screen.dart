import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/insights.dart';

/// Kinds of spending, and the queue of charges that still need one.
///
/// Two tabs because they are two jobs: maintaining the vocabulary, and
/// applying it. The second is where the time goes, so it is reachable
/// directly from the "not yet labelled" card on the insights screen.
class ExpenseTypesScreen extends ConsumerStatefulWidget {
  final bool startOnUntyped;
  const ExpenseTypesScreen({super.key, this.startOnUntyped = false});

  @override
  ConsumerState<ExpenseTypesScreen> createState() => _ExpenseTypesScreenState();
}

class _ExpenseTypesScreenState extends ConsumerState<ExpenseTypesScreen>
    with SingleTickerProviderStateMixin {
  late final TabController _tabs = TabController(
    length: 2,
    vsync: this,
    initialIndex: widget.startOnUntyped ? 1 : 0,
  );

  @override
  void dispose() {
    _tabs.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          title: const Text('Kinds of spending'),
          bottom: TabBar(
            controller: _tabs,
            tabs: const [
              Tab(text: 'Kinds'),
              Tab(text: 'To label'),
            ],
          ),
        ),
        floatingActionButton: _tabs.index == 0
            ? FloatingActionButton(
                onPressed: () => _addType(context, ref),
                child: const Icon(Icons.add),
              )
            : null,
        body: TabBarView(
          controller: _tabs,
          children: const [_TypeList(), _UntypedList()],
        ),
      );
}

// --------------------------------------------------------------------- //
// The vocabulary
// --------------------------------------------------------------------- //
class _TypeList extends ConsumerWidget {
  const _TypeList();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final types = ref.watch(expenseTypesProvider);
    final theme = Theme.of(context);

    return types.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(child: Text('$e')),
      data: (list) {
        if (list.isEmpty) {
          return Center(
            child: Padding(
              padding: const EdgeInsets.all(28),
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Text('No kinds yet', style: theme.textTheme.titleMedium),
                  const SizedBox(height: 8),
                  Text(
                    'A kind is what a charge WAS — fast food, fuel, '
                    'subscriptions — as opposed to which envelope paid for it. '
                    'They appear as you label spending, or add one now.',
                    textAlign: TextAlign.center,
                    style: theme.textTheme.bodySmall,
                  ),
                ],
              ),
            ),
          );
        }

        final needReview = list.where((t) => t.needsReview).toList();
        return RefreshIndicator(
          onRefresh: () async => ref.invalidate(expenseTypesProvider),
          child: ListView(
            padding: const EdgeInsets.only(bottom: 88),
            children: [
              if (needReview.isNotEmpty)
                Card(
                  margin: const EdgeInsets.all(12),
                  child: Padding(
                    padding: const EdgeInsets.all(14),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('The assistant added these',
                            style: theme.textTheme.titleSmall),
                        const SizedBox(height: 4),
                        Text(
                          'Keep them, rename them, or merge them into something '
                          'you already use. Two names for the same thing split '
                          'every total between them.',
                          style: theme.textTheme.bodySmall,
                        ),
                        const SizedBox(height: 8),
                        Wrap(
                          spacing: 8,
                          children: [
                            for (final t in needReview)
                              InputChip(
                                label: Text(t.name),
                                onPressed: () => _editType(context, ref, t, list),
                              ),
                          ],
                        ),
                      ],
                    ),
                  ),
                ),
              for (final t in list)
                ListTile(
                  title: Row(
                    children: [
                      Flexible(child: Text(t.name)),
                      if (t.needsReview) ...[
                        const SizedBox(width: 6),
                        Icon(Icons.auto_awesome,
                            size: 13, color: theme.colorScheme.outline),
                      ],
                    ],
                  ),
                  subtitle: Text(
                    t.description?.isNotEmpty == true
                        ? t.description!
                        : 'No description',
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                  ),
                  trailing: Column(
                    mainAxisAlignment: MainAxisAlignment.center,
                    crossAxisAlignment: CrossAxisAlignment.end,
                    children: [
                      Text(Money.format(t.spendCents),
                          style: theme.textTheme.bodyMedium),
                      Text('${t.txnCount}×', style: theme.textTheme.bodySmall),
                    ],
                  ),
                  onTap: () => _editType(context, ref, t, list),
                ),
            ],
          ),
        );
      },
    );
  }
}

Future<void> _addType(BuildContext context, WidgetRef ref) async {
  final suggestions = await ref.read(apiProvider).expenseTypeSuggestions();
  if (!context.mounted) return;

  final picked = await showModalBottomSheet<String>(
    context: context,
    isScrollControlled: true,
    builder: (ctx) => _AddTypeSheet(suggestions: suggestions),
  );
  if (picked == null || !context.mounted) return;

  try {
    await ref.read(apiProvider).createExpenseType(
          picked,
          description: suggestions
              .where((s) => s.name == picked)
              .map((s) => s.description)
              .where((d) => d.isNotEmpty)
              .firstOrNull,
        );
    ref.invalidate(expenseTypesProvider);
  } on ApiException catch (e) {
    if (context.mounted) {
      // A 409 here is the duplicate guard, and its message names the
      // existing kind. Showing it verbatim is the useful thing.
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(e.message)));
    }
  }
}

extension _First<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}

/// Offers standard names alongside a free-text field.
///
/// The standards are not created until something is filed under them,
/// so this list is longer than the account's actual vocabulary.
class _AddTypeSheet extends StatefulWidget {
  final List<TypeSuggestion> suggestions;
  const _AddTypeSheet({required this.suggestions});

  @override
  State<_AddTypeSheet> createState() => _AddTypeSheetState();
}

class _AddTypeSheetState extends State<_AddTypeSheet> {
  final _controller = TextEditingController();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final available = widget.suggestions.where((s) => !s.inUse).toList();

    return Padding(
      padding: EdgeInsets.fromLTRB(
          16, 16, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Add a kind of spending', style: theme.textTheme.titleMedium),
          const SizedBox(height: 12),
          TextField(
            controller: _controller,
            autofocus: true,
            textCapitalization: TextCapitalization.sentences,
            decoration: const InputDecoration(
              labelText: 'Name',
              helperText: 'General enough that future charges fit it too',
              border: OutlineInputBorder(),
            ),
            onSubmitted: (v) =>
                Navigator.of(context).pop(v.trim().isEmpty ? null : v.trim()),
          ),
          if (available.isNotEmpty) ...[
            const SizedBox(height: 16),
            Text('Or pick a standard one', style: theme.textTheme.titleSmall),
            const SizedBox(height: 8),
            ConstrainedBox(
              constraints: const BoxConstraints(maxHeight: 220),
              child: SingleChildScrollView(
                child: Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    for (final s in available)
                      ActionChip(
                        label: Text(s.name),
                        onPressed: () => Navigator.of(context).pop(s.name),
                      ),
                  ],
                ),
              ),
            ),
          ],
          const SizedBox(height: 12),
          Align(
            alignment: Alignment.centerRight,
            child: FilledButton(
              onPressed: () {
                final v = _controller.text.trim();
                Navigator.of(context).pop(v.isEmpty ? null : v);
              },
              child: const Text('Add'),
            ),
          ),
        ],
      ),
    );
  }
}

Future<void> _editType(
  BuildContext context,
  WidgetRef ref,
  ExpenseType type,
  List<ExpenseType> all,
) async {
  final result = await showModalBottomSheet<_TypeEdit>(
    context: context,
    isScrollControlled: true,
    builder: (_) => _EditTypeSheet(type: type, others: all.where((t) => t.id != type.id).toList()),
  );
  if (result == null || !context.mounted) return;

  final api = ref.read(apiProvider);
  try {
    switch (result) {
      case _Save(:final name, :final description):
        await api.updateExpenseType(type.id, {
          'name': name,
          'description': description,
          // Opening and saving is the confirmation an AI-coined type
          // was waiting for.
          'reviewed': true,
        });
      case _Merge(:final intoId):
        await api.mergeExpenseType(type.id, intoId);
      case _Archive():
        await api.archiveExpenseType(type.id);
    }
    ref.invalidate(expenseTypesProvider);
    ref.invalidate(insightsProvider);
  } on ApiException catch (e) {
    if (context.mounted) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(e.message)));
    }
  }
}

sealed class _TypeEdit {
  const _TypeEdit();
}

class _Save extends _TypeEdit {
  final String name;
  final String description;
  const _Save(this.name, this.description);
}

class _Merge extends _TypeEdit {
  final String intoId;
  const _Merge(this.intoId);
}

class _Archive extends _TypeEdit {
  const _Archive();
}

class _EditTypeSheet extends StatefulWidget {
  final ExpenseType type;
  final List<ExpenseType> others;
  const _EditTypeSheet({required this.type, required this.others});

  @override
  State<_EditTypeSheet> createState() => _EditTypeSheetState();
}

class _EditTypeSheetState extends State<_EditTypeSheet> {
  late final _name = TextEditingController(text: widget.type.name);
  late final _desc = TextEditingController(text: widget.type.description ?? '');

  @override
  void dispose() {
    _name.dispose();
    _desc.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: EdgeInsets.fromLTRB(
          16, 16, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(widget.type.name, style: theme.textTheme.titleMedium),
          if (widget.type.spendCents > 0)
            Text('${Money.format(widget.type.spendCents)} in this period',
                style: theme.textTheme.bodySmall),
          const SizedBox(height: 14),
          TextField(
            controller: _name,
            decoration: const InputDecoration(
              labelText: 'Name',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _desc,
            maxLines: 3,
            decoration: const InputDecoration(
              labelText: 'What belongs in this kind',
              helperText: 'Read by the assistant when it files a charge. '
                  'Naming the shops you use is what makes it accurate.',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 16),
          Row(
            children: [
              FilledButton(
                onPressed: () => Navigator.of(context)
                    .pop(_Save(_name.text.trim(), _desc.text.trim())),
                child: const Text('Save'),
              ),
              const Spacer(),
              if (widget.others.isNotEmpty)
                TextButton.icon(
                  onPressed: () => _pickMerge(context),
                  icon: const Icon(Icons.merge, size: 18),
                  label: const Text('Merge into…'),
                ),
            ],
          ),
          const Divider(height: 24),
          // Archive keeps history where it is; merge moves it. Saying
          // which is which here, because the distinction only matters
          // at the moment of choosing.
          ListTile(
            dense: true,
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.archive_outlined),
            title: const Text('Stop using this kind'),
            subtitle: const Text(
                'Past spending keeps this label. Merge instead to move it.'),
            onTap: () => Navigator.of(context).pop(const _Archive()),
          ),
        ],
      ),
    );
  }

  Future<void> _pickMerge(BuildContext context) async {
    final chosen = await showDialog<String>(
      context: context,
      builder: (ctx) => SimpleDialog(
        title: Text('Merge “${widget.type.name}” into'),
        children: [
          for (final o in widget.others)
            SimpleDialogOption(
              onPressed: () => Navigator.of(ctx).pop(o.id),
              child: Text(o.name),
            ),
        ],
      ),
    );
    if (chosen != null && context.mounted) {
      Navigator.of(context).pop(_Merge(chosen));
    }
  }
}

// --------------------------------------------------------------------- //
// The labelling queue
// --------------------------------------------------------------------- //
class _UntypedList extends ConsumerWidget {
  const _UntypedList();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final rows = ref.watch(untypedSpendingProvider);
    final theme = Theme.of(context);

    return rows.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(child: Text('$e')),
      data: (list) {
        if (list.isEmpty) {
          return Center(
            child: Padding(
              padding: const EdgeInsets.all(28),
              child: Text(
                'Everything filed in the last 90 days has a kind.',
                textAlign: TextAlign.center,
                style: theme.textTheme.bodyMedium,
              ),
            ),
          );
        }
        return RefreshIndicator(
          onRefresh: () async => ref.invalidate(untypedSpendingProvider),
          child: ListView.builder(
            itemCount: list.length,
            itemBuilder: (_, i) => _UntypedRow(row: list[i]),
          ),
        );
      },
    );
  }
}

class _UntypedRow extends ConsumerStatefulWidget {
  final SpendRow row;
  const _UntypedRow({required this.row});

  @override
  ConsumerState<_UntypedRow> createState() => _UntypedRowState();
}

class _UntypedRowState extends ConsumerState<_UntypedRow> {
  TypeSuggestionResult? _suggestion;
  bool _busy = false;
  bool _done = false;

  Future<void> _suggest() async {
    setState(() => _busy = true);
    try {
      final s = await ref.read(apiProvider).suggestExpenseType(
            merchant: widget.row.merchant,
            amountCents: widget.row.amountCents,
          );
      if (mounted) setState(() => _suggestion = s);
    } on ApiException catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(e.message)));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _apply(String name, {String? description, bool fromAi = false}) async {
    setState(() => _busy = true);
    try {
      await ref.read(apiProvider).setTransactionExpenseType(
            widget.row.transactionId,
            name: name,
            description: description,
            fromAi: fromAi,
          );
      if (mounted) setState(() => _done = true);
      ref.invalidate(expenseTypesProvider);
      ref.invalidate(insightsProvider);
    } on ApiException catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(e.message)));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _pickManually() async {
    final suggestions = await ref.read(apiProvider).expenseTypeSuggestions();
    if (!mounted) return;
    final picked = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      builder: (_) => _AddTypeSheet(suggestions: suggestions),
    );
    if (picked == null) return;
    await _apply(
      picked,
      description: suggestions
          .where((s) => s.name == picked)
          .map((s) => s.description)
          .where((d) => d.isNotEmpty)
          .firstOrNull,
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final r = widget.row;

    if (_done) {
      return ListTile(
        leading: Icon(Icons.check, color: theme.colorScheme.primary),
        title: Text(r.merchant, style: TextStyle(color: theme.disabledColor)),
      );
    }

    final s = _suggestion;
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(r.merchant, style: theme.textTheme.titleSmall),
                ),
                Text(Money.format(r.amountCents),
                    style: theme.textTheme.titleSmall),
              ],
            ),
            Text('${r.date} · paid from ${r.category}',
                style: theme.textTheme.bodySmall),
            const SizedBox(height: 10),
            if (_busy)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 6),
                child: SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
              )
            else if (s == null)
              Row(
                children: [
                  FilledButton.tonalIcon(
                    onPressed: _suggest,
                    icon: const Icon(Icons.auto_awesome, size: 16),
                    label: const Text('Suggest'),
                  ),
                  const SizedBox(width: 8),
                  TextButton(
                    onPressed: _pickManually,
                    child: const Text('Choose'),
                  ),
                ],
              )
            else if (s.unsure)
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    s.why.isEmpty
                        ? 'The assistant could not tell what this was.'
                        : s.why,
                    style: theme.textTheme.bodySmall,
                  ),
                  const SizedBox(height: 8),
                  TextButton(
                      onPressed: _pickManually, child: const Text('Choose')),
                ],
              )
            else
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Chip(
                        label: Text(s.name!),
                        avatar: Icon(
                          s.isNew ? Icons.add : Icons.label_outline,
                          size: 15,
                        ),
                      ),
                      const SizedBox(width: 8),
                      Text(s.confidence, style: theme.textTheme.bodySmall),
                    ],
                  ),
                  if (s.isNew)
                    Text('This would be a new kind.',
                        style: theme.textTheme.bodySmall),
                  if (s.collapsedFrom != null)
                    // Evidence the duplicate guard did something, which
                    // is reassuring rather than noise: it shows why the
                    // taxonomy is not growing a synonym every time.
                    Text(
                      'Suggested “${s.collapsedFrom}”, using the existing '
                      '“${s.name}” instead.',
                      style: theme.textTheme.bodySmall,
                    ),
                  if (s.why.isNotEmpty) ...[
                    const SizedBox(height: 4),
                    Text(s.why, style: theme.textTheme.bodySmall),
                  ],
                  const SizedBox(height: 10),
                  Row(
                    children: [
                      FilledButton(
                        onPressed: () => _apply(s.name!,
                            description: s.description, fromAi: true),
                        child: const Text('Use it'),
                      ),
                      const SizedBox(width: 8),
                      TextButton(
                          onPressed: _pickManually,
                          child: const Text('Something else')),
                    ],
                  ),
                ],
              ),
          ],
        ),
      ),
    );
  }
}

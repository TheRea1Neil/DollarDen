import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../core/theme.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';
import 'add_category_screen.dart';
import 'assisted_review_screen.dart';
import 'scan_receipt_screen.dart';

/// The review queue: transactions the deterministic rules could not place.
///
/// This is the highest-traffic screen in the app, so resolving a single
/// transaction is one tap on a category. Splitting is available but takes a
/// second step, because it is the rarer case.
/// Clear imported history in one go.
///
/// Connecting a bank imports whatever the institution offers -- around
/// three months, which arrived as 167 charges to file. Nobody is going
/// to categorise a backlog they never meant to budget, and the obvious
/// workaround is deleting rows, which loses the record. These are
/// marked ignored instead: they stay in history, and because they never
/// allocated anything no balance moves.
Future<void> dismissOldHistory(BuildContext context, WidgetRef ref) async {
  final now = DateTime.now();
  final picked = await showDatePicker(
    context: context,
    initialDate: DateTime(now.year, now.month, 1),
    firstDate: DateTime(now.year - 3),
    lastDate: now,
    helpText: 'Dismiss everything BEFORE',
  );
  if (picked == null || !context.mounted) return;

  final api = ref.read(apiProvider);
  late final DismissPreview preview;
  try {
    preview = await api.dismissBefore(picked);
  } on ApiException catch (e) {
    if (context.mounted) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(e.message)));
    }
    return;
  }
  if (!context.mounted) return;

  if (preview.dismissed == 0) {
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Nothing to dismiss before that date')),
    );
    return;
  }

  // Always show the count before doing it. A mistyped year would
  // otherwise quietly clear the whole queue.
  final ok = await showDialog<bool>(
        context: context,
        builder: (dc) => AlertDialog(
          title: Text('Dismiss ${preview.dismissed} charges?'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('Everything before '
                  '${picked.toIso8601String().split('T').first} that you have '
                  'not already filed — ${preview.oldest} to ${preview.newest}, '
                  '${Money.format(preview.outflowCents)} of spending.'),
              const SizedBox(height: 12),
              const Text(
                'They stay in your history and your balances do not move. '
                'You can put any of them back from History by changing how '
                'it is treated.',
              ),
            ],
          ),
          actions: [
            TextButton(
                onPressed: () => Navigator.of(dc).pop(false),
                child: const Text('Cancel')),
            FilledButton(
                onPressed: () => Navigator.of(dc).pop(true),
                child: const Text('Dismiss them')),
          ],
        ),
      ) ??
      false;
  if (!ok || !context.mounted) return;

  try {
    final done = await api.dismissBefore(picked, dryRun: false);
    ref.invalidate(reviewProvider);
    ref.invalidate(summaryProvider);
    if (!context.mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text('Dismissed ${done.dismissed} charges')),
    );
  } on ApiException catch (e) {
    if (context.mounted) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(e.message)));
    }
  }
}

class ReviewScreen extends ConsumerWidget {
  /// Rendered inside the Transactions tab rather than as its own tab.
  ///
  /// When embedded the app bar belongs to the tab, which also hosts the
  /// "dismiss old history" action -- it is a Review action, so the tab
  /// only shows it while this section is the one on screen.
  final bool embedded;

  const ReviewScreen({super.key, this.embedded = false});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final items = ref.watch(reviewProvider);

    return Scaffold(
      appBar: embedded
          ? null
          : AppBar(
              title: const Text('Review'),
              actions: [
                IconButton(
                  tooltip: 'Dismiss old history',
                  icon: const Icon(Icons.history_toggle_off),
                  onPressed: () => dismissOldHistory(context, ref),
                ),
              ],
            ),
      body: items.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => Center(child: Text('$e')),
        data: (list) {
          if (list.isEmpty) {
            return const _AllClear();
          }
          return RefreshIndicator(
            onRefresh: () async {
              ref.invalidate(reviewProvider);
              await ref.read(reviewProvider.future);
            },
            child: ListView.separated(
              padding: const EdgeInsets.all(16),
              itemCount: list.length + 1,
              separatorBuilder: (_, __) => const SizedBox(height: 10),
              itemBuilder: (context, i) {
                if (i == 0) {
                  // Walking the backlog with a suggestion per charge is
                  // the fast path; the list below stays for picking off
                  // individual ones.
                  return Padding(
                    padding: const EdgeInsets.only(bottom: 4),
                    child: FilledButton.tonalIcon(
                      onPressed: () => Navigator.of(context).push(
                        MaterialPageRoute<void>(
                          builder: (_) =>
                              AssistedReviewScreen(items: list),
                        ),
                      ),
                      icon: const Icon(Icons.auto_awesome, size: 18),
                      label: Text('Go through all ${list.length} with the '
                          'assistant'),
                    ),
                  );
                }
                return _ReviewCard(item: list[i - 1]);
              },
            ),
          );
        },
      ),
    );
  }
}

class _AllClear extends StatelessWidget {
  const _AllClear();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.check_circle_outline, size: 52, color: theme.colorScheme.primary),
          const SizedBox(height: 16),
          Text('Nothing to review', style: theme.textTheme.titleMedium),
          const SizedBox(height: 6),
          Text('Every transaction has been categorised.',
              style: theme.textTheme.bodyMedium
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
        ],
      ),
    );
  }
}

/// Whether the assistant is still going to deal with this one.
///
/// Every open item used to look identical, so there was no way to tell
/// "it is still working on this" from "it looked and gave up" from "it
/// never saw this at all". Two of those resolve themselves and two
/// never will.
class _AiStatus extends StatelessWidget {
  final ReviewItem item;
  const _AiStatus({required this.item});

  /// "15:36" from the server's ISO timestamp, in local time.
  String? get _retryAt {
    final raw = item.aiRetryAt;
    if (raw == null) return null;
    final t = DateTime.tryParse(raw)?.toLocal();
    if (t == null) return null;
    return '${t.hour.toString().padLeft(2, '0')}:'
        '${t.minute.toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    final (IconData icon, String label, Color colour, String? detail) =
        switch (item.aiState) {
      'working' => (
          Icons.auto_awesome,
          'The assistant is looking at this',
          theme.colorScheme.primary,
          null,
        ),
      'deferred' => (
          Icons.hourglass_empty,
          _retryAt == null
              ? 'Waiting for the AI computer'
              : 'Waiting for the AI computer — around ${_retryAt!}',
          theme.colorScheme.onSurfaceVariant,
          item.aiReason,
        ),
      'suggested' => (
          Icons.lightbulb_outline,
          item.aiSuggestedCategory == null
              ? 'Your call — the assistant was not sure enough'
              : 'Your call — it suggests ${item.aiSuggestedCategory}',
          theme.colorScheme.tertiary,
          item.aiReason,
        ),
      'unsure' => (
          Icons.help_outline,
          'Your call — the assistant could not decide',
          theme.colorScheme.tertiary,
          item.aiReason,
        ),
      'failed' => (
          Icons.error_outline,
          'Your call — the assistant could not run',
          theme.colorScheme.error,
          item.aiReason,
        ),
      _ => (
          Icons.person_outline,
          'Your call — not looked at automatically',
          theme.colorScheme.onSurfaceVariant,
          null,
        ),
    };

    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(children: [
            Icon(icon, size: 14, color: colour),
            const SizedBox(width: 6),
            Expanded(
              child: Text(label,
                  style: theme.textTheme.labelSmall?.copyWith(color: colour)),
            ),
          ]),
          // The model's own reasoning. This is the half worth reading --
          // "Amazon sells everything from groceries to cloud services"
          // tells you why it is your call far better than any status.
          if (detail != null && detail.trim().isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 3, left: 20),
              child: Text(detail.trim(),
                  style: theme.textTheme.bodySmall?.copyWith(
                      color: theme.colorScheme.onSurfaceVariant),
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis),
            ),
        ],
      ),
    );
  }
}

class _ReviewCard extends ConsumerWidget {
  final ReviewItem item;
  const _ReviewCard({required this.item});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(16),
        onTap: () => _choose(context, ref),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(item.title,
                        style: theme.textTheme.titleMedium
                            ?.copyWith(fontWeight: FontWeight.w600),
                        overflow: TextOverflow.ellipsis),
                    const SizedBox(height: 2),
                    Text(item.date,
                        style: theme.textTheme.bodySmall
                            ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                    _AiStatus(item: item),
                  ],
                ),
              ),
              const SizedBox(width: 12),
              _FlowAmount(signedCents: item.amountCents),
              const Icon(Icons.chevron_right),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _choose(BuildContext context, WidgetRef ref) async {
    final categories = await ref.read(categoriesProvider.future);
    if (!context.mounted) return;

    final result = await showModalBottomSheet<_PickerResult>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (ctx) => _CategoryPicker(item: item, categories: categories),
    );
    if (result == null || !context.mounted) return;

    if (result is _PickReopen) {
      ref.invalidate(categoriesProvider);
      await _choose(context, ref);
      return;
    }

    if (result is _PickItemise) {
      await Navigator.of(context).push<bool>(
        MaterialPageRoute(
          builder: (_) => ScanReceiptScreen(
            transactionId: item.transactionId,
            amountCents: item.amountCents,
            title: item.merchantName ?? item.name,
          ),
        ),
      );
      ref.invalidate(reviewProvider);
      ref.invalidate(summaryProvider);
      return;
    }

    final api = ref.read(apiProvider);
    try {
      switch (result) {
        case _PickExisting(:final categoryId, :final remember):
          await api.resolveReview(
            item.id,
            // One category takes the whole amount. Splitting is separate.
            [(categoryId: categoryId, amountCents: item.amountCents)],
            remember: remember,
          );
        case _PickNew(
            :final name,
            :final kind,
            :final targetCents,
            :final remember,
            :final description
          ):
          await api.resolveReviewWithNewCategory(
            item.id, name: name, kind: kind, description: description,
            targetCents: targetCents, remember: remember,
          );
        case _PickItemise():
          return;   // handled before the switch
        case _PickReopen():
          // Handled before the switch; listed so the match stays
          // exhaustive and a new result type cannot be forgotten.
          return;
        case _PickExclude(:final treatment):
          await api.setTreatment(item.transactionId, treatment);
      }
      ref.invalidate(reviewProvider);
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(result is _PickExclude
              ? '${item.title} excluded from the budget'
              : '${item.title} categorised'),
        ));
      }
    } on ApiException catch (e) {
      if (context.mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(e.message)));
      }
    }
  }
}

sealed class _PickerResult {
  const _PickerResult();
}

class _PickExisting extends _PickerResult {
  final String categoryId;
  final bool remember;
  const _PickExisting(this.categoryId, this.remember);
}

class _PickNew extends _PickerResult {
  final String name;
  final String kind;
  final int? targetCents;
  final bool remember;
  final String? description;
  const _PickNew(this.name, this.kind, this.targetCents, this.remember,
      {this.description});
}

/// "This charge covers several things — let me itemise it."
class _PickItemise extends _PickerResult {
  const _PickItemise();
}

/// "I made a category, show me the list again."
class _PickReopen extends _PickerResult {
  const _PickReopen();
}

class _PickExclude extends _PickerResult {
  final String treatment;
  const _PickExclude(this.treatment);
}

class _CategoryPicker extends StatefulWidget {
  final ReviewItem item;
  final List<Category> categories;
  const _CategoryPicker({required this.item, required this.categories});

  @override
  State<_CategoryPicker> createState() => _CategoryPickerState();
}

class _CategoryPickerState extends State<_CategoryPicker> {
  // Off by default: a matcher created here permanently short-circuits
  // both review and the assistant for that merchant.
  bool _remember = false;

  Future<void> _createCategory(BuildContext context) async {
    // Offer the assistant here too. Creating a category was manual-only
    // from this flow, which is the one place you are most likely to be
    // mid-thought about what the category is actually for.
    final how = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(
            leading: const Icon(Icons.auto_awesome),
            title: const Text('Describe it'),
            subtitle: const Text('The assistant drafts it, including how '
                'to fund it.'),
            onTap: () => Navigator.pop(ctx, 'ai'),
          ),
          ListTile(
            leading: const Icon(Icons.edit_outlined),
            title: const Text('Set it up myself'),
            onTap: () => Navigator.pop(ctx, 'manual'),
          ),
        ]),
      ),
    );
    if (how == null || !context.mounted) return;

    if (how == 'ai') {
      await Navigator.of(context).push<bool>(
        MaterialPageRoute(
          builder: (_) => AddCategoryScreen(seedRequest: widget.item.title),
        ),
      );
      // Hand control back so the caller can refresh the list and
      // reopen, rather than dropping the user out of the flow with a
      // category they cannot yet pick.
      if (context.mounted) Navigator.pop(context, const _PickReopen());
      return;
    }

    final created = await showDialog<_PickNew>(
      context: context,
      builder: (ctx) => _NewCategoryDialog(remember: _remember),
    );
    if (created != null && context.mounted) Navigator.pop(context, created);
  }

  Future<void> _excludeSheet(BuildContext context) async {
    final choice = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Padding(
              padding: EdgeInsets.fromLTRB(16, 0, 16, 8),
              child: Text(
                'Money that moves without being spent is left out of the '
                'budget, so card purchases are not counted twice.',
                textAlign: TextAlign.center,
              ),
            ),
            ListTile(
              leading: const Icon(Icons.credit_card),
              title: const Text('Credit card payment'),
              onTap: () => Navigator.pop(ctx, 'card_payment'),
            ),
            ListTile(
              leading: const Icon(Icons.swap_horiz),
              title: const Text('Transfer between my accounts'),
              onTap: () => Navigator.pop(ctx, 'transfer'),
            ),
            ListTile(
              leading: const Icon(Icons.visibility_off_outlined),
              title: const Text('Just ignore it'),
              onTap: () => Navigator.pop(ctx, 'ignored'),
            ),
          ],
        ),
      ),
    );
    if (choice != null && context.mounted) {
      Navigator.pop(context, _PickExclude(choice));
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(widget.item.title,
                style: theme.textTheme.titleLarge
                    ?.copyWith(fontWeight: FontWeight.w700)),
            const SizedBox(height: 8),
            _FlowAmount(signedCents: widget.item.amountCents, compact: false),
            const SizedBox(height: 8),
            // Say the consequence outright. This is the moment the choice
            // is made, and "adds to" vs "takes out of" is the whole
            // difference between a refund and a charge.
            Builder(builder: (_) {
              final flow = MoneyFlow.ofSigned(widget.item.amountCents);
              final amount = Money.format(widget.item.amountCents.abs());
              return Text(
                flow.isInto
                    ? 'Picking a category ADDS $amount to it.'
                    : 'Picking a category TAKES $amount OUT of it.',
                textAlign: TextAlign.center,
                style: theme.textTheme.bodyMedium?.copyWith(
                  color: flow.isInto
                      ? AppTheme.positive(context)
                      : theme.colorScheme.onSurfaceVariant,
                  fontWeight: flow.isInto ? FontWeight.w600 : null,
                ),
              );
            }),
            const SizedBox(height: 12),
            Flexible(
              child: ListView(
                shrinkWrap: true,
                children: [
                  for (final c in widget.categories)
                    ListTile(
                      title: Text(c.name),
                      subtitle: Text(Money.format(c.balanceCents)),
                      trailing: const Icon(Icons.chevron_right),
                      onTap: () => Navigator.pop(
                          context, _PickExisting(c.id, _remember)),
                    ),
                  const Divider(),
                  ListTile(
                    leading: const Icon(Icons.receipt_long_outlined),
                    title: const Text('Split by receipt'),
                    subtitle: const Text('Photograph or paste it and file '
                        'each item separately.'),
                    onTap: () => Navigator.pop(context, const _PickItemise()),
                  ),
                  // Noticing a charge fits nowhere is exactly when you
                  // realise a category is missing, so make one here.
                  ListTile(
                    leading: CircleAvatar(
                      backgroundColor:
                          theme.colorScheme.primaryContainer,
                      child: Icon(Icons.add,
                          color: theme.colorScheme.onPrimaryContainer),
                    ),
                    title: const Text('New category…'),
                    subtitle: const Text('Create one for this charge'),
                    onTap: () => _createCategory(context),
                  ),
                  ListTile(
                    leading: CircleAvatar(
                      backgroundColor:
                          theme.colorScheme.surfaceContainerHighest,
                      child: const Icon(Icons.block),
                    ),
                    title: const Text("Don't count this"),
                    subtitle: const Text('Card payment or transfer'),
                    onTap: () => _excludeSheet(context),
                  ),
                ],
              ),
            ),
            CheckboxListTile(
              value: _remember,
              onChanged: (v) => setState(() => _remember = v ?? false),
              title: const Text('Always file this merchant here'),
              subtitle: Text(_remember
                  ? 'Future charges skip review entirely.'
                  : 'Off — you will be asked again next time.'),
              contentPadding: EdgeInsets.zero,
            ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    );
  }
}


class _NewCategoryDialog extends StatefulWidget {
  final bool remember;
  const _NewCategoryDialog({required this.remember});

  @override
  State<_NewCategoryDialog> createState() => _NewCategoryDialogState();
}

class _NewCategoryDialogState extends State<_NewCategoryDialog> {
  final _name = TextEditingController();
  final _description = TextEditingController();
  final _target = TextEditingController();
  String _kind = 'spending';

  @override
  void dispose() {
    _name.dispose();
    _description.dispose();
    _target.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('New category'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          TextField(
            controller: _name,
            autofocus: true,
            textCapitalization: TextCapitalization.words,
            decoration: const InputDecoration(labelText: 'Name'),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _description,
            textCapitalization: TextCapitalization.sentences,
            decoration: const InputDecoration(
              labelText: 'What it is for (optional)',
              hintText: 'Groceries from a shop; eating out is separate.',
              helperText: 'The assistant reads this when writing your '
                  'summaries.',
              helperMaxLines: 2,
            ),
          ),
          const SizedBox(height: 12),
          DropdownButtonFormField<String>(
            initialValue: _kind,
            decoration: const InputDecoration(labelText: 'Type'),
            items: const [
              DropdownMenuItem(value: 'spending', child: Text('Spending')),
              DropdownMenuItem(
                  value: 'savings_bucket', child: Text('Savings bucket')),
              DropdownMenuItem(value: 'obligation', child: Text('Obligation')),
            ],
            onChanged: (v) => setState(() => _kind = v ?? 'spending'),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _target,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            decoration: const InputDecoration(
              labelText: 'Refill target (optional)',
              prefixText: r'$',
            ),
          ),
        ],
      ),
      actions: [
        TextButton(
            onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          onPressed: () {
            final name = _name.text.trim();
            if (name.isEmpty) return;
            Navigator.pop(
              context,
              _PickNew(name, _kind, Money.parse(_target.text), widget.remember,
                  description: _description.text.trim().isEmpty
                      ? null
                      : _description.text.trim()),
            );
          },
          child: const Text('Create & assign'),
        ),
      ],
    );
  }
}


/// A budget-signed amount, shown so the direction is unmistakable.
///
/// A leading minus alone is not enough: the backend stores spending as
/// POSITIVE, so a raw render shows a refund as "-$20.00" and reads like a
/// loss. Colour, an arrow and a word all say the same thing here, because
/// filing a refund as if it were spending is a mistake the user cannot
/// see once it is made.
class _FlowAmount extends StatelessWidget {
  final int signedCents;
  final bool compact;

  const _FlowAmount({required this.signedCents, this.compact = true});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final flow = MoneyFlow.ofSigned(signedCents);
    final colour = switch (flow) {
      MoneyFlow.out => theme.colorScheme.onSurface,
      MoneyFlow.into => AppTheme.positive(context),
      MoneyFlow.none => theme.colorScheme.onSurfaceVariant,
    };

    return Column(
      crossAxisAlignment: CrossAxisAlignment.end,
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (flow != MoneyFlow.none)
              Icon(
                flow.isOut ? Icons.arrow_outward : Icons.south_west,
                size: 16,
                color: colour,
              ),
            const SizedBox(width: 4),
            Text(
              MoneyFlowFormat.flow(signedCents),
              style: theme.textTheme.titleMedium?.copyWith(
                fontWeight: FontWeight.w700,
                color: colour,
                fontFeatures: const [FontFeature.tabularFigures()],
              ),
            ),
          ],
        ),
        if (!compact || flow.isInto)
          Text(
            flow.isInto ? 'money in' : 'spent',
            style: theme.textTheme.labelSmall?.copyWith(color: colour),
          ),
      ],
    );
  }
}

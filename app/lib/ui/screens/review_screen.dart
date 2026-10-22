import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';

/// The review queue: transactions the deterministic rules could not place.
///
/// This is the highest-traffic screen in the app, so resolving a single
/// transaction is one tap on a category. Splitting is available but takes a
/// second step, because it is the rarer case.
class ReviewScreen extends ConsumerWidget {
  const ReviewScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final items = ref.watch(reviewProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Review')),
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
              itemCount: list.length,
              separatorBuilder: (_, __) => const SizedBox(height: 10),
              itemBuilder: (_, i) => _ReviewCard(item: list[i]),
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
                  ],
                ),
              ),
              const SizedBox(width: 12),
              Text(
                Money.format(item.amountCents),
                style: theme.textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w700,
                  fontFeatures: const [FontFeature.tabularFigures()],
                ),
              ),
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
        case _PickNew(:final name, :final kind, :final targetCents, :final remember):
          await api.resolveReviewWithNewCategory(
            item.id, name: name, kind: kind,
            targetCents: targetCents, remember: remember,
          );
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
  const _PickNew(this.name, this.kind, this.targetCents, this.remember);
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
  bool _remember = true;

  Future<void> _createCategory(BuildContext context) async {
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
            Text(Money.format(widget.item.amountCents),
                style: theme.textTheme.titleMedium
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
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
              title: const Text('Remember this merchant'),
              subtitle: const Text('Categorise it automatically next time'),
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
  final _target = TextEditingController();
  String _kind = 'spending';

  @override
  void dispose() {
    _name.dispose();
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
              _PickNew(name, _kind, Money.parse(_target.text), widget.remember),
            );
          },
          child: const Text('Create & assign'),
        ),
      ],
    );
  }
}

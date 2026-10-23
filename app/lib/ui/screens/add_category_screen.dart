import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';

/// Describe one category in plain words and let the assistant draft it.
///
/// Separate from the full budget designer on purpose: once a split plan is
/// tuned, regenerating the whole thing to add "Dog Vet" risks changing
/// rules the user already settled. This touches one category and inserts
/// at most one rule.
class AddCategoryScreen extends ConsumerStatefulWidget {
  /// A merchant to start from, when this is opened because a charge fit
  /// nowhere. Saves retyping what you were just looking at.
  final String? seedRequest;

  const AddCategoryScreen({super.key, this.seedRequest});

  @override
  ConsumerState<AddCategoryScreen> createState() => _AddCategoryScreenState();
}

class _AddCategoryScreenState extends ConsumerState<AddCategoryScreen> {
  final _request = TextEditingController();

  bool _busy = false;
  String _progress = '';
  String? _error;
  SuggestedCategory? _draft;

  @override
  void initState() {
    super.initState();
    final seed = widget.seedRequest;
    if (seed != null && seed.trim().isNotEmpty) {
      _request.text = 'Somewhere to put charges like "$seed".';
    }
  }

  @override
  void dispose() {
    _request.dispose();
    super.dispose();
  }

  Future<void> _suggest() async {
    final text = _request.text.trim();
    if (text.isEmpty) return;
    final api = ref.read(apiProvider);
    setState(() {
      _busy = true;
      _error = null;
      _draft = null;
      _progress = 'Queued…';
    });
    try {
      final job = await api.queueJob('design_category', {'request': text});
      final done = await api.awaitJob(job.id, onProgress: (j) {
        if (mounted) setState(() => _progress = j.progressLabel);
      });
      if (!mounted) return;
      if (!done.ok) {
        setState(() => _error = done.error ?? 'The assistant could not finish.');
        return;
      }
      setState(() => _draft = SuggestedCategory.fromJson(done.result ?? {}));
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _create() async {
    final d = _draft;
    if (d == null) return;
    setState(() => _busy = true);
    try {
      final out = await ref.read(apiProvider).applyCategory(
            category: d.toProposed(),
            ruleType: d.ruleType,
            params: d.params,
            matcher: d.autoMatch && (d.merchantHint?.trim().isNotEmpty ?? false)
                ? {'merchant_contains': d.merchantHint!.trim().toLowerCase()}
                : null,
          );
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      if (!mounted) return;
      final pos = out['funding_position'];
      Navigator.of(context).pop(true);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(pos == null
            ? '${d.name} created'
            : '${d.name} created · funded ${_ordinal(pos as int)} '
                'from each paycheck'),
      ));
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _busy = false);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(e.message),
        backgroundColor: Theme.of(context).colorScheme.error,
      ));
    }
  }

  static String _ordinal(int n) {
    if (n % 100 >= 11 && n % 100 <= 13) return '${n}th';
    return switch (n % 10) {
      1 => '${n}st',
      2 => '${n}nd',
      3 => '${n}rd',
      _ => '${n}th',
    };
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('New category')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text('What is this category for?',
              style: theme.textTheme.titleMedium
                  ?.copyWith(fontWeight: FontWeight.w600)),
          const SizedBox(height: 4),
          Text(
            'Describe it the way you would to a person. The assistant '
            'drafts it and you fix anything it got wrong before it is '
            'created.',
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _request,
            maxLines: 4,
            minLines: 3,
            autofocus: true,
            textCapitalization: TextCapitalization.sentences,
            decoration: const InputDecoration(
              hintText: "Money for the dog — food, and vet visits that always "
                  "seem to come out of nowhere. I'd like about \$500 sitting "
                  "there.",
            ),
          ),
          const SizedBox(height: 16),
          FilledButton.icon(
            onPressed: _busy ? null : _suggest,
            icon: _busy
                ? const SizedBox(
                    height: 18, width: 18,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.auto_awesome),
            label: Text(_busy ? _progress : 'Draft it'),
          ),
          if (_busy && _progress.contains('Waking'))
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                'The AI server sleeps when idle — this takes about ten '
                'seconds the first time.',
                textAlign: TextAlign.center,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
              ),
            ),
          if (_error != null) ...[
            const SizedBox(height: 16),
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: theme.colorScheme.errorContainer,
                borderRadius: BorderRadius.circular(12),
              ),
              child: Text(_error!,
                  style: TextStyle(color: theme.colorScheme.onErrorContainer)),
            ),
          ],
          if (_draft != null) ...[
            const SizedBox(height: 24),
            _DraftEditor(
              draft: _draft!,
              busy: _busy,
              onChanged: () => setState(() {}),
              onCreate: _create,
            ),
          ],
        ],
      ),
    );
  }
}

/// The draft, fully editable. Nothing here is committed until "Create".
class _DraftEditor extends ConsumerWidget {
  final SuggestedCategory draft;
  final bool busy;
  final VoidCallback onChanged;
  final VoidCallback onCreate;

  const _DraftEditor({
    required this.draft,
    required this.busy,
    required this.onChanged,
    required this.onCreate,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);

    // Checked live against the real list rather than trusting the
    // server's verdict on the model's original name: the user may well
    // have just renamed the thing to fix exactly this.
    final taken = ref.watch(categoriesProvider).maybeWhen(
          data: (cats) => cats.any((c) =>
              c.name.toLowerCase() == draft.name.trim().toLowerCase()),
          orElse: () => false,
        );

    // Anything the server said about the old name is no longer about
    // what is on screen.
    final problems = [
      for (final p in draft.problems)
        if (!p.contains('already exists')) p,
      if (taken) '"${draft.name.trim()}" already exists — pick another name',
    ];

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Draft', style: theme.textTheme.titleMedium
                ?.copyWith(fontWeight: FontWeight.w600)),
            const SizedBox(height: 12),
            TextFormField(
              initialValue: draft.name,
              decoration: const InputDecoration(labelText: 'Name'),
              onChanged: (v) {
                draft.name = v;
                onChanged();
              },
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              initialValue: draft.kind,
              decoration: const InputDecoration(labelText: 'Kind'),
              items: const [
                DropdownMenuItem(
                    value: 'spending', child: Text('Spending — refilled and spent down')),
                DropdownMenuItem(
                    value: 'savings_bucket', child: Text('Savings — builds up')),
                DropdownMenuItem(
                    value: 'obligation', child: Text('Obligation — a bill you owe')),
              ],
              onChanged: (v) {
                if (v != null) draft.kind = v;
                onChanged();
              },
            ),
            const SizedBox(height: 12),
            TextFormField(
              initialValue: draft.description,
              maxLines: 2,
              decoration: const InputDecoration(
                labelText: 'What it is for',
                helperText: 'The assistant reads this later when sorting '
                    'transactions and writing summaries.',
                helperMaxLines: 3,
              ),
              onChanged: (v) {
                draft.description = v;
                onChanged();
              },
            ),
            const SizedBox(height: 12),
            TextFormField(
              initialValue: draft.targetCents == null
                  ? ''
                  : (draft.targetCents! / 100).toStringAsFixed(2),
              keyboardType: const TextInputType.numberWithOptions(decimal: true),
              decoration: const InputDecoration(
                labelText: 'Target',
                prefixText: '\$',
                helperText: 'Leave blank if there is no set amount.',
              ),
              onChanged: (v) {
                final parsed = double.tryParse(v.replaceAll(',', ''));
                draft.targetCents =
                    v.trim().isEmpty || parsed == null ? null : (parsed * 100).round();
                onChanged();
              },
            ),
            const SizedBox(height: 16),
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: theme.colorScheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(12),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Funding',
                      style: theme.textTheme.labelLarge
                          ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                  const SizedBox(height: 4),
                  Text(draft.fundingSummary, style: theme.textTheme.bodyMedium),
                  if (draft.why.isNotEmpty) ...[
                    const SizedBox(height: 4),
                    Text(draft.why,
                        style: theme.textTheme.bodySmall?.copyWith(
                            color: theme.colorScheme.onSurfaceVariant,
                            fontStyle: FontStyle.italic)),
                  ],
                  if (draft.ruleType != null) ...[
                    const SizedBox(height: 8),
                    Text(
                      'It goes last in the funding order, just ahead of '
                      'whatever sweeps up the remainder, so it cannot '
                      'starve your bills.',
                      style: theme.textTheme.bodySmall
                          ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
                    ),
                    const SizedBox(height: 4),
                    TextButton.icon(
                      onPressed: () {
                        draft.ruleType = null;
                        draft.params = const {};
                        onChanged();
                      },
                      icon: const Icon(Icons.block, size: 16),
                      label: const Text("Don't fund it automatically"),
                    ),
                  ],
                ],
              ),
            ),
            if (draft.merchantHint != null &&
                draft.merchantHint!.trim().isNotEmpty) ...[
              const SizedBox(height: 8),
              // Off by default. A wrong guess here quietly files real
              // spending into the wrong envelope, and the user would have
              // no reason to go looking for it.
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                value: draft.autoMatch,
                onChanged: (v) {
                  draft.autoMatch = v;
                  onChanged();
                },
                title: const Text('File matching transactions here '
                    'automatically'),
                subtitle: Text(
                  'Skips the review queue for anything matching. You can '
                  'change or remove this later under matching rules.',
                  style: theme.textTheme.bodySmall,
                ),
              ),
              if (draft.autoMatch)
                // Editable, because the model's guess is often too narrow:
                // "dog food" will never match VCA ANIMAL HOSPITAL.
                TextFormField(
                  initialValue: draft.merchantHint,
                  decoration: const InputDecoration(
                    labelText: 'Match when the name contains',
                    helperText: 'One word works best. Case does not matter.',
                  ),
                  onChanged: (v) {
                    draft.merchantHint = v;
                    onChanged();
                  },
                ),
            ],
            for (final n in draft.notices) ...[
              const SizedBox(height: 8),
              Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                const Icon(Icons.info_outline, size: 16),
                const SizedBox(width: 6),
                Expanded(child: Text(n, style: theme.textTheme.bodySmall)),
              ]),
            ],
            for (final p in problems) ...[
              const SizedBox(height: 8),
              Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Icon(Icons.error_outline,
                    size: 16, color: theme.colorScheme.error),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(p,
                      style: theme.textTheme.bodySmall
                          ?.copyWith(color: theme.colorScheme.error)),
                ),
              ]),
            ],
            const SizedBox(height: 16),
            FilledButton(
              onPressed:
                  busy || draft.name.trim().isEmpty || problems.isNotEmpty
                      ? null
                      : onCreate,
              child: Text('Create ${draft.name.trim().isEmpty ? '' : draft.name}'),
            ),
          ],
        ),
      ),
    );
  }
}

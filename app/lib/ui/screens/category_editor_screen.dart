import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';

/// Create or edit one category by hand.
///
/// The counterpart to the AI draft screen: same fields, no model involved.
/// Editing an existing category is the same form, because a category the
/// assistant drafted and one typed by hand are the same thing afterward.
class CategoryEditorScreen extends ConsumerStatefulWidget {
  /// Null to create a new one.
  final Category? existing;

  const CategoryEditorScreen({super.key, this.existing});

  @override
  ConsumerState<CategoryEditorScreen> createState() =>
      _CategoryEditorScreenState();
}

class _CategoryEditorScreenState extends ConsumerState<CategoryEditorScreen> {
  final _form = GlobalKey<FormState>();
  late final TextEditingController _name;
  late final TextEditingController _description;
  late final TextEditingController _target;
  late final TextEditingController _alert;
  late String _kind;

  bool _busy = false;
  String? _error;

  bool get _isNew => widget.existing == null;

  @override
  void initState() {
    super.initState();
    final c = widget.existing;
    _name = TextEditingController(text: c?.name ?? '');
    _description = TextEditingController(text: c?.description ?? '');
    _target = TextEditingController(
        text: c?.targetCents == null
            ? ''
            : (c!.targetCents! / 100).toStringAsFixed(2));
    _alert = TextEditingController(
        text: c?.alertAtCents == null
            ? ''
            : (c!.alertAtCents! / 100).toStringAsFixed(2));
    _kind = c?.kind ?? 'spending';
  }

  @override
  void dispose() {
    _name.dispose();
    _description.dispose();
    _target.dispose();
    _alert.dispose();
    super.dispose();
  }

  int? _cents(TextEditingController c) {
    final t = c.text.trim();
    if (t.isEmpty) return null;
    final v = double.tryParse(t.replaceAll(',', '').replaceAll('\$', ''));
    return v == null ? null : (v * 100).round();
  }

  Future<void> _save() async {
    if (!_form.currentState!.validate()) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    final api = ref.read(apiProvider);
    final description = _description.text.trim();
    try {
      if (_isNew) {
        await api.createCategory(
          name: _name.text.trim(),
          kind: _kind,
          description: description.isEmpty ? null : description,
          targetCents: _cents(_target),
          alertAtCents: _cents(_alert),
        );
      } else {
        // Send nulls explicitly: clearing a target is a real edit, and
        // omitting the key would silently keep the old value.
        await api.updateCategory(widget.existing!.id, {
          'name': _name.text.trim(),
          'kind': _kind,
          'description': description.isEmpty ? null : description,
          'target_cents': _cents(_target),
          'alert_at_cents': _cents(_alert),
        });
      }
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = e.message;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = '$e';
        });
      }
    }
  }

  Future<void> _archive() async {
    final c = widget.existing!;
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Archive ${c.name}?'),
        content: Text(
          c.balanceCents == 0
              ? 'It stops appearing in your budget. Past transactions keep '
                  'their history, and you can restore it later.'
              : 'It still holds ${Money.format(c.balanceCents)}. Archiving '
                  'leaves that money allocated to it — move it somewhere '
                  'else first if you want it back in circulation.',
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('Archive')),
        ],
      ),
    );
    if (ok != true) return;
    setState(() => _busy = true);
    try {
      await ref.read(apiProvider).archiveCategory(c.id);
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = e.message;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final c = widget.existing;

    return Scaffold(
      appBar: AppBar(
        title: Text(_isNew ? 'New category' : c!.name),
        actions: [
          if (!_isNew)
            IconButton(
              tooltip: 'Archive',
              icon: const Icon(Icons.archive_outlined),
              onPressed: _busy ? null : _archive,
            ),
        ],
      ),
      body: Form(
        key: _form,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (c != null) ...[
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Row(
                    children: [
                      Text('Balance',
                          style: theme.textTheme.labelLarge?.copyWith(
                              color: theme.colorScheme.onSurfaceVariant)),
                      const Spacer(),
                      Text(Money.format(c.balanceCents),
                          style: theme.textTheme.titleLarge?.copyWith(
                              fontWeight: FontWeight.w600,
                              color: c.overspent
                                  ? theme.colorScheme.error
                                  : null)),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 16),
            ],
            TextFormField(
              controller: _name,
              textCapitalization: TextCapitalization.words,
              autofocus: _isNew,
              decoration: const InputDecoration(labelText: 'Name'),
              validator: (v) =>
                  (v == null || v.trim().isEmpty) ? 'Give it a name' : null,
            ),
            const SizedBox(height: 16),
            DropdownButtonFormField<String>(
              initialValue: _kind,
              decoration: const InputDecoration(labelText: 'Kind'),
              items: const [
                DropdownMenuItem(
                    value: 'spending',
                    child: Text('Spending — refilled and spent down')),
                DropdownMenuItem(
                    value: 'savings_bucket',
                    child: Text('Savings — builds up over time')),
                DropdownMenuItem(
                    value: 'obligation',
                    child: Text('Obligation — a bill you owe')),
              ],
              onChanged: _busy ? null : (v) => setState(() => _kind = v ?? _kind),
            ),
            const SizedBox(height: 16),
            TextFormField(
              controller: _description,
              maxLines: 3,
              minLines: 2,
              textCapitalization: TextCapitalization.sentences,
              decoration: const InputDecoration(
                labelText: 'What it is for',
                hintText: 'Groceries only — eating out comes out of Fun.',
                helperText: 'The assistant reads this when it sorts '
                    'transactions and writes your summaries. It does not '
                    'affect how paychecks are split.',
                helperMaxLines: 4,
              ),
            ),
            const SizedBox(height: 16),
            TextFormField(
              controller: _target,
              keyboardType: const TextInputType.numberWithOptions(decimal: true),
              decoration: const InputDecoration(
                labelText: 'Target',
                prefixText: '\$',
                helperText: 'What a full envelope looks like. Leave blank if '
                    'there is no set amount.',
                helperMaxLines: 3,
              ),
              validator: (v) =>
                  (v == null || v.trim().isEmpty || _cents(_target) != null)
                      ? null
                      : 'Not a number',
            ),
            const SizedBox(height: 16),
            TextFormField(
              controller: _alert,
              keyboardType: const TextInputType.numberWithOptions(decimal: true),
              decoration: const InputDecoration(
                labelText: 'Warn me below',
                prefixText: '\$',
                helperText: 'Turns the balance amber when it drops under this.',
                helperMaxLines: 3,
              ),
              validator: (v) =>
                  (v == null || v.trim().isEmpty || _cents(_alert) != null)
                      ? null
                      : 'Not a number',
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
                    style:
                        TextStyle(color: theme.colorScheme.onErrorContainer)),
              ),
            ],
            const SizedBox(height: 24),
            FilledButton(
              onPressed: _busy ? null : _save,
              child: _busy
                  ? const SizedBox(
                      height: 18,
                      width: 18,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : Text(_isNew ? 'Create' : 'Save'),
            ),
            if (!_isNew) ...[
              const SizedBox(height: 16),
              Text(
                'Funding — how much this gets from each paycheck — lives in '
                'your split plan, not here.',
                textAlign: TextAlign.center,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

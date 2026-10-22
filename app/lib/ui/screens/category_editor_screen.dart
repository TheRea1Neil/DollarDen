import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';
import '../widgets/funding_editor.dart';
import 'refine_category_screen.dart';

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

  // Everything beyond the plain fields: funding, matchers, recent
  // activity. Loaded separately because a brand-new category has none
  // of it yet.
  CategoryDetail? _detail;
  bool _loadingDetail = false;
  String? _pendingRuleType;
  Map<String, dynamic> _pendingParams = const {};
  int? _pendingPosition;
  bool _fundingTouched = false;
  int _categoryCount = 1;

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
    if (!_isNew) _loadDetail();
  }

  Future<void> _loadDetail() async {
    setState(() => _loadingDetail = true);
    try {
      final api = ref.read(apiProvider);
      final d = await api.categoryDetail(widget.existing!.id);
      final all = await api.categories();
      if (!mounted) return;
      setState(() {
        _detail = d;
        _categoryCount = all.length;
        _pendingRuleType = d.funding?.ruleType;
        _pendingParams = d.funding?.params ?? const {};
        _pendingPosition = d.funding?.position;
        _loadingDetail = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loadingDetail = false;
        _error = 'Could not load this category\'s rules: $e';
      });
    }
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
      // Funding is a plan revision, so it is a separate call and only
      // made when the user actually touched it.
      if (!_isNew && _fundingTouched) {
        await api.setFunding(
          widget.existing!.id,
          ruleType: _pendingRuleType,
          params: _pendingParams,
          position: _pendingPosition,
        );
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

  Future<void> _refine() async {
    final changed = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => RefineCategoryScreen(detail: _detail!),
      ),
    );
    if (changed == true && mounted) {
      // The assistant may have changed the name, the funding, anything.
      // Reload rather than trying to guess what moved.
      final fresh = await ref.read(apiProvider).categoryDetail(widget.existing!.id);
      if (!mounted) return;
      setState(() {
        _detail = fresh;
        _name.text = fresh.category.name;
        _kind = fresh.category.kind;
        _description.text = fresh.category.description ?? '';
        _target.text = fresh.category.targetCents == null
            ? ''
            : (fresh.category.targetCents! / 100).toStringAsFixed(2);
        _alert.text = fresh.category.alertAtCents == null
            ? ''
            : (fresh.category.alertAtCents! / 100).toStringAsFixed(2);
        _pendingRuleType = fresh.funding?.ruleType;
        _pendingParams = fresh.funding?.params ?? const {};
        _pendingPosition = fresh.funding?.position;
        _fundingTouched = false;
      });
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
    }
  }

  Future<void> _removeMatcher(Matcher m) async {
    setState(() => _busy = true);
    try {
      await ref.read(apiProvider).deleteMatcher(m.id);
      await _loadDetail();
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _addMatcher() async {
    final result = await showDialog<Map<String, dynamic>>(
      context: context,
      builder: (ctx) => const _MatcherDialog(),
    );
    if (result == null) return;
    setState(() => _busy = true);
    try {
      await ref.read(apiProvider).addMatcher(widget.existing!.id, result);
      await _loadDetail();
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
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
              tooltip: 'Ask the assistant to change this',
              icon: const Icon(Icons.auto_awesome),
              onPressed: _busy || _detail == null ? null : _refine,
            ),
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
            if (!_isNew) ...[
              const SizedBox(height: 28),
              _SectionHeader(
                title: 'Funding',
                subtitle: 'How much of each paycheck lands here, and when '
                    'in the order. Changing this saves a new version of '
                    'your split plan.',
              ),
              if (_loadingDetail)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 24),
                  child: Center(child: CircularProgressIndicator()),
                )
              else
                FundingEditor(
                  initial: _detail?.funding,
                  categoryCount: _categoryCount,
                  onChanged: (type, params, position) {
                    _pendingRuleType = type;
                    _pendingParams = params;
                    _pendingPosition = position;
                    _fundingTouched = true;
                  },
                ),

              const SizedBox(height: 28),
              _SectionHeader(
                title: 'What comes out of it',
                subtitle: 'Transactions matching these skip the review '
                    'queue and are deducted here automatically. Anything '
                    'else you file by hand while reviewing.',
              ),
              ...[
                for (final m in _detail?.matchers ?? const <Matcher>[])
                  Card(
                    margin: const EdgeInsets.only(bottom: 8),
                    child: ListTile(
                      dense: true,
                      leading: const Icon(Icons.filter_alt_outlined),
                      title: Text(m.summary),
                      trailing: IconButton(
                        tooltip: 'Remove',
                        icon: const Icon(Icons.close),
                        onPressed: _busy ? null : () => _removeMatcher(m),
                      ),
                    ),
                  ),
              ],
              if ((_detail?.matchers ?? const []).isEmpty && !_loadingDetail)
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Text(
                    'Nothing is filed here automatically yet.',
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
                  ),
                ),
              OutlinedButton.icon(
                onPressed: _busy ? null : _addMatcher,
                icon: const Icon(Icons.add, size: 18),
                label: const Text('Add a rule'),
              ),

              if ((_detail?.recent ?? const []).isNotEmpty) ...[
                const SizedBox(height: 28),
                _SectionHeader(
                  title: 'Recent activity',
                  subtitle: 'What has actually moved in and out, which is '
                      'the real test of whether the rules above are right.',
                ),
                for (final a in _detail!.recent.take(6))
                  ListTile(
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                    title: Text((a['merchant'] as String?) ??
                        _sourceLabel(a['source'] as String?)),
                    subtitle: Text((a['note'] as String?) ??
                        _sourceLabel(a['source'] as String?)),
                    trailing: Text(
                      Money.formatSigned(a['amount_cents'] as int? ?? 0),
                      style: theme.textTheme.bodyMedium?.copyWith(
                        color: (a['amount_cents'] as int? ?? 0) < 0
                            ? theme.colorScheme.error
                            : null,
                      ),
                    ),
                  ),
              ],
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

          ],
        ),
      ),
    );
  }
}


/// Turn a ledger `source` into something readable.
String _sourceLabel(String? source) => switch (source) {
      'paycheck_split' => 'From a paycheck',
      'auto_categorise' => 'Filed automatically',
      'manual' => 'Filed by hand',
      'opening_balance' => 'Opening balance',
      'reconcile' => 'Reconciliation',
      null => '—',
      _ => source.replaceAll('_', ' '),
    };

class _SectionHeader extends StatelessWidget {
  final String title;
  final String subtitle;
  const _SectionHeader({required this.title, required this.subtitle});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(title,
              style: theme.textTheme.titleMedium
                  ?.copyWith(fontWeight: FontWeight.w600)),
          const SizedBox(height: 2),
          Text(subtitle,
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
        ],
      ),
    );
  }
}

/// Build one matching rule.
///
/// Only the two conditions worth hand-writing are offered. The engine
/// supports regex, Plaid categories, accounts and amount ranges too, but
/// those are better suggested by the assistant than typed into a phone.
class _MatcherDialog extends StatefulWidget {
  const _MatcherDialog();

  @override
  State<_MatcherDialog> createState() => _MatcherDialogState();
}

class _MatcherDialogState extends State<_MatcherDialog> {
  final _contains = TextEditingController();
  String _direction = 'outflow';

  @override
  void dispose() {
    _contains.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('File it here when…'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          TextField(
            controller: _contains,
            autofocus: true,
            onChanged: (_) => setState(() {}),
            decoration: const InputDecoration(
              labelText: 'The name contains',
              hintText: 'kroger',
              helperText: 'One word works best. Case does not matter.',
              helperMaxLines: 2,
            ),
          ),
          const SizedBox(height: 12),
          DropdownButtonFormField<String>(
            initialValue: _direction,
            decoration: const InputDecoration(labelText: 'And it is'),
            items: const [
              DropdownMenuItem(value: 'outflow', child: Text('Money going out')),
              DropdownMenuItem(value: 'inflow', child: Text('Money coming in')),
            ],
            onChanged: (v) => setState(() => _direction = v ?? _direction),
          ),
        ],
      ),
      actions: [
        TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Cancel')),
        FilledButton(
          // An empty matcher would claim every transaction, so there is
          // nothing to save until there is something to match on.
          onPressed: _contains.text.trim().isEmpty
              ? null
              : () => Navigator.pop(context, {
                    'merchant_contains': _contains.text.trim().toLowerCase(),
                    'direction': _direction,
                  }),
          child: const Text('Add'),
        ),
      ],
    );
  }
}

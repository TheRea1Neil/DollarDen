import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../core/theme.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';
import '../widgets/funding_editor.dart';

/// Record a paycheck — but show exactly what it will do first.
///
/// Splitting a paycheck moves real money across every envelope at once
/// and is the least reversible-feeling thing the app does. The preview
/// writes nothing, so there is no reason to make anyone guess.
class PaycheckScreen extends ConsumerStatefulWidget {
  /// Prefill from a deposit the app spotted, so reviewing a real
  /// paycheck is one tap rather than retyping the amount.
  final int? initialCents;
  final String? sourceLabel;

  const PaycheckScreen({super.key, this.initialCents, this.sourceLabel});

  @override
  ConsumerState<PaycheckScreen> createState() => _PaycheckScreenState();
}

class _PaycheckScreenState extends ConsumerState<PaycheckScreen> {
  final _amount = TextEditingController();
  bool _busy = false;
  String? _error;
  PaycheckPreview? _preview;

  /// The order and rules currently being tried. Null means "whatever is
  /// saved"; non-null means the user has dragged or edited something and
  /// the numbers on screen describe a plan that is not saved yet.
  List<PreviewLine>? _draft;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    if (widget.initialCents != null) {
      _amount.text = (widget.initialCents! / 100).toStringAsFixed(2);
      WidgetsBinding.instance.addPostFrameCallback((_) => _previewIt());
    }
  }

  @override
  void dispose() {
    _amount.dispose();
    super.dispose();
  }

  int? get _cents => Money.parse(_amount.text);

  Future<void> _previewIt({List<PreviewLine>? using}) async {
    final cents = _cents;
    if (cents == null || cents <= 0) return;
    setState(() {
      _busy = true;
      _error = null;
      if (using == null) _preview = null;
    });
    try {
      final rules = using == null
          ? null
          : [
              for (var i = 0; i < using.length; i++)
                using[i].toRuleJson(i + 1)
            ];
      final p = await ref
          .read(apiProvider)
          .previewPaycheck(cents, rules: rules);
      if (mounted) {
        setState(() {
          _preview = p;
          // Keep the draft in step with what came back, so a later drag
          // builds on the recalculated rules rather than stale ones.
          if (using != null) _draft = p.lines;
        });
      }
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _reorder(int from, int to) {
    final lines = [...(_draft ?? _preview!.lines)];
    if (to > from) to -= 1;
    lines.insert(to, lines.removeAt(from));
    setState(() => _draft = lines);
    _previewIt(using: lines);
  }

  Future<void> _editRule(PreviewLine line) async {
    final result = await showModalBottomSheet<(String?, Map<String, dynamic>)>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (ctx) => _RuleSheet(line: line),
    );
    if (result == null) return;
    final (type, params) = result;
    if (type == null) return;   // "remove funding" is not meaningful here
    final lines = [
      for (final l in (_draft ?? _preview!.lines))
        l.categoryId == line.categoryId ? l.copyWithRule(type, params) : l
    ];
    setState(() => _draft = lines);
    await _previewIt(using: lines);
  }

  /// Commit the dragged order and edited rules as a new plan version.
  Future<void> _saveChanges() async {
    final p = _preview;
    final lines = _draft;
    if (p == null || lines == null) return;
    setState(() => _saving = true);
    try {
      await ref.read(apiProvider).revisePlan(
            p.planId,
            'Standard',
            [for (var i = 0; i < lines.length; i++) lines[i].toRuleJson(i + 1)],
          );
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      if (!mounted) return;
      setState(() {
        _saving = false;
        _draft = null;
      });
      await _previewIt();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Saved. These rules apply from now on.')),
      );
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _saving = false;
        _error = e.message;
      });
    }
  }

  Future<void> _apply() async {
    final p = _preview;
    if (p == null) return;
    setState(() => _busy = true);
    try {
      final out = await ref.read(apiProvider).recordPaycheck(p.grossCents);
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      if (!mounted) return;
      final id = out['id'] as String?;
      Navigator.of(context).pop(true);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('${Money.format(p.grossCents)} split across '
            '${p.lines.where((l) => l.allocatedCents != 0).length} categories'),
        action: id == null
            ? null
            // Undo matters here: this touched every envelope at once.
            : SnackBarAction(
                label: 'Undo',
                onPressed: () async {
                  try {
                    await ref.read(apiProvider).unapplyPaycheck(id);
                    ref.invalidate(summaryProvider);
                    ref.invalidate(categoriesProvider);
                  } catch (_) {}
                },
              ),
        duration: const Duration(seconds: 8),
      ));
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
    final p = _preview;

    return Scaffold(
      appBar: AppBar(title: const Text('Record a paycheck')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (widget.sourceLabel != null) ...[
            Card(
              child: ListTile(
                leading: const Icon(Icons.account_balance),
                title: Text(widget.sourceLabel!),
                subtitle: const Text('Detected deposit'),
              ),
            ),
            const SizedBox(height: 12),
          ],
          TextField(
            controller: _amount,
            autofocus: true,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            onChanged: (_) => setState(() => _preview = null),
            onSubmitted: (_) => _previewIt(),
            decoration: const InputDecoration(
              labelText: 'Amount',
              prefixText: '\$',
              hintText: '2,000.00',
              helperText: 'Gross — what the split rules are calculated from.',
            ),
          ),
          const SizedBox(height: 16),
          FilledButton.icon(
            onPressed: _busy || _cents == null ? null : _previewIt,
            icon: _busy
                ? const SizedBox(
                    height: 18, width: 18,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.visibility_outlined),
            label: const Text('Show me what it does'),
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
          if (p != null) ...[
            const SizedBox(height: 24),
            for (final w in p.warnings)
              Container(
                width: double.infinity,
                margin: const EdgeInsets.only(bottom: 12),
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: theme.colorScheme.errorContainer,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Icon(Icons.warning_amber_rounded,
                      size: 18, color: theme.colorScheme.onErrorContainer),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(w,
                        style: TextStyle(
                            color: theme.colorScheme.onErrorContainer)),
                  ),
                ]),
              ),
            if (!p.isSavedPlan)
              Container(
                width: double.infinity,
                margin: const EdgeInsets.only(bottom: 12),
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: theme.colorScheme.tertiaryContainer,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Row(children: [
                  Icon(Icons.science_outlined,
                      size: 18, color: theme.colorScheme.onTertiaryContainer),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      'Trying something out. Your saved plan has not '
                      'changed.',
                      style: TextStyle(
                          color: theme.colorScheme.onTertiaryContainer),
                    ),
                  ),
                ]),
              ),
            Text('Drag to change who gets paid first. Tap a row to change '
                'its rule.',
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            const SizedBox(height: 8),
            Card(
              child: ReorderableListView.builder(
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                buildDefaultDragHandles: false,
                itemCount: (_draft ?? p.lines).length,
                onReorder: _busy ? (_, __) {} : _reorder,
                itemBuilder: (context, i) {
                  final line = (_draft ?? p.lines)[i];
                  return _PreviewRow(
                    key: ValueKey(line.categoryId),
                    index: i,
                    line: line,
                    onTap: _busy ? null : () => _editRule(line),
                  );
                },
              ),
            ),
            if (_draft != null) ...[
              const SizedBox(height: 12),
              Row(children: [
                Expanded(
                  child: OutlinedButton(
                    onPressed: _saving || _busy
                        ? null
                        : () {
                            setState(() => _draft = null);
                            _previewIt();
                          },
                    child: const Text('Discard changes'),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: FilledButton.tonal(
                    onPressed: _saving || _busy ? null : _saveChanges,
                    child: _saving
                        ? const SizedBox(
                            height: 16, width: 16,
                            child: CircularProgressIndicator(strokeWidth: 2))
                        : const Text('Save these rules'),
                  ),
                ),
              ]),
            ],
            const SizedBox(height: 12),
            Row(children: [
              Text('Allocated',
                  style: theme.textTheme.titleSmall
                      ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
              const Spacer(),
              Text(Money.format(p.totalAllocatedCents),
                  style: theme.textTheme.titleMedium
                      ?.copyWith(fontWeight: FontWeight.w700)),
            ]),
            if (p.leftoverCents != 0)
              Row(children: [
                Text('Not allocated',
                    style: theme.textTheme.titleSmall
                        ?.copyWith(color: theme.colorScheme.error)),
                const Spacer(),
                Text(Money.format(p.leftoverCents),
                    style: theme.textTheme.titleMedium?.copyWith(
                        fontWeight: FontWeight.w700,
                        color: theme.colorScheme.error)),
              ]),
            const SizedBox(height: 20),
            FilledButton(
              onPressed: _busy ? null : _apply,
              child: Text('Apply ${Money.format(p.grossCents)}'),
            ),
            const SizedBox(height: 4),
            Text('Nothing has changed yet. You can undo this afterwards.',
                textAlign: TextAlign.center,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          ],
        ],
      ),
    );
  }
}

class _PreviewRow extends StatelessWidget {
  final PreviewLine line;
  final int index;
  final VoidCallback? onTap;

  const _PreviewRow({
    super.key,
    required this.line,
    required this.index,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final nothing = line.gotNothing;
    return InkWell(
      onTap: onTap,
      child: Padding(
      padding: const EdgeInsets.fromLTRB(8, 12, 16, 12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ReorderableDragStartListener(
            index: index,
            child: Padding(
              padding: const EdgeInsets.only(right: 4, top: 2),
              child: Icon(Icons.drag_indicator,
                  size: 18, color: theme.colorScheme.onSurfaceVariant),
            ),
          ),
          SizedBox(
            width: 18,
            child: Text('${index + 1}',
                style: theme.textTheme.labelSmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          ),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(line.name,
                    style: theme.textTheme.bodyLarge?.copyWith(
                      fontWeight: FontWeight.w500,
                      color: nothing ? theme.colorScheme.onSurfaceVariant : null,
                    )),
                const SizedBox(height: 2),
                Text(line.ruleSummary,
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                if (line.shortCents > 0)
                  Text('${Money.format(line.shortCents)} short of what it asked for',
                      style: theme.textTheme.bodySmall
                          ?.copyWith(color: theme.colorScheme.error)),
              ],
            ),
          ),
          const SizedBox(width: 8),
          Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Text(
                nothing ? '—' : '+${Money.format(line.allocatedCents)}',
                style: theme.textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w700,
                  color: nothing
                      ? theme.colorScheme.onSurfaceVariant
                      : AppTheme.positive(context),
                ),
              ),
              Text('${Money.format(line.balanceCents)} → '
                  '${Money.format(line.afterCents)}',
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            ],
          ),
          const SizedBox(width: 4),
          Icon(Icons.edit_outlined,
              size: 16, color: theme.colorScheme.onSurfaceVariant),
        ],
      ),
      ),
    );
  }
}

/// Change one category's rule from inside the paycheck review.
///
/// Reuses the same editor as the category screen, so "refill to $150"
/// versus "$150 every paycheck" is explained identically wherever it is
/// changed.
class _RuleSheet extends StatefulWidget {
  final PreviewLine line;
  const _RuleSheet({required this.line});

  @override
  State<_RuleSheet> createState() => _RuleSheetState();
}

class _RuleSheetState extends State<_RuleSheet> {
  String? _type;
  Map<String, dynamic> _params = const {};

  @override
  void initState() {
    super.initState();
    _type = widget.line.ruleType;
    _params = widget.line.params;
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: EdgeInsets.fromLTRB(
          16, 0, 16, MediaQuery.of(context).viewInsets.bottom + 24),
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(widget.line.name,
                style: theme.textTheme.titleLarge
                    ?.copyWith(fontWeight: FontWeight.w700)),
            const SizedBox(height: 4),
            Text('Currently holds ${Money.format(widget.line.balanceCents)}',
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            const SizedBox(height: 16),
            FundingEditor(
              initial: Funding(
                planId: '',
                planVersion: 0,
                ruleType: widget.line.ruleType,
                params: widget.line.params,
                enabled: true,
                position: widget.line.orderIndex,
                ofTotal: widget.line.orderIndex,
                summary: widget.line.ruleSummary,
              ),
              // Position is dragged in the list behind this sheet, so the
              // slider would be a second, conflicting control.
              categoryCount: 1,
              onChanged: (type, params, _) {
                _type = type;
                _params = params;
              },
            ),
            const SizedBox(height: 20),
            Row(children: [
              Expanded(
                child: OutlinedButton(
                  onPressed: () => Navigator.pop(context),
                  child: const Text('Cancel'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: FilledButton(
                  onPressed: () => Navigator.pop(context, (_type, _params)),
                  child: const Text('Try it'),
                ),
              ),
            ]),
          ],
        ),
      ),
    );
  }
}

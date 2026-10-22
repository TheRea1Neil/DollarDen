import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../core/split_engine.dart' as engine;
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

  /// The deposit this paycheck came from. Without it the deposit is
  /// never marked as recorded and keeps being offered for review.
  final String? transactionId;

  const PaycheckScreen({
    super.key,
    this.initialCents,
    this.sourceLabel,
    this.transactionId,
  });

  @override
  ConsumerState<PaycheckScreen> createState() => _PaycheckScreenState();
}

class _PaycheckScreenState extends ConsumerState<PaycheckScreen> {
  final _amount = TextEditingController();
  bool _busy = false;
  String? _error;
  /// The saved plan's rules and balances, fetched once. Rule EDITS are
  /// permanent, so this is refreshed from the server when one is made.
  List<PreviewLine>? _baseLines;
  String? _planId;

  /// The locally computed split. Everything on screen comes from here.
  PaycheckPreview? _computed;

  /// The order the user has dragged things into, by category id. The
  /// only one-off thing: dragging is exploration and never touches the
  /// saved plan.
  List<String>? _order;

  /// Money set aside before the rules run, for this paycheck only.
  final Map<String, (String name, int cents)> _manual = {};

  /// The rules in the order they will be funded.
  List<PreviewLine> get _effective {
    final base = _baseLines ?? const <PreviewLine>[];
    final order = _order;
    if (order == null) return base;
    final byId = {for (final l in base) l.categoryId: l};
    final out = [
      for (final id in order)
        if (byId.containsKey(id)) byId[id]!
    ];
    // Anything the plan gained since the drag keeps a place rather than
    // vanishing.
    out.addAll(base.where((l) => !order.contains(l.categoryId)));
    return out;
  }

  @override
  void initState() {
    super.initState();
    if (widget.initialCents != null) {
      _amount.text = (widget.initialCents! / 100).toStringAsFixed(2);
      WidgetsBinding.instance.addPostFrameCallback((_) => _loadPlan());
    }
  }

  @override
  void dispose() {
    _amount.dispose();
    super.dispose();
  }

  int? get _cents => Money.parse(_amount.text);

  /// Fetch the saved plan's rules and balances. One call, on open and
  /// after a permanent rule edit — never on a drag.
  Future<void> _loadPlan() async {
    final cents = _cents;
    if (cents == null || cents <= 0) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final base = await ref.read(apiProvider).previewPaycheck(cents);
      if (!mounted) return;
      _baseLines = base.lines.where((l) => !l.isManualOnly).toList();
      _planId = base.planId;
      _recompute();
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// Recalculate entirely on the device.
  ///
  /// Dragging has to feel immediate, and a round trip per drag also let
  /// the list on screen and the rules the next request assumed drift
  /// apart. `core/split_engine.dart` is a port of the server's engine
  /// held to it by golden tests, so this is the same arithmetic the
  /// server will do when the paycheck is applied.
  void _recompute() {
    final gross = _cents;
    final lines = _effective;
    if (gross == null || lines.isEmpty) return;

    final manualTotal = _manual.values.fold(0, (sum, e) => sum + e.$2);
    if (manualTotal > gross) {
      setState(() {
        _error = 'Set-aside amounts (${Money.format(manualTotal)}) exceed '
            'the paycheck (${Money.format(gross)}).';
        _computed = null;
      });
      return;
    }

    // Set-asides land first and raise the balance the rules then see.
    final balances = <String, int>{
      for (final l in lines) l.categoryId: l.balanceCents,
    };
    for (final e in _manual.entries) {
      balances[e.key] = (balances[e.key] ?? 0) + e.value.$2;
    }

    final engineRules = [
      for (var i = 0; i < lines.length; i++)
        engine.SplitRule(
          categoryId: lines[i].categoryId,
          orderIndex: i + 1,
          ruleType: engine.splitRuleTypeFrom(lines[i].ruleType)!,
          params: lines[i].params,
        )
    ];

    try {
      final res = engine.applySplit(gross - manualTotal, engineRules, balances);
      final got = res.byCategory;
      final short = {for (final u in res.unfunded) u.$1: u.$2};

      final out = <PreviewLine>[];
      final starved = <String>[];
      for (var i = 0; i < lines.length; i++) {
        final l = lines[i];
        final byRule = got[l.categoryId] ?? 0;
        final byHand = _manual[l.categoryId]?.$2 ?? 0;
        if (byRule == 0 && byHand == 0) starved.add(l.name);
        out.add(PreviewLine(
          categoryId: l.categoryId, name: l.name, orderIndex: i + 1,
          ruleType: l.ruleType, ruleSummary: l.ruleSummary, params: l.params,
          balanceCents: l.balanceCents,
          allocatedCents: byRule, manualCents: byHand,
          afterCents: l.balanceCents + byRule + byHand,
          shortCents: short[l.categoryId] ?? 0,
        ));
      }
      // Set-asides into a category with no rule still have to be shown.
      for (final e in _manual.entries) {
        if (out.any((l) => l.categoryId == e.key)) continue;
        out.insert(0, PreviewLine(
          categoryId: e.key, name: e.value.$1, orderIndex: 0,
          ruleType: 'manual', ruleSummary: 'set aside by hand this time',
          params: const {}, balanceCents: 0,
          allocatedCents: 0, manualCents: e.value.$2,
          afterCents: e.value.$2, shortCents: 0,
        ));
      }

      setState(() {
        _error = null;
        _computed = PaycheckPreview(
          grossCents: gross,
          planId: _planId ?? '',
          planVersion: 0,
          lines: out,
          totalAllocatedCents: res.totalCents + manualTotal,
          manualTotalCents: manualTotal,
          rulesGrossCents: gross - manualTotal,
          leftoverCents: res.remainingCents,
          starved: starved,
          warnings: _warningsFor(out, starved, res.remainingCents),
          isSavedPlan: _order == null,
        );
      });
    } on engine.SplitPlanError catch (e) {
      setState(() {
        _error = e.message;
        _computed = null;
      });
    }
  }

  List<String> _warningsFor(
      List<PreviewLine> lines, List<String> starved, int leftover) {
    final out = <String>[];
    if (starved.isNotEmpty) {
      // Name the culprit, not just the victims.
      final funded = lines.where((l) => l.allocatedCents > 0).toList();
      final culprit = funded.isEmpty ? null : funded.last;
      out.add(
        '${starved.length} categor${starved.length == 1 ? "y" : "ies"} got '
        'nothing: ${starved.join(", ")}.'
        '${culprit == null ? "" : " ${culprit.name} is last to be funded and "
            "took ${Money.format(culprit.allocatedCents)}, leaving nothing "
            "for the ones below it."}',
      );
    }
    if (leftover > 0) {
      out.add('${Money.format(leftover)} was not allocated at all.');
    }
    return out;
  }

  void _reorder(int from, int to) {
    // Indices come from the list being displayed, which is exactly
    // `_effective`. Previously they were applied to a differently
    // filtered list, so after one drag the mapping was wrong and
    // nothing could be moved again.
    final ids = [for (final l in _effective) l.categoryId];
    if (to > from) to -= 1;
    ids.insert(to, ids.removeAt(from));
    setState(() => _order = ids);
    _recompute();
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

    // Nothing to do, and saying so beats a silent no-op that looks like
    // a failed save.
    if (type == line.ruleType && _sameParams(params, line.params)) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('No change to save'),
          behavior: SnackBarBehavior.floating,
        ));
      }
      return;
    }

    // A rule change is a decision, not an experiment: it is saved to the
    // plan straight away. Only the dragged ORDER is one-off. Position is
    // preserved, so saving a rule does not also move the category.
    setState(() => _busy = true);
    try {
      await ref.read(apiProvider).setFunding(
            line.categoryId,
            ruleType: type,
            params: params,
          );
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      if (!mounted) return;
      await _loadPlan();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('${line.name} saved — this applies from now on.'),
      ));
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// Compare only the keys that matter to the engine; a rule carrying
  /// an extra null is the same rule.
  static bool _sameParams(Map<String, dynamic> a, Map<String, dynamic> b) {
    String norm(Map<String, dynamic> m) {
      final clean = {
        for (final e in m.entries)
          if (e.value != null) e.key: e.value
      };
      final keys = clean.keys.toList()..sort();
      return [for (final k in keys) '$k=${clean[k]}'].join('&');
    }

    return norm(a) == norm(b);
  }

  List<Map<String, dynamic>> get _manualJson => [
        for (final e in _manual.entries)
          {'category_id': e.key, 'amount_cents': e.value.$2}
      ];

  Future<void> _addSetAside() async {
    final cats = await ref.read(categoriesProvider.future);
    if (!mounted) return;
    final picked = await showModalBottomSheet<(String, String, int)>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (ctx) => _SetAsideSheet(categories: cats),
    );
    if (picked == null) return;
    setState(() => _manual[picked.$1] = (picked.$2, picked.$3));
    _recompute();
  }

  Future<void> _removeSetAside(String categoryId) async {
    setState(() => _manual.remove(categoryId));
    _recompute();
  }

  Future<void> _apply() async {
    final p = _computed;
    if (p == null) return;
    setState(() => _busy = true);
    try {
      final lines = _order == null ? null : _effective;
      await ref.read(apiProvider).recordPaycheck(
            p.grossCents,
            transactionId: widget.transactionId,
            // Only the ORDER is one-off. Any rule change was already
            // saved to the plan when it was made.
            rules: lines == null
                ? null
                : [
                    for (var i = 0; i < lines.length; i++)
                      lines[i].toRuleJson(i + 1)
                  ],
            manual: _manualJson,
          );

      // Everything that could be stale now is. The prompt in particular:
      // leaving it would keep offering a paycheck that is already done.
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      ref.invalidate(unreviewedPaychecksProvider);
      if (!mounted) return;

      final messenger = ScaffoldMessenger.of(context);
      // Clear anything already queued, so confirmations cannot stack up
      // into one that never seems to leave.
      messenger.clearSnackBars();
      Navigator.of(context).pop(true);
      messenger.showSnackBar(SnackBar(
        content: Text('${Money.format(p.grossCents)} split across '
            '${p.lines.where((l) => !l.gotNothing).length} categories'),
        behavior: SnackBarBehavior.floating,
        duration: const Duration(seconds: 4),
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
    final p = _computed;

    return Scaffold(
      appBar: AppBar(title: const Text('Record a paycheck')),
      body: CustomScrollView(
        // A CustomScrollView with a SliverReorderableList is what makes
        // dragging auto-scroll. A ReorderableListView nested in a
        // ListView with NeverScrollableScrollPhysics cannot: there is no
        // scrollable for the drag to drive, so a category at the bottom
        // can never be dragged to the top.
        slivers: [
          SliverPadding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 0),
            sliver: SliverList(
              delegate: SliverChildListDelegate([
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
                  autofocus: widget.initialCents == null,
                  keyboardType:
                      const TextInputType.numberWithOptions(decimal: true),
                  onChanged: (_) => setState(() {
                    _computed = null;
                    _baseLines = null;
                  }),
                  onSubmitted: (_) => _loadPlan(),
                  decoration: const InputDecoration(
                    labelText: 'Amount',
                    prefixText: '\$',
                    hintText: '2,000.00',
                    helperText: 'Gross — what the split rules work from.',
                  ),
                ),
                const SizedBox(height: 16),
                FilledButton.icon(
                  onPressed: _busy || _cents == null ? null : () => _loadPlan(),
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
                        style: TextStyle(
                            color: theme.colorScheme.onErrorContainer)),
                  ),
                ],
                if (p != null) ...[
                  const SizedBox(height: 24),
                  // Set-asides come first on screen because they come
                  // first in the maths.
                  _SetAsideSection(
                    entries: _manual,
                    busy: _busy,
                    onAdd: _addSetAside,
                    onRemove: _removeSetAside,
                    rulesGrossCents: p.rulesGrossCents,
                    grossCents: p.grossCents,
                  ),
                  const SizedBox(height: 16),
                  for (final w in p.warnings)
                    Container(
                      width: double.infinity,
                      margin: const EdgeInsets.only(bottom: 12),
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
                              child: Text(w,
                                  style: TextStyle(
                                      color: theme
                                          .colorScheme.onErrorContainer)),
                            ),
                          ]),
                    ),
                  if (_order != null)
                    Container(
                      width: double.infinity,
                      margin: const EdgeInsets.only(bottom: 12),
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: theme.colorScheme.tertiaryContainer,
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: Row(children: [
                        Icon(Icons.history_toggle_off,
                            size: 18,
                            color: theme.colorScheme.onTertiaryContainer),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            'This order is just for this paycheck. Your '
                            'saved plan keeps its own.',
                            style: TextStyle(
                                color:
                                    theme.colorScheme.onTertiaryContainer),
                          ),
                        ),
                      ]),
                    ),
                  Text(
                      'Drag to try a different order — just for this '
                      'paycheck. Tap a row to change its rule, which '
                      'saves for good.',
                      style: theme.textTheme.bodySmall?.copyWith(
                          color: theme.colorScheme.onSurfaceVariant)),
                  const SizedBox(height: 8),
                ],
              ]),
            ),
          ),
          if (p != null)
            SliverPadding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              sliver: SliverReorderableList(
                itemCount: p.lines.length,
                onReorder: _reorder,
                itemBuilder: (context, i) {
                  final line = p.lines[i];
                  return Material(
                    key: ValueKey(line.categoryId),
                    color: theme.colorScheme.surfaceContainerLow,
                    child: Column(children: [
                      if (i > 0) const Divider(height: 1),
                      _PreviewRow(
                        index: i,
                        line: line,
                        onTap: _busy || line.isManualOnly
                            ? null
                            : () => _editRule(line),
                      ),
                    ]),
                  );
                },
              ),
            ),
          if (p != null)
            SliverPadding(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 32),
              sliver: SliverList(
                delegate: SliverChildListDelegate([
                  if (_order != null) ...[
                    OutlinedButton(
                      onPressed: _busy
                          ? null
                          : () {
                              setState(() => _order = null);
                              _recompute();
                            },
                      child: const Text("Back to my plan's order"),
                    ),
                    const SizedBox(height: 12),
                  ],
                  Row(children: [
                    Text('Allocated',
                        style: theme.textTheme.titleSmall?.copyWith(
                            color: theme.colorScheme.onSurfaceVariant)),
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
                  Text(
                      'Nothing has changed yet. You can undo this '
                      'afterwards.',
                      textAlign: TextAlign.center,
                      style: theme.textTheme.bodySmall?.copyWith(
                          color: theme.colorScheme.onSurfaceVariant)),
                ]),
              ),
            ),
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
                nothing
                    ? '—'
                    : '+${Money.format(line.allocatedCents + line.manualCents)}',
                style: theme.textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w700,
                  color: nothing
                      ? theme.colorScheme.onSurfaceVariant
                      : AppTheme.positive(context),
                ),
              ),
              if (line.manualCents > 0 && line.allocatedCents > 0)
                Text('${Money.format(line.manualCents)} by hand',
                    style: theme.textTheme.labelSmall
                        ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
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

  /// False until every field the chosen rule type needs has a value.
  /// Saving an incomplete rule used to write the old one back, which
  /// read as "I had to do it twice".
  bool _complete = false;

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
            const SizedBox(height: 12),
            // Said here, at the moment of the decision, because it is the
            // opposite of dragging: the order is a what-if, this is not.
            Container(
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: theme.colorScheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Row(children: [
                Icon(Icons.push_pin_outlined,
                    size: 16, color: theme.colorScheme.onSurfaceVariant),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'Saves to this category for good — every paycheck '
                    'from now on, not just this one.',
                    style: theme.textTheme.bodySmall,
                  ),
                ),
              ]),
            ),
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
              onChanged: (type, params, _, complete) {
                // setState so the Save button enables the moment the
                // rule becomes usable.
                setState(() {
                  _type = type;
                  _params = params;
                  _complete = complete;
                });
              },
            ),
            if (!_complete) ...[
              const SizedBox(height: 8),
              Text('Fill in the amount to save this rule.',
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.error)),
            ],
            if (!_complete) ...[
              const SizedBox(height: 8),
              Text('Fill in every amount this rule needs before saving.',
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.error)),
            ],
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
                  onPressed: _complete
                      ? () => Navigator.pop(context, (_type, _params))
                      : null,
                  child: const Text('Save rule'),
                ),
              ),
            ]),
          ],
        ),
      ),
    );
  }
}


/// Money taken off the top, before any rule runs.
///
/// The ordering is the whole point: a refund you do not tithe on should
/// not be counted as income by a percentage rule. Setting it aside first
/// shrinks what the rules divide.
class _SetAsideSection extends StatelessWidget {
  final Map<String, (String, int)> entries;
  final bool busy;
  final VoidCallback onAdd;
  final void Function(String categoryId) onRemove;
  final int rulesGrossCents;
  final int grossCents;

  const _SetAsideSection({
    required this.entries,
    required this.busy,
    required this.onAdd,
    required this.onRemove,
    required this.rulesGrossCents,
    required this.grossCents,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(children: [
          Text('Set aside first',
              style: theme.textTheme.titleSmall
                  ?.copyWith(fontWeight: FontWeight.w600)),
          const Spacer(),
          TextButton.icon(
            onPressed: busy ? null : onAdd,
            icon: const Icon(Icons.add, size: 18),
            label: const Text('Add'),
          ),
        ]),
        if (entries.isEmpty)
          Text(
            'Money that skips the rules entirely — a refund you do not '
            'tithe on, say. The rules then divide what is left.',
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
          )
        else ...[
          for (final e in entries.entries)
            Card(
              margin: const EdgeInsets.only(bottom: 4),
              child: ListTile(
                dense: true,
                leading: const Icon(Icons.push_pin_outlined, size: 18),
                title: Text(e.value.$1),
                trailing: Row(mainAxisSize: MainAxisSize.min, children: [
                  Text(Money.format(e.value.$2),
                      style: theme.textTheme.titleSmall
                          ?.copyWith(fontWeight: FontWeight.w700)),
                  IconButton(
                    icon: const Icon(Icons.close, size: 18),
                    onPressed: busy ? null : () => onRemove(e.key),
                  ),
                ]),
              ),
            ),
          const SizedBox(height: 4),
          Text(
            'Rules divide the remaining ${Money.format(rulesGrossCents)}, '
            'not the full ${Money.format(grossCents)}.',
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
          ),
        ],
      ],
    );
  }
}

/// Pick a category and an amount to set aside.
class _SetAsideSheet extends StatefulWidget {
  final List<Category> categories;
  const _SetAsideSheet({required this.categories});

  @override
  State<_SetAsideSheet> createState() => _SetAsideSheetState();
}

class _SetAsideSheetState extends State<_SetAsideSheet> {
  final _amount = TextEditingController();
  Category? _chosen;

  @override
  void dispose() {
    _amount.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final cents = Money.parse(_amount.text);
    return Padding(
      padding: EdgeInsets.fromLTRB(
          16, 0, 16, MediaQuery.of(context).viewInsets.bottom + 24),
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Set aside first',
                style: theme.textTheme.titleLarge
                    ?.copyWith(fontWeight: FontWeight.w700)),
            const SizedBox(height: 4),
            Text('This goes straight into the category before any rule runs.',
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            const SizedBox(height: 16),
            DropdownButtonFormField<Category>(
              initialValue: _chosen,
              isExpanded: true,
              decoration: const InputDecoration(labelText: 'Into'),
              items: [
                for (final c in widget.categories)
                  DropdownMenuItem(value: c, child: Text(c.name)),
              ],
              onChanged: (v) => setState(() => _chosen = v),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _amount,
              autofocus: true,
              keyboardType:
                  const TextInputType.numberWithOptions(decimal: true),
              onChanged: (_) => setState(() {}),
              decoration: const InputDecoration(
                  labelText: 'Amount', prefixText: '\$'),
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
                  onPressed: _chosen == null || cents == null || cents <= 0
                      ? null
                      : () => Navigator.pop(
                          context, (_chosen!.id, _chosen!.name, cents)),
                  child: const Text('Set aside'),
                ),
              ),
            ]),
          ],
        ),
      ),
    );
  }
}

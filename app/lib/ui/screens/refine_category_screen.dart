import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../data/api_client.dart';
import '../widgets/ai_waiting.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';

/// Ask the assistant to change one existing category.
///
/// Unlike designing a new one, this starts from what is already there and
/// has to preserve what the user did not mention. The proposal is shown as
/// a before/after so a change you did not ask for is visible rather than
/// buried in a form.
class RefineCategoryScreen extends ConsumerStatefulWidget {
  final CategoryDetail detail;
  const RefineCategoryScreen({super.key, required this.detail});

  @override
  ConsumerState<RefineCategoryScreen> createState() =>
      _RefineCategoryScreenState();
}

class _RefineCategoryScreenState extends ConsumerState<RefineCategoryScreen> {
  final _request = TextEditingController();

  bool _busy = false;
  String _progress = '';
  String? _error;

  /// Queued behind a busy graphics card. Not a failure, and
  /// must not be shown as one.
  JobDeferred? _deferred;
  RefinedCategory? _draft;

  Category get _c => widget.detail.category;

  @override
  void dispose() {
    _request.dispose();
    super.dispose();
  }

  Future<void> _ask() async {
    final text = _request.text.trim();
    if (text.isEmpty) return;
    final api = ref.read(apiProvider);
    setState(() {
      _busy = true;
      _error = null;
      _deferred = null;
      _draft = null;
      _progress = 'Queued…';
    });
    try {
      final job = await api.queueJob('refine_category', {
        'category_id': _c.id,
        'request': text,
        // Send the rendered summaries so the model reasons about the rule
        // the same way the user just read it on the previous screen.
        'funding_summary': widget.detail.funding?.summary,
        'matcher_summaries':
            [for (final m in widget.detail.matchers) m.summary],
      });
      final done = await api.awaitJob(job.id, onProgress: (j) {
        if (mounted) setState(() => _progress = j.progressLabel);
      });
      if (!mounted) return;
      if (!done.ok) {
        setState(() => _error = done.error ?? 'The assistant could not finish.');
        return;
      }
      setState(() => _draft = RefinedCategory.fromJson(done.result ?? {}));
    } on JobDeferred catch (d) {
      if (mounted) setState(() => _deferred = d);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _apply() async {
    final d = _draft;
    if (d == null) return;
    setState(() => _busy = true);
    final api = ref.read(apiProvider);
    try {
      await api.updateCategory(_c.id, {
        'name': d.name,
        'kind': d.kind,
        'description': d.description.isEmpty ? null : d.description,
        'target_cents': d.targetCents,
        'alert_at_cents': d.alertAtCents,
      });
      // Only touch the split plan if the funding actually differs —
      // every funding change costs a plan version, and a no-op revision
      // makes the history harder to read for nothing.
      final current = widget.detail.funding?.ruleType;
      if (d.apiRuleType != current || d.apiParams != null) {
        await api.setFunding(_c.id,
            ruleType: d.apiRuleType, params: d.apiParams ?? const {});
      }
      for (final m in d.addMatchers) {
        await api.addMatcher(_c.id, {
          'merchant_contains':
              (m['merchant_contains'] as String).trim().toLowerCase(),
          if (m['direction'] != null) 'direction': m['direction'],
        });
      }
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
    return Scaffold(
      appBar: AppBar(title: Text('Change ${_c.name}')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('As it stands',
                      style: theme.textTheme.labelLarge?.copyWith(
                          color: theme.colorScheme.onSurfaceVariant)),
                  const SizedBox(height: 6),
                  Text(_c.description?.isNotEmpty == true
                      ? _c.description!
                      : 'No description written down.'),
                  const SizedBox(height: 6),
                  Text(widget.detail.funding?.summary ??
                      'Not funded automatically.'),
                  if (_c.targetCents != null)
                    Text('Full at ${Money.format(_c.targetCents!)}.'),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _request,
            maxLines: 4,
            minLines: 3,
            autofocus: true,
            textCapitalization: TextCapitalization.sentences,
            decoration: const InputDecoration(
              labelText: 'What should change?',
              hintText: 'Only put money in while it is under \$450, and cap '
                  'it at \$50 a paycheck. Also eating out should come out of '
                  'Fun, not here.',
            ),
          ),
          const SizedBox(height: 16),
          FilledButton.icon(
            onPressed: _busy ? null : _ask,
            icon: _busy
                ? const SizedBox(
                    height: 18, width: 18,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.auto_awesome),
            label: Text(_busy ? _progress : 'Suggest the change'),
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
          if (_deferred != null) ...[
            const SizedBox(height: 16),
            AiWaiting(deferred: _deferred!, inline: true),
          ],
          if (_draft != null) ...[
            const SizedBox(height: 24),
            _DraftView(
              before: widget.detail,
              draft: _draft!,
              busy: _busy,
              onApply: _apply,
            ),
          ],
        ],
      ),
    );
  }
}

class _DraftView extends StatelessWidget {
  final CategoryDetail before;
  final RefinedCategory draft;
  final bool busy;
  final VoidCallback onApply;

  const _DraftView({
    required this.before,
    required this.draft,
    required this.busy,
    required this.onApply,
  });

  /// Describe the proposed funding the same way the API describes the
  /// current one, so before and after are actually comparable.
  String get _fundingAfter {
    final f = draft.funding;
    final t = f['rule_type'];
    int? c(String k) => f[k] is int ? f[k] as int : null;
    switch (t) {
      case 'percent':
        final r = double.tryParse('${f['rate'] ?? ''}');
        final pct = r == null ? '${f['rate']}' : '${(r * 100).toStringAsFixed(0)}%';
        final of = f['of'] == 'remaining' ? 'what is left' : 'gross pay';
        return '$pct of $of, every paycheck';
      case 'fixed':
        final a = c('amount_cents');
        return a == null ? 'a fixed amount (not set)'
                         : '${Money.format(a)} every paycheck';
      case 'refill_to':
        final tc = c('target_cents');
        final cap = c('max_contribution_cents');
        final base = tc == null
            ? 'refilled to a target (not set)'
            : 'topped back up to ${Money.format(tc)} each paycheck';
        return cap == null
            ? base
            : '$base, but never more than ${Money.format(cap)} at once';
      case 'conditional':
        const words = {'<': 'less than', '<=': 'at most', '>': 'more than',
                       '>=': 'at least', '==': 'exactly'};
        final v = c('when_value_cents');
        final a = c('then_amount_cents');
        final op = words[f['when_op']] ?? '${f['when_op']}';
        return 'only while this holds $op '
            '${v == null ? '(not set)' : Money.format(v)}: '
            '${a == null ? '(not set)' : Money.format(a)} per paycheck';
      default:
        return 'not funded automatically';
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final rows = <(String, String, String)>[
      ('Name', before.category.name, draft.name),
      ('Kind', before.category.kind, draft.kind),
      ('For', before.category.description ?? '—',
          draft.description.isEmpty ? '—' : draft.description),
      (
        'Full at',
        before.category.targetCents == null
            ? '—' : Money.format(before.category.targetCents!),
        draft.targetCents == null ? '—' : Money.format(draft.targetCents!)
      ),
      (
        'Warn below',
        before.category.alertAtCents == null
            ? '—' : Money.format(before.category.alertAtCents!),
        draft.alertAtCents == null ? '—' : Money.format(draft.alertAtCents!)
      ),
      ('Funding', before.funding?.summary ?? 'not funded automatically',
          _fundingAfter),
    ];

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Proposed', style: theme.textTheme.titleMedium
                ?.copyWith(fontWeight: FontWeight.w600)),
            if (draft.whatChanged.isNotEmpty) ...[
              const SizedBox(height: 4),
              Text(draft.whatChanged,
                  style: theme.textTheme.bodyMedium
                      ?.copyWith(fontStyle: FontStyle.italic)),
            ],
            const SizedBox(height: 12),
            for (final (label, was, now) in rows)
              Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(label,
                        style: theme.textTheme.labelSmall?.copyWith(
                            color: theme.colorScheme.onSurfaceVariant)),
                    if (was == now)
                      Text(now, style: theme.textTheme.bodyMedium)
                    else ...[
                      Text(was,
                          style: theme.textTheme.bodySmall?.copyWith(
                            color: theme.colorScheme.onSurfaceVariant,
                            decoration: TextDecoration.lineThrough,
                          )),
                      Text(now,
                          style: theme.textTheme.bodyMedium
                              ?.copyWith(fontWeight: FontWeight.w600)),
                    ],
                  ],
                ),
              ),
            if (draft.addMatchers.isNotEmpty) ...[
              const SizedBox(height: 4),
              Text('Also files here automatically',
                  style: theme.textTheme.labelSmall?.copyWith(
                      color: theme.colorScheme.onSurfaceVariant)),
              for (final m in draft.addMatchers)
                Text('• names containing "${m['merchant_contains']}"',
                    style: theme.textTheme.bodyMedium),
            ],
            for (final n in draft.notices) ...[
              const SizedBox(height: 8),
              Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                const Icon(Icons.info_outline, size: 16),
                const SizedBox(width: 6),
                Expanded(child: Text(n, style: theme.textTheme.bodySmall)),
              ]),
            ],
            for (final p in draft.problems) ...[
              const SizedBox(height: 8),
              Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Icon(Icons.error_outline, size: 16,
                    color: theme.colorScheme.error),
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
              onPressed: busy || !draft.applicable ? null : onApply,
              child: const Text('Apply'),
            ),
            const SizedBox(height: 4),
            Text(
              'You can still fine-tune everything afterwards on the '
              'category screen.',
              textAlign: TextAlign.center,
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
          ],
        ),
      ),
    );
  }
}

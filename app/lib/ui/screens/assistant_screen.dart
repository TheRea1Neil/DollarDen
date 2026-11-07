import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../core/theme.dart';
import '../../data/api_client.dart';
import '../widgets/ai_waiting.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';

/// Describe what you are trying to achieve; get a budget you can edit.
///
/// The proposal is never applied automatically. It arrives as a draft the
/// user reviews, corrects and approves — the model proposes, the person
/// disposes (ADR 0003). That matters because the model does make mistakes
/// that no validator can catch, such as a 10x dollars/cents error, which
/// is obvious to a human reading the plan.
class AssistantScreen extends ConsumerStatefulWidget {
  const AssistantScreen({super.key});

  @override
  ConsumerState<AssistantScreen> createState() => _AssistantScreenState();
}

class _AssistantScreenState extends ConsumerState<AssistantScreen> {
  final _goals = TextEditingController();
  final _extra = TextEditingController();

  bool _loadingGoals = true;
  bool _saving = false;
  bool _busy = false;
  String _progress = '';
  Proposal? _proposal;
  String? _error;

  /// Queued behind a busy graphics card. Not a failure, and
  /// must not be shown as one.
  JobDeferred? _deferred;

  @override
  void initState() {
    super.initState();
    _loadGoals();
  }

  @override
  void dispose() {
    _goals.dispose();
    _extra.dispose();
    super.dispose();
  }

  Future<void> _loadGoals() async {
    try {
      _goals.text = await ref.read(apiProvider).getGoals();
    } catch (_) {
      // Not fatal: the user can still type goals in.
    } finally {
      if (mounted) setState(() => _loadingGoals = false);
    }
  }

  /// Goals used to persist only as a side effect of generating a whole
  /// budget. They are read by five AI tasks and this is the only screen
  /// that can edit them, so saving them has to stand on its own.
  Future<void> _save() async {
    setState(() => _saving = true);
    try {
      await ref.read(apiProvider).setGoals(_goals.text.trim());
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Goals saved')),
      );
    } on JobDeferred catch (d) {
      if (mounted) setState(() => _deferred = d);
    } on ApiException catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.message),
              backgroundColor: Theme.of(context).colorScheme.error),
        );
      }
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _generate() async {
    final api = ref.read(apiProvider);
    setState(() {
      _busy = true;
      _error = null;
      _deferred = null;
      _proposal = null;
      _progress = 'Queued…';
    });
    try {
      await api.setGoals(_goals.text.trim());
      final job = await api.queueJob('design_categories', {
        'instructions': _extra.text.trim(),
      });
      final done = await api.awaitJob(
        job.id,
        onProgress: (j) {
          if (mounted) setState(() => _progress = j.progressLabel);
        },
      );
      if (!mounted) return;
      if (!done.ok) {
        setState(() => _error = done.error ?? 'The assistant could not finish.');
        return;
      }
      setState(() => _proposal = Proposal.fromJson(done.result ?? {}));
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _apply() async {
    final p = _proposal;
    if (p == null) return;
    setState(() => _busy = true);
    try {
      final out = await ref.read(apiProvider).applyProposal(
            categories: p.categories,
            rules: p.rules,
          );
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      if (!mounted) return;
      final created = (out['created_categories'] as List).length;
      final reused = (out['reused_categories'] as List).length;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('$created created, $reused reused · plan v${out['plan_version']}'),
      ));
      setState(() => _proposal = null);
    } on ApiException catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.message),
              backgroundColor: Theme.of(context).colorScheme.error),
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Goals'),
        actions: [
          if (_saving)
            const Padding(
              padding: EdgeInsets.symmetric(horizontal: 20),
              child: SizedBox(
                height: 18, width: 18,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
            )
          else
            IconButton(
              tooltip: 'Save goals',
              icon: const Icon(Icons.check),
              onPressed: _loadingGoals || _busy ? null : _save,
            ),
        ],
      ),
      body: _loadingGoals
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(16),
              children: [
                Text('What are you trying to do with your money?',
                    style: theme.textTheme.titleMedium
                        ?.copyWith(fontWeight: FontWeight.w600)),
                const SizedBox(height: 4),
                Text(
                  'Written in your own words. This also shapes the spending '
                  'summaries you get later.',
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: _goals,
                  maxLines: 5,
                  minLines: 3,
                  textCapitalization: TextCapitalization.sentences,
                  decoration: const InputDecoration(
                    hintText: 'Save \$6,000 for emergencies, then for a newer '
                        'car. Eat out less. Tithing comes first.',
                  ),
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: _extra,
                  maxLines: 3,
                  minLines: 2,
                  textCapitalization: TextCapitalization.sentences,
                  decoration: const InputDecoration(
                    labelText: 'Fixed amounts it should know (optional)',
                    hintText: 'Paid every two weeks. Rent \$350. Health \$150.',
                  ),
                ),
                const SizedBox(height: 16),
                FilledButton.icon(
                  onPressed: _busy ? null : _generate,
                  icon: _busy
                      ? const SizedBox(
                          height: 18, width: 18,
                          child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.auto_awesome),
                  label: Text(_busy ? _progress : 'Suggest a budget'),
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
                if (_deferred != null) ...[
                  const SizedBox(height: 16),
                  AiWaiting(deferred: _deferred!, inline: true),
                ],
                if (_proposal != null) ...[
                  const SizedBox(height: 24),
                  _ProposalView(
                    proposal: _proposal!,
                    busy: _busy,
                    onApply: _apply,
                    onChanged: () => setState(() {}),
                  ),
                ],
              ],
            ),
    );
  }
}

class _ProposalView extends StatelessWidget {
  final Proposal proposal;
  final bool busy;
  final VoidCallback onApply;
  final VoidCallback onChanged;

  const _ProposalView({
    required this.proposal,
    required this.busy,
    required this.onApply,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Text('Suggested budget',
                style: theme.textTheme.titleMedium
                    ?.copyWith(fontWeight: FontWeight.w700)),
            const Spacer(),
            Text('tap to edit',
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          ],
        ),
        const SizedBox(height: 4),
        Text(
          'Check these before applying. The assistant gets amounts wrong '
          'sometimes — especially by a factor of ten.',
          style: theme.textTheme.bodySmall
              ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
        ),
        if (proposal.notes.isNotEmpty) ...[
          const SizedBox(height: 12),
          Text(proposal.notes, style: theme.textTheme.bodyMedium),
        ],

        if (proposal.problems.isNotEmpty) ...[
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: AppTheme.warning.withValues(alpha: 0.12),
              borderRadius: BorderRadius.circular(12),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Needs fixing before this can be applied',
                    style: theme.textTheme.labelLarge
                        ?.copyWith(color: AppTheme.warning)),
                const SizedBox(height: 6),
                for (final p in proposal.problems)
                  Text('• $p', style: theme.textTheme.bodySmall),
              ],
            ),
          ),
        ],

        if (proposal.warnings.isNotEmpty) ...[
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: AppTheme.danger.withValues(alpha: 0.10),
              borderRadius: BorderRadius.circular(12),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('This will not work the way you expect',
                    style: theme.textTheme.labelLarge
                        ?.copyWith(color: AppTheme.danger)),
                const SizedBox(height: 6),
                for (final w in proposal.warnings)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 4),
                    child: Text('• $w', style: theme.textTheme.bodySmall),
                  ),
              ],
            ),
          ),
        ],
        if (proposal.notices.isNotEmpty) ...[
          const SizedBox(height: 12),
          for (final n in proposal.notices.where((n) => !n.contains('already exists')))
            Text('· $n',
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
        ],
        const SizedBox(height: 16),
        Text('Categories', style: theme.textTheme.labelLarge),
        const SizedBox(height: 6),
        Card(
          child: Column(
            children: [
              for (var i = 0; i < proposal.categories.length; i++) ...[
                if (i > 0) const Divider(height: 1, indent: 16, endIndent: 16),
                _CategoryRow(
                  category: proposal.categories[i],
                  onChanged: onChanged,
                ),
              ],
            ],
          ),
        ),

        const SizedBox(height: 16),
        Text('How each paycheck gets split', style: theme.textTheme.labelLarge),
        Text('In order, top to bottom',
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
        const SizedBox(height: 6),
        Card(
          child: Column(
            children: [
              for (var i = 0; i < proposal.rules.length; i++) ...[
                if (i > 0) const Divider(height: 1, indent: 16, endIndent: 16),
                ListTile(
                  leading: CircleAvatar(
                    radius: 14,
                    backgroundColor: theme.colorScheme.surfaceContainerHighest,
                    child: Text('${i + 1}', style: theme.textTheme.labelSmall),
                  ),
                  title: Text(proposal.rules[i].categoryName,
                      style: const TextStyle(fontWeight: FontWeight.w600)),
                  subtitle: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(proposal.rules[i].describe()),
                      if (proposal.rules[i].why.isNotEmpty)
                        Text(proposal.rules[i].why,
                            style: theme.textTheme.bodySmall?.copyWith(
                                color: theme.colorScheme.onSurfaceVariant)),
                    ],
                  ),
                ),
              ],
            ],
          ),
        ),

        const SizedBox(height: 20),
        FilledButton(
          onPressed: (busy || !proposal.applicable) ? null : onApply,
          child: Text(proposal.applicable
              ? 'Apply this budget'
              : 'Fix the problems above first'),
        ),
        const SizedBox(height: 6),
        Text(
          'Nothing changes until you tap apply. You can edit everything '
          'afterwards too.',
          textAlign: TextAlign.center,
          style: theme.textTheme.bodySmall
              ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
        ),
      ],
    );
  }
}

class _CategoryRow extends StatelessWidget {
  final ProposedCategory category;
  final VoidCallback onChanged;

  const _CategoryRow({required this.category, required this.onChanged});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ListTile(
      title: Row(
        children: [
          Flexible(
            child: Text(category.name,
                style: const TextStyle(fontWeight: FontWeight.w600),
                overflow: TextOverflow.ellipsis),
          ),
          if (category.alreadyExists) ...[
            const SizedBox(width: 8),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
              decoration: BoxDecoration(
                color: theme.colorScheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(6),
              ),
              child: Text('existing', style: theme.textTheme.labelSmall),
            ),
          ],
        ],
      ),
      subtitle: Text(
        category.targetCents != null
            ? '${category.kind} · target ${Money.format(category.targetCents!)}'
            : category.kind,
      ),
      trailing: const Icon(Icons.edit_outlined, size: 18),
      onTap: () => _edit(context),
    );
  }

  Future<void> _edit(BuildContext context) async {
    final name = TextEditingController(text: category.name);
    final target = TextEditingController(
      text: category.targetCents != null
          ? (category.targetCents! / 100).toStringAsFixed(2)
          : '',
    );
    var kind = category.kind;

    final saved = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setLocal) => AlertDialog(
          title: const Text('Edit category'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: name,
                decoration: const InputDecoration(labelText: 'Name'),
              ),
              const SizedBox(height: 12),
              DropdownButtonFormField<String>(
                initialValue: kind,
                decoration: const InputDecoration(labelText: 'Type'),
                items: const [
                  DropdownMenuItem(value: 'spending', child: Text('Spending')),
                  DropdownMenuItem(
                      value: 'savings_bucket', child: Text('Savings bucket')),
                  DropdownMenuItem(value: 'obligation', child: Text('Obligation')),
                ],
                onChanged: (v) => setLocal(() => kind = v ?? kind),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: target,
                keyboardType: const TextInputType.numberWithOptions(decimal: true),
                decoration: const InputDecoration(
                    labelText: 'Refill target', prefixText: r'$'),
              ),
            ],
          ),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(ctx, false),
                child: const Text('Cancel')),
            FilledButton(
                onPressed: () => Navigator.pop(ctx, true),
                child: const Text('Save')),
          ],
        ),
      ),
    );

    if (saved == true) {
      category.name = name.text.trim();
      category.kind = kind;
      category.targetCents = Money.parse(target.text);
      onChanged();
    }
  }
}

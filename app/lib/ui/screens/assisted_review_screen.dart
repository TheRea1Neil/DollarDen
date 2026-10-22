import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../core/theme.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';

/// Walk the review backlog, one charge at a time, with a suggestion.
///
/// It SUGGESTS. Measured at 83% precision in
/// `experiments/categorise-arms`, with one wrong answer arriving at high
/// confidence — and a wrong auto-file is money in the wrong envelope
/// nobody notices, while a wrong suggestion costs one tap to reject.
///
/// Every suggestion shows what backed it: a verified quote from the
/// category's description, or the descriptions that name something in
/// the charge, or an explicit admission that it went on the merchant
/// name alone. That last case is the one worth seeing.
class AssistedReviewScreen extends ConsumerStatefulWidget {
  final List<ReviewItem> items;
  const AssistedReviewScreen({super.key, required this.items});

  @override
  ConsumerState<AssistedReviewScreen> createState() =>
      _AssistedReviewScreenState();
}

class _AssistedReviewScreenState extends ConsumerState<AssistedReviewScreen> {
  int _index = 0;
  CategorySuggestion? _suggestion;
  bool _busy = false;
  String? _error;
  int _filed = 0;
  int _skipped = 0;
  bool _remember = true;

  ReviewItem? get _item =>
      _index < widget.items.length ? widget.items[_index] : null;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _ask());
  }

  Future<void> _ask() async {
    final item = _item;
    if (item == null) return;
    setState(() {
      _busy = true;
      _error = null;
      _suggestion = null;
    });
    try {
      final s = await ref.read(apiProvider).suggestCategory(
            merchant: item.title,
            amountCents: item.amountCents,
          );
      if (mounted) setState(() => _suggestion = s);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _next() {
    setState(() {
      _index += 1;
      _suggestion = null;
    });
    if (_item != null) {
      _ask();
    } else {
      ref.invalidate(reviewProvider);
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
    }
  }

  Future<void> _accept(String categoryId) async {
    final item = _item;
    if (item == null) return;
    setState(() => _busy = true);
    try {
      await ref.read(apiProvider).resolveReview(
        item.id,
        [(categoryId: categoryId, amountCents: item.amountCents)],
        remember: _remember,
      );
      _filed += 1;
      _next();
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = e.message;
        });
      }
    }
  }

  Future<void> _chooseMyself() async {
    final cats = await ref.read(categoriesProvider.future);
    if (!mounted) return;
    final picked = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          children: [
            for (final c in cats)
              ListTile(
                title: Text(c.name),
                subtitle: Text(Money.format(c.balanceCents)),
                onTap: () => Navigator.pop(ctx, c.id),
              ),
          ],
        ),
      ),
    );
    if (picked != null) await _accept(picked);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final item = _item;

    if (item == null) {
      return Scaffold(
        appBar: AppBar(title: const Text('Assisted review')),
        body: Center(
          child: Padding(
            padding: const EdgeInsets.all(32),
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              Icon(Icons.task_alt, size: 48, color: AppTheme.healthy),
              const SizedBox(height: 16),
              Text('Done', style: theme.textTheme.headlineSmall),
              const SizedBox(height: 8),
              Text('$_filed filed, $_skipped left for later',
                  style: theme.textTheme.bodyMedium?.copyWith(
                      color: theme.colorScheme.onSurfaceVariant)),
              const SizedBox(height: 24),
              FilledButton(
                onPressed: () => Navigator.pop(context),
                child: const Text('Back to the budget'),
              ),
            ]),
          ),
        ),
      );
    }

    final s = _suggestion;
    return Scaffold(
      appBar: AppBar(
        title: Text('${_index + 1} of ${widget.items.length}'),
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(2),
          child: LinearProgressIndicator(
            value: _index / widget.items.length,
            minHeight: 2,
          ),
        ),
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(item.title,
                      style: theme.textTheme.headlineSmall
                          ?.copyWith(fontWeight: FontWeight.w700)),
                  const SizedBox(height: 4),
                  Text(item.date,
                      style: theme.textTheme.bodySmall?.copyWith(
                          color: theme.colorScheme.onSurfaceVariant)),
                  const SizedBox(height: 12),
                  _FlowAmountLarge(signedCents: item.amountCents),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),
          if (_busy && s == null)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 40),
              child: Center(child: CircularProgressIndicator()),
            ),
          if (_error != null) ...[
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
            const SizedBox(height: 12),
          ],
          if (s != null) _SuggestionCard(suggestion: s),
          const SizedBox(height: 16),
          if (s != null && !s.unsure && s.categoryId != null)
            FilledButton.icon(
              onPressed: _busy ? null : () => _accept(s.categoryId!),
              icon: const Icon(Icons.check),
              label: Text('File in ${s.categoryName}'),
            ),
          const SizedBox(height: 8),
          OutlinedButton(
            onPressed: _busy ? null : _chooseMyself,
            child: Text(s != null && !s.unsure
                ? 'Pick a different category'
                : 'Choose a category'),
          ),
          const SizedBox(height: 8),
          TextButton(
            onPressed: _busy
                ? null
                : () {
                    _skipped += 1;
                    _next();
                  },
            child: const Text('Skip for now'),
          ),
          const Divider(height: 32),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            value: _remember,
            onChanged: (v) => setState(() => _remember = v),
            title: const Text('Remember this merchant'),
            subtitle: const Text(
                'Files it automatically next time, without asking the '
                'assistant again.'),
          ),
        ],
      ),
    );
  }
}

/// The suggestion, with whatever actually backs it.
class _SuggestionCard extends StatelessWidget {
  final CategorySuggestion suggestion;
  const _SuggestionCard({required this.suggestion});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final s = suggestion;

    if (s.unsure) {
      return Card(
        color: theme.colorScheme.surfaceContainerHighest,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Icon(Icons.help_outline, color: theme.colorScheme.onSurfaceVariant),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('No idea',
                      style: theme.textTheme.titleMedium
                          ?.copyWith(fontWeight: FontWeight.w700)),
                  const SizedBox(height: 4),
                  Text(
                    s.why.isEmpty
                        ? "This name does not say what was bought."
                        : s.why,
                    style: theme.textTheme.bodyMedium,
                  ),
                ],
              ),
            ),
          ]),
        ),
      );
    }

    return Card(
      color: theme.colorScheme.primaryContainer,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              Icon(Icons.auto_awesome,
                  size: 18, color: theme.colorScheme.onPrimaryContainer),
              const SizedBox(width: 8),
              Text('Suggests',
                  style: theme.textTheme.labelLarge?.copyWith(
                      color: theme.colorScheme.onPrimaryContainer)),
              const Spacer(),
              _ConfidenceChip(confidence: s.confidence,
                  hasEvidence: s.hasEvidence),
            ]),
            const SizedBox(height: 6),
            Text(s.categoryName ?? '',
                style: theme.textTheme.headlineSmall?.copyWith(
                    fontWeight: FontWeight.w700,
                    color: theme.colorScheme.onPrimaryContainer)),
            if (s.why.isNotEmpty) ...[
              const SizedBox(height: 6),
              Text(s.why,
                  style: theme.textTheme.bodyMedium?.copyWith(
                      color: theme.colorScheme.onPrimaryContainer)),
            ],

            const SizedBox(height: 14),
            Text('What backs this',
                style: theme.textTheme.labelSmall?.copyWith(
                    color: theme.colorScheme.onPrimaryContainer)),
            const SizedBox(height: 4),

            if (s.matchedPhrase != null)
              _Evidence(
                icon: Icons.format_quote,
                text: 'Your ${s.categoryName} description says '
                    '"${s.matchedPhrase}"',
              ),
            for (final m in s.mentions)
              _Evidence(
                icon: Icons.search,
                text: 'Your "${m.categoryName}" description mentions '
                    '"${m.word}" — ${m.description}',
              ),
            if (!s.hasEvidence)
              _Evidence(
                icon: Icons.lightbulb_outline,
                text: 'Nothing in your descriptions matches this. It went '
                    'on the merchant name alone — worth a second look, and '
                    'worth a description if this keeps coming up.',
              ),
          ],
        ),
      ),
    );
  }
}

class _Evidence extends StatelessWidget {
  final IconData icon;
  final String text;
  const _Evidence({required this.icon, required this.text});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Icon(icon, size: 14, color: theme.colorScheme.onPrimaryContainer),
        const SizedBox(width: 6),
        Expanded(
          child: Text(text,
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onPrimaryContainer)),
        ),
      ]),
    );
  }
}

/// Confidence, qualified by whether anything checkable backs it.
///
/// "High" with no evidence is the exact shape of the one wrong answer in
/// the experiment, so it is not allowed to look the same as high with a
/// description behind it.
class _ConfidenceChip extends StatelessWidget {
  final String confidence;
  final bool hasEvidence;
  const _ConfidenceChip(
      {required this.confidence, required this.hasEvidence});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final strong = confidence == 'high' && hasEvidence;
    final label = switch ((confidence, hasEvidence)) {
      ('high', true) => 'confident',
      ('high', false) => 'confident, but a guess',
      ('medium', _) => 'fairly sure',
      _ => 'unsure',
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: strong
            ? AppTheme.healthy.withValues(alpha: 0.18)
            : AppTheme.warning.withValues(alpha: 0.18),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(label,
          style: theme.textTheme.labelSmall?.copyWith(
              color: theme.colorScheme.onPrimaryContainer)),
    );
  }
}

class _FlowAmountLarge extends StatelessWidget {
  final int signedCents;
  const _FlowAmountLarge({required this.signedCents});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final flow = MoneyFlow.ofSigned(signedCents);
    final colour = flow.isInto
        ? AppTheme.positive(context)
        : theme.colorScheme.onSurface;
    return Row(children: [
      Icon(flow.isInto ? Icons.south_west : Icons.arrow_outward,
          size: 20, color: colour),
      const SizedBox(width: 6),
      Text(MoneyFlowFormat.flow(signedCents),
          style: theme.textTheme.headlineMedium
              ?.copyWith(fontWeight: FontWeight.w700, color: colour)),
      const SizedBox(width: 8),
      Text(flow.isInto ? 'money in' : 'spent',
          style: theme.textTheme.bodyMedium
              ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
    ]);
  }
}

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/insights.dart';
import '../../domain/models.dart';

/// Ask a question about your own spending.
///
/// The model does NOT answer the question. It reads the question and
/// chooses which filters to run; the server computes the figure in SQL
/// and hands back both the number and the query it ran. So the screen
/// shows two things: the answer, and what was actually asked of the
/// data.
///
/// That second line is the important one. A model that reads "last
/// month" as "this month" produces an answer that is fluent, specific
/// and about the wrong thirty days, and there is nothing in the
/// sentence to give it away.
class AskScreen extends ConsumerStatefulWidget {
  const AskScreen({super.key});

  @override
  ConsumerState<AskScreen> createState() => _AskScreenState();
}

class _AskScreenState extends ConsumerState<AskScreen> {
  final _controller = TextEditingController();
  final _history = <SpendingAnswer>[];
  bool _busy = false;
  String? _error;
  String _stage = '';

  static const _examples = <String>[
    'How much did I spend on fast food this month?',
    'What did I pay in subscriptions last month?',
    'How much went to groceries in the last 3 months?',
    'Where did my money go last month?',
    'What did I spend at Amazon this year?',
    'Am I spending more than I did last month?',
  ];

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Future<void> _ask([String? preset]) async {
    final question = (preset ?? _controller.text).trim();
    if (question.isEmpty || _busy) return;

    setState(() {
      _busy = true;
      _error = null;
      _stage = 'Waking the assistant…';
    });
    try {
      final answer = await ref.read(apiProvider).askAboutSpending(
            question,
            onProgress: (AiJob j) {
              if (!mounted) return;
              setState(() => _stage = j.status == 'running'
                  ? 'Working out what you asked…'
                  : 'Waking the assistant…');
            },
          );
      if (!mounted) return;
      setState(() {
        _history.insert(0, answer);
        _controller.clear();
      });
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Ask about your spending')),
      body: Column(
        children: [
          Expanded(
            child: ListView(
              padding: const EdgeInsets.all(12),
              children: [
                if (_history.isEmpty && !_busy) ...[
                  Text('Try asking', style: theme.textTheme.titleSmall),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      for (final e in _examples)
                        ActionChip(label: Text(e), onPressed: () => _ask(e)),
                    ],
                  ),
                  const SizedBox(height: 16),
                  Card(
                    child: Padding(
                      padding: const EdgeInsets.all(14),
                      child: Text(
                        'Answers are calculated from your transactions, not '
                        'estimated. Each one shows the filters it used, so you '
                        'can check the question was read the way you meant it.',
                        style: theme.textTheme.bodySmall,
                      ),
                    ),
                  ),
                ],
                if (_busy)
                  Card(
                    child: Padding(
                      padding: const EdgeInsets.all(16),
                      child: Row(
                        children: [
                          const SizedBox(
                            width: 16,
                            height: 16,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          ),
                          const SizedBox(width: 12),
                          Expanded(child: Text(_stage)),
                        ],
                      ),
                    ),
                  ),
                if (_error != null)
                  Card(
                    color: theme.colorScheme.errorContainer,
                    child: Padding(
                      padding: const EdgeInsets.all(14),
                      child: Text(_error!),
                    ),
                  ),
                for (final a in _history) _AnswerCard(answer: a),
              ],
            ),
          ),
          SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 4, 12, 12),
              child: Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _controller,
                      textInputAction: TextInputAction.send,
                      onSubmitted: (_) => _ask(),
                      decoration: const InputDecoration(
                        hintText: 'How much did I spend on…',
                        border: OutlineInputBorder(),
                        isDense: true,
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  IconButton.filled(
                    onPressed: _busy ? null : () => _ask(),
                    icon: const Icon(Icons.send),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _AnswerCard extends StatelessWidget {
  final SpendingAnswer answer;
  const _AnswerCard({required this.answer});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final r = answer.result;
    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(answer.question,
                style: theme.textTheme.bodySmall?.copyWith(
                  fontStyle: FontStyle.italic,
                )),
            const SizedBox(height: 10),
            if (answer.metric != 'count')
              Text(Money.format(r.totalCents),
                  style: theme.textTheme.headlineMedium)
            else
              Text('${r.count}', style: theme.textTheme.headlineMedium),
            const SizedBox(height: 8),
            Text(answer.answer, style: theme.textTheme.bodyMedium),

            // What was actually asked of the data.
            const SizedBox(height: 12),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
              decoration: BoxDecoration(
                color: theme.colorScheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(6),
              ),
              child: Row(
                children: [
                  Icon(Icons.filter_alt_outlined,
                      size: 14, color: theme.colorScheme.onSurfaceVariant),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      'Read as: ${answer.filterSummary}  ·  '
                      '${r.count} transaction${r.count == 1 ? '' : 's'}',
                      style: theme.textTheme.bodySmall,
                    ),
                  ),
                ],
              ),
            ),

            if (answer.periodNotUnderstood)
              _Note(
                icon: Icons.schedule,
                text: 'The dates in that question were not clear, so this '
                    'covers ${answer.period.label}. Try naming a month.',
              ),
            if (answer.unresolvedFilters.isNotEmpty)
              _Note(
                icon: Icons.help_outline,
                // Without this, a zero caused by a filter that matched
                // nothing looks exactly like a genuine zero.
                text: 'No kind of spending called '
                    '${answer.unresolvedFilters.join(', ')} exists yet, so it '
                    'was not included.',
              ),

            if (r.previousTotalCents != null && answer.metric != 'count') ...[
              const SizedBox(height: 8),
              Text(
                'Previous period (${r.previousPeriod?.label ?? 'before'}): '
                '${Money.format(r.previousTotalCents!)}'
                '${_direction(r.deltaCents)}',
                style: theme.textTheme.bodySmall,
              ),
            ],

            if (r.groups.isNotEmpty) ...[
              const Divider(height: 24),
              for (final g in r.groups)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 3),
                  child: Row(
                    children: [
                      Expanded(
                        child: Text(g.label,
                            maxLines: 1, overflow: TextOverflow.ellipsis),
                      ),
                      Text(Money.format(g.cents)),
                    ],
                  ),
                ),
            ],
          ],
        ),
      ),
    );
  }

  static String _direction(int? delta) {
    if (delta == null || delta == 0) return '';
    return delta > 0
        ? ' — ${Money.format(delta)} more now'
        : ' — ${Money.format(-delta)} less now';
  }
}

class _Note extends StatelessWidget {
  final IconData icon;
  final String text;
  const _Note({required this.icon, required this.text});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.only(top: 10),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, size: 14, color: theme.colorScheme.error),
          const SizedBox(width: 6),
          Expanded(
            child: Text(text,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.error)),
          ),
        ],
      ),
    );
  }
}

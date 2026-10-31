import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

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
    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(answer.question,
                style: theme.textTheme.bodySmall
                    ?.copyWith(fontStyle: FontStyle.italic)),
            const SizedBox(height: 10),
            // Rendered, not shown raw: the model writes **bold** for
            // envelope names and figures, and the asterisks used to
            // land on screen.
            MarkdownBody(
              data: answer.answer,
              selectable: true,
              styleSheet: MarkdownStyleSheet.fromTheme(theme).copyWith(
                p: theme.textTheme.bodyMedium,
                listBullet: theme.textTheme.bodyMedium,
                // The answer is a few sentences; default markdown
                // spacing leaves it looking like a document.
                blockSpacing: 8,
              ),
            ),

            for (final s in answer.emptyLookups)
              _Note(
                icon: Icons.help_outline,
                // A zero from a filter that matched nothing reads
                // exactly like a real zero, so it is called out.
                text: 'Nothing matched ${s.label}. If that is not what '
                    'you meant, try an envelope name or the shop exactly '
                    'as it appears on your statement.',
              ),
            if (answer.hitLimit)
              const _Note(
                icon: Icons.timer_outlined,
                text: 'It ran out of lookups before finishing, so this '
                    'answer may be incomplete.',
              ),

            if (answer.steps.isNotEmpty) ...[
              const SizedBox(height: 12),
              _Lookups(steps: answer.steps),
            ],
          ],
        ),
      ),
    );
  }
}

/// What the assistant actually looked up.
///
/// Collapsed by default — most of the time the answer is the point —
/// but always available, because a figure you cannot check is a claim.
/// It is also where a misread question becomes visible: the arguments
/// show what was asked of the data, not what you meant.
class _Lookups extends StatelessWidget {
  final List<AskStep> steps;
  const _Lookups({required this.steps});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Theme(
      data: theme.copyWith(dividerColor: Colors.transparent),
      child: ExpansionTile(
        tilePadding: EdgeInsets.zero,
        childrenPadding: const EdgeInsets.only(bottom: 4),
        dense: true,
        visualDensity: VisualDensity.compact,
        leading: Icon(Icons.search,
            size: 16, color: theme.colorScheme.onSurfaceVariant),
        title: Text(
          'What it looked up (${steps.length})',
          style: theme.textTheme.bodySmall,
        ),
        children: [
          for (final s in steps)
            Padding(
              padding: const EdgeInsets.only(left: 4, bottom: 6),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(s.label,
                      style: theme.textTheme.bodySmall?.copyWith(
                          fontWeight: FontWeight.w600)),
                  if (s.outcome != null)
                    Text(s.outcome!,
                        style: theme.textTheme.bodySmall?.copyWith(
                            color: theme.colorScheme.onSurfaceVariant)),
                ],
              ),
            ),
        ],
      ),
    );
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

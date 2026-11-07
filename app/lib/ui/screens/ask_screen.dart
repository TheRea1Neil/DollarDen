import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../widgets/ai_waiting.dart';
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
  final _scroll = ScrollController();
  /// The conversation, oldest first. A pending entry has no answer yet.
  final _turns = <_Turn>[];

  /// Sent to the server so a follow-up has something to refer to.
  /// Rebuilt from _turns each time rather than tracked separately --
  /// one source of truth for what was said.
  List<Map<String, String>> get _historyForServer => [
        for (final t in _turns)
          if (t.answer != null)
            {'question': t.question, 'answer': t.answer!.answer},
      ];

  int _contextUsed = 0;
  int _contextLimit = 0;
  bool _busy = false;
  String? _error;

  /// Not an error: the question is queued behind a busy graphics card.
  /// Kept apart from _error so it can be explained rather than shown as
  /// a failure.
  JobDeferred? _deferred;
  String? _lastQuestion;
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
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _ask([String? preset]) async {
    final question = (preset ?? _controller.text).trim();
    if (question.isEmpty || _busy) return;

    setState(() {
      _busy = true;
      _error = null;
      _deferred = null;
      _lastQuestion = question;
      _stage = 'Waking the assistant…';
      // The question appears the instant it is sent, not when the
      // answer returns. Waiting with nothing on screen is what made
      // this feel unresponsive.
      if (_turns.isEmpty || _turns.last.answer != null) {
        _turns.add(_Turn(question));
      }
      _controller.clear();
    });
    _scrollToLatest();
    try {
      final answer = await ref.read(apiProvider).askAboutSpending(
            question,
            history: _historyForServer,
            onProgress: (AiJob j) {
              if (!mounted) return;
              setState(() => _stage = j.status == 'running'
                  ? 'Working out what you asked…'
                  : 'Waking the assistant…');
            },
          );
      if (!mounted) return;
      setState(() {
        final pending = _turns.lastWhere((t) => t.answer == null,
            orElse: () => _turns.isEmpty ? _Turn(question) : _turns.last);
        pending.answer = answer;
        _contextUsed = answer.contextUsed;
        _contextLimit = answer.contextLimit;
      });
      _scrollToLatest();
    } on JobDeferred catch (d) {
      // The GPU is busy or it is the middle of the night. The job is
      // queued and will run on its own; saying "could not answer" here
      // is both wrong and discouraging.
      if (mounted) setState(() => _deferred = d);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// Start again, keeping nothing. The conversation is per-visit by
  /// design -- closing the screen already forgets it -- but mid-thread
  /// there has to be a way to drop a line of questioning that has gone
  /// somewhere unhelpful.
  void _newConversation() {
    setState(() {
      _turns.clear();
      _error = null;
      _deferred = null;
      _contextUsed = 0;
      _contextLimit = 0;
      _controller.clear();
    });
  }

  /// Stick to the newest message. The list is reversed, so "the bottom"
  /// is offset zero.
  void _scrollToLatest() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      _scroll.animateTo(0,
          duration: const Duration(milliseconds: 220), curve: Curves.easeOut);
    });
  }

  /// Override the gate and ask again straight away.
  Future<void> _runAnyway() async {
    final d = _deferred;
    final q = _lastQuestion;
    if (d == null || q == null) return;
    setState(() {
      _deferred = null;
      _busy = true;
      _stage = 'Running it now…';
    });
    try {
      await ref.read(apiProvider).retryJob(d.job.id);
    } catch (_) {
      // Retrying the parked job is best effort; asking again always
      // works and is what the user wanted either way.
    }
    if (!mounted) return;
    setState(() => _busy = false);
    await _ask(q);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Ask about your spending'),
        actions: [
          if (_turns.isNotEmpty)
            IconButton(
              tooltip: 'Start a new conversation',
              icon: const Icon(Icons.add_comment_outlined),
              onPressed: _busy ? null : _newConversation,
            ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: _turns.isEmpty && !_busy
                ? _Starters(examples: _examples, onPick: _ask)
                : ListView.builder(
                    controller: _scroll,
                    // Reversed: index 0 is the NEWEST message and sits at
                    // the bottom, which is where a chat belongs. The old
                    // top-down list meant new answers appeared above old
                    // ones and the view jumped to the wrong end.
                    reverse: true,
                    padding: const EdgeInsets.fromLTRB(12, 12, 12, 4),
                    itemCount: _turns.length + 1,
                    itemBuilder: (context, i) {
                      // Index 0 is whatever is happening right now.
                      if (i == 0) {
                        return Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            if (_deferred != null)
                              AiWaiting(
                                deferred: _deferred!,
                                inline: true,
                                onRunAnyway: _busy ? null : _runAnyway,
                              ),
                            if (_error != null)
                              Card(
                                color: theme.colorScheme.errorContainer,
                                child: Padding(
                                  padding: const EdgeInsets.all(14),
                                  child: Text(_error!),
                                ),
                              ),
                          ],
                        );
                      }
                      final turn = _turns[_turns.length - i];
                      return Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          _QuestionBubble(text: turn.question),
                          if (turn.answer != null)
                            _AnswerCard(answer: turn.answer!)
                          else if (_busy)
                            _Thinking(stage: _stage),
                        ],
                      );
                    },
                  ),
          ),
          if (_contextLimit > 0)
            _ContextBar(used: _contextUsed, limit: _contextLimit),
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

/// The empty state: what this can answer, before anything is asked.
class _Starters extends StatelessWidget {
  final List<String> examples;
  final void Function(String) onPick;
  const _Starters({required this.examples, required this.onPick});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text('Try asking', style: theme.textTheme.titleSmall),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            for (final e in examples)
              ActionChip(label: Text(e), onPressed: () => onPick(e)),
          ],
        ),
        const SizedBox(height: 16),
        Card(
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Text(
              'Answers are calculated from your transactions, not '
              'estimated. Each one shows the filters it used, so you can '
              'check the question was read the way you meant it.\n\n'
              'Follow-up questions keep the thread, so you can say '
              '"what about last month?" and it will know what you mean.',
              style: theme.textTheme.bodySmall,
            ),
          ),
        ),
      ],
    );
  }
}

/// A pending answer, in the place the answer will appear.
class _Thinking extends StatelessWidget {
  final String stage;
  const _Thinking({required this.stage});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Align(
      alignment: Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.only(right: 48, bottom: 12),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        decoration: BoxDecoration(
          color: theme.colorScheme.surfaceContainerHighest,
          borderRadius: BorderRadius.circular(16),
        ),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          const SizedBox(
              width: 14,
              height: 14,
              child: CircularProgressIndicator(strokeWidth: 2)),
          const SizedBox(width: 10),
          Text(stage, style: theme.textTheme.bodySmall),
        ]),
      ),
    );
  }
}

/// How much of the model's memory this conversation is using.
///
/// Shown rather than capping the thread at a fixed number of turns: the
/// real limit is tokens, and a turn count says nothing about how long
/// each turn was. On a machine with a larger window this simply fills
/// more slowly, with no other change.
class _ContextBar extends StatelessWidget {
  final int used;
  final int limit;
  const _ContextBar({required this.used, required this.limit});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final ratio = limit == 0 ? 0.0 : (used / limit).clamp(0.0, 1.0);
    final tight = ratio > 0.8;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 2),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ClipRRect(
            borderRadius: BorderRadius.circular(3),
            child: LinearProgressIndicator(
              value: ratio,
              minHeight: 3,
              backgroundColor: theme.colorScheme.surfaceContainerHighest,
              valueColor: AlwaysStoppedAnimation(
                tight ? theme.colorScheme.error : theme.colorScheme.primary,
              ),
            ),
          ),
          const SizedBox(height: 3),
          Text(
            tight
                ? 'Conversation memory almost full — start a new one for '
                    'best results'
                : 'Conversation memory ${(ratio * 100).round()}% '
                    '($used of $limit)',
            style: theme.textTheme.labelSmall?.copyWith(
              color: tight
                  ? theme.colorScheme.error
                  : theme.colorScheme.onSurfaceVariant,
            ),
          ),
        ],
      ),
    );
  }
}

/// One exchange. `answer` is null while the model is still working,
/// which is what lets the question appear the moment it is sent.
class _Turn {
  final String question;
  SpendingAnswer? answer;
  _Turn(this.question);
}

/// A sent question, shown the instant you press send.
class _QuestionBubble extends StatelessWidget {
  final String text;
  const _QuestionBubble({required this.text});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Align(
      alignment: Alignment.centerRight,
      child: Container(
        margin: const EdgeInsets.only(left: 48, bottom: 8),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          color: theme.colorScheme.primaryContainer,
          borderRadius: const BorderRadius.only(
            topLeft: Radius.circular(16),
            topRight: Radius.circular(16),
            bottomLeft: Radius.circular(16),
            bottomRight: Radius.circular(4),
          ),
        ),
        child: Text(text,
            style: theme.textTheme.bodyMedium
                ?.copyWith(color: theme.colorScheme.onPrimaryContainer)),
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
      margin: const EdgeInsets.only(right: 24, bottom: 14),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // The question is its own bubble directly above now, so
            // repeating it here just doubled it on screen.
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

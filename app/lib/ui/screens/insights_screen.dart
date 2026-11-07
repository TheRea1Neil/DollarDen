import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../data/api_client.dart';
import '../widgets/ai_waiting.dart';
import '../../data/providers.dart';
import '../../domain/insights.dart';
import 'ask_screen.dart';
import 'expense_types_screen.dart';

/// Insights: the figures first, the prose on request.
///
/// The cards load from SQL and are always there. The plain-words read
/// is a button, not an automatic fetch, because it needs the inference
/// host -- which sleeps, takes about ten seconds to wake, and is not
/// worth making the whole screen wait for (ADR 0003).
class InsightsScreen extends ConsumerStatefulWidget {
  const InsightsScreen({super.key});

  @override
  ConsumerState<InsightsScreen> createState() => _InsightsScreenState();
}

class _InsightsScreenState extends ConsumerState<InsightsScreen> {
  String? _narrative;
  bool _narrating = false;
  String? _narrateError;

  /// Queued behind a busy graphics card. This screen had no handling at
  /// all for it -- the spinner simply stopped and nothing was said,
  /// which is the worst of the three possible outcomes.
  JobDeferred? _narrateDeferred;

  static const _periods = <String, String>{
    'this_month': 'This month',
    'last_month': 'Last month',
    'last_7_days': 'Last 7 days',
    'last_30_days': 'Last 30 days',
    'last_3_months': 'Last 3 months',
    'this_year': 'This year',
  };

  Future<void> _narrate() async {
    setState(() {
      _narrating = true;
      _narrateError = null;
      _narrateDeferred = null;
    });
    try {
      final period = ref.read(insightsPeriodProvider);
      final out = await ref.read(apiProvider).narrateInsights(period: period);
      if (mounted) setState(() => _narrative = out.narrative);
    } on ApiException catch (e) {
      if (mounted) setState(() => _narrateError = e.message);
    } on JobDeferred catch (d) {
      if (mounted) setState(() => _narrateDeferred = d);
    } catch (e) {
      // Never leave the spinner stopping with nothing said.
      if (mounted) setState(() => _narrateError = '$e');
    } finally {
      if (mounted) setState(() => _narrating = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final period = ref.watch(insightsPeriodProvider);
    final insights = ref.watch(insightsProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Insights'),
        actions: [
          IconButton(
            tooltip: 'Kinds of spending',
            icon: const Icon(Icons.label_outline),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute<void>(builder: (_) => const ExpenseTypesScreen()),
            ),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => Navigator.of(context).push(
          MaterialPageRoute<void>(builder: (_) => const AskScreen()),
        ),
        icon: const Icon(Icons.question_answer_outlined),
        label: const Text('Ask'),
      ),
      body: Column(
        children: [
          _PeriodBar(
            periods: _periods,
            selected: period,
            onPick: (p) {
              ref.read(insightsPeriodProvider.notifier).state = p;
              // The narrative described the old period; keeping it on
              // screen next to new numbers would be straightforwardly
              // misleading.
              setState(() => _narrative = null);
            },
          ),
          Expanded(
            child: insights.when(
              loading: () => const Center(child: CircularProgressIndicator()),
              error: (e, _) => _Retry(
                message: '$e',
                onRetry: () => ref.invalidate(insightsProvider),
              ),
              data: (data) => RefreshIndicator(
                onRefresh: () async {
                  ref.invalidate(insightsProvider);
                  ref.invalidate(untypedSpendingProvider);
                  setState(() => _narrative = null);
                },
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(12, 4, 12, 96),
                  children: [
                    _NarrativeBox(
                      text: _narrative,
                      busy: _narrating,
                      error: _narrateError,
                      deferred: _narrateDeferred,
                      onAsk: _narrate,
                    ),
                    for (final card in data.cards) _buildCard(context, card),
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildCard(BuildContext context, InsightCard c) => switch (c.kind) {
        'empty' => _EmptyCard(card: c),
        'flow' => _FlowCard(card: c),
        'pace' => _PaceCard(card: c),
        'by_type' => _BreakdownCard(card: c),
        'movers' => _MoversCard(card: c),
        'unusual' => _UnusualCard(card: c),
        'recurring' => _RecurringCard(card: c),
        'envelopes' => _EnvelopesCard(card: c),
        'top_merchants' => _MerchantsCard(card: c),
        'untyped' => _UntypedCard(card: c),
        // A card kind this build does not know about. Showing its title
        // beats dropping it silently: the server decides what exists.
        _ => _GenericCard(card: c),
      };
}

// --------------------------------------------------------------------- //
// Chrome
// --------------------------------------------------------------------- //
class _PeriodBar extends StatelessWidget {
  final Map<String, String> periods;
  final String selected;
  final ValueChanged<String> onPick;

  const _PeriodBar({
    required this.periods,
    required this.selected,
    required this.onPick,
  });

  @override
  Widget build(BuildContext context) => SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        child: Row(
          children: [
            for (final e in periods.entries)
              Padding(
                padding: const EdgeInsets.only(right: 8),
                child: ChoiceChip(
                  label: Text(e.value),
                  selected: e.key == selected,
                  onSelected: (_) => onPick(e.key),
                ),
              ),
          ],
        ),
      );
}

/// A card shell. Every insight uses it so they read as one list.
class _Card extends StatelessWidget {
  final String title;
  final String? subtitle;
  final Widget child;
  final Widget? trailing;

  const _Card({
    required this.title,
    required this.child,
    this.subtitle,
    this.trailing,
  });

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
            Row(
              children: [
                Expanded(
                  child: Text(title, style: theme.textTheme.titleMedium),
                ),
                if (trailing != null) trailing!,
              ],
            ),
            if (subtitle != null) ...[
              const SizedBox(height: 2),
              Text(subtitle!, style: theme.textTheme.bodySmall),
            ],
            const SizedBox(height: 12),
            child,
          ],
        ),
      ),
    );
  }
}

class _NarrativeBox extends StatelessWidget {
  final String? text;
  final bool busy;
  final String? error;
  final JobDeferred? deferred;
  final VoidCallback onAsk;

  const _NarrativeBox({
    required this.text,
    required this.busy,
    required this.error,
    required this.deferred,
    required this.onAsk,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    if (busy) {
      return const _Card(
        title: 'In plain words',
        child: Row(
          children: [
            SizedBox(
              width: 16,
              height: 16,
              child: CircularProgressIndicator(strokeWidth: 2),
            ),
            SizedBox(width: 12),
            // The host sleeps; saying so beats an unexplained wait.
            Expanded(child: Text('Waking the assistant…')),
          ],
        ),
      );
    }
    if (deferred != null) {
      return _Card(
        title: 'In plain words',
        child: AiWaiting(deferred: deferred!, inline: true),
      );
    }
    if (text != null) {
      return _Card(
        title: 'In plain words',
        trailing: IconButton(
          tooltip: 'Again',
          icon: const Icon(Icons.refresh, size: 18),
          onPressed: onAsk,
        ),
        child: Text(text!, style: theme.textTheme.bodyMedium),
      );
    }
    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: ListTile(
        leading: const Icon(Icons.auto_awesome_outlined),
        title: const Text('Explain this in plain words'),
        subtitle: Text(
          error ?? 'Reads the figures below back to you. Takes a few seconds.',
          style: error != null
              ? TextStyle(color: Theme.of(context).colorScheme.error)
              : null,
        ),
        onTap: onAsk,
      ),
    );
  }
}

class _Retry extends StatelessWidget {
  final String message;
  final VoidCallback onRetry;
  const _Retry({required this.message, required this.onRetry});

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Text(message, textAlign: TextAlign.center),
              const SizedBox(height: 12),
              OutlinedButton(onPressed: onRetry, child: const Text('Try again')),
            ],
          ),
        ),
      );
}

// --------------------------------------------------------------------- //
// Cards
// --------------------------------------------------------------------- //
class _EmptyCard extends StatelessWidget {
  final InsightCard card;
  const _EmptyCard({required this.card});

  @override
  Widget build(BuildContext context) => _Card(
        title: card.title,
        child: Text(card.strAt('detail') ?? ''),
      );
}

class _FlowCard extends StatelessWidget {
  final InsightCard card;
  const _FlowCard({required this.card});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final net = card.intAt('net_cents');
    final prev = card.data['previous_out_cents'] as int?;
    final pct = card.data['change_pct'] as num?;

    return _Card(
      title: card.title,
      subtitle: '${card.intAt('txn_count')} transactions',
      child: Column(
        children: [
          Row(
            children: [
              Expanded(
                child: _Figure(
                  label: 'Spent',
                  cents: card.intAt('out_cents'),
                  emphasis: true,
                ),
              ),
              Expanded(
                child: _Figure(label: 'Came in', cents: card.intAt('in_cents')),
              ),
            ],
          ),
          const Divider(height: 24),
          Row(
            children: [
              Text(net >= 0 ? 'Left over' : 'Short by',
                  style: theme.textTheme.bodyMedium),
              const Spacer(),
              Text(
                Money.format(net.abs()),
                style: theme.textTheme.titleMedium?.copyWith(
                  color: net >= 0
                      ? theme.colorScheme.primary
                      : theme.colorScheme.error,
                ),
              ),
            ],
          ),
          if (prev != null) ...[
            const SizedBox(height: 8),
            Align(
              alignment: Alignment.centerLeft,
              child: Text(
                'Spending was ${Money.format(prev)} the period before'
                '${pct == null ? '' : ' (${pct > 0 ? '+' : ''}$pct%)'}.',
                style: theme.textTheme.bodySmall,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _Figure extends StatelessWidget {
  final String label;
  final int cents;
  final bool emphasis;
  const _Figure({required this.label, required this.cents, this.emphasis = false});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: theme.textTheme.bodySmall),
        const SizedBox(height: 2),
        Text(
          Money.format(cents),
          style: emphasis
              ? theme.textTheme.headlineSmall
              : theme.textTheme.titleMedium,
        ),
      ],
    );
  }
}

class _PaceCard extends StatelessWidget {
  final InsightCard card;
  const _PaceCard({required this.card});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final elapsed = card.intAt('days_elapsed');
    final total = card.intAt('days_total');
    final projected = card.intAt('projected_cents');
    final prev = card.data['previous_total_cents'] as int?;

    return _Card(
      title: card.title,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          LinearProgressIndicator(
            value: total == 0 ? 0 : elapsed / total,
            minHeight: 6,
          ),
          const SizedBox(height: 12),
          Text(
            '${Money.format(card.intAt('spent_cents'))} so far. '
            'At this rate, ${Money.format(projected)} by the end.',
            style: theme.textTheme.bodyMedium,
          ),
          if (prev != null) ...[
            const SizedBox(height: 4),
            Text(
              projected > prev
                  ? 'That would be ${Money.format(projected - prev)} more than last period.'
                  : 'That would be ${Money.format(prev - projected)} less than last period.',
              style: theme.textTheme.bodySmall,
            ),
          ],
        ],
      ),
    );
  }
}

/// Spending by kind, as proportional bars.
///
/// Bars rather than a pie: a bar can carry a label, a figure and a
/// comparison at a glance, and a pie of twelve slices carries none of
/// them.
class _BreakdownCard extends StatelessWidget {
  final InsightCard card;
  const _BreakdownCard({required this.card});

  @override
  Widget build(BuildContext context) {
    final groups = card.listAt('groups').map(SpendGroup.fromJson).toList();
    final biggest = groups.isEmpty
        ? 1
        : groups.map((g) => g.cents).reduce((a, b) => a > b ? a : b);

    return _Card(
      title: card.title,
      subtitle: Money.format(card.intAt('total_cents')),
      child: Column(
        children: [
          for (final g in groups)
            _Bar(
              label: g.label,
              cents: g.cents,
              fraction: biggest == 0 ? 0 : g.cents / biggest,
              note: g.previousCents == 0
                  ? 'new this period'
                  : _movement(g.deltaCents),
              onTap: g.key == null
                  ? null
                  : () => _openDrill(context, g.label, typeId: g.key!),
            ),
        ],
      ),
    );
  }

  static String _movement(int delta) {
    if (delta == 0) return 'no change';
    return delta > 0
        ? '${Money.format(delta)} more'
        : '${Money.format(-delta)} less';
  }
}

class _Bar extends StatelessWidget {
  final String label;
  final int cents;
  final double fraction;
  final String? note;
  final VoidCallback? onTap;

  const _Bar({
    required this.label,
    required this.cents,
    required this.fraction,
    this.note,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(label,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: theme.textTheme.bodyMedium),
                ),
                Text(Money.format(cents), style: theme.textTheme.bodyMedium),
              ],
            ),
            const SizedBox(height: 4),
            ClipRRect(
              borderRadius: BorderRadius.circular(3),
              child: LinearProgressIndicator(
                value: fraction.clamp(0, 1),
                minHeight: 6,
                backgroundColor: theme.colorScheme.surfaceContainerHighest,
              ),
            ),
            if (note != null) ...[
              const SizedBox(height: 2),
              Text(note!, style: theme.textTheme.bodySmall),
            ],
          ],
        ),
      ),
    );
  }
}

class _MoversCard extends StatelessWidget {
  final InsightCard card;
  const _MoversCard({required this.card});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return _Card(
      title: card.title,
      child: Column(
        children: [
          for (final i in card.listAt('items'))
            ListTile(
              dense: true,
              contentPadding: EdgeInsets.zero,
              leading: Icon(
                (i['delta_cents'] as int) > 0
                    ? Icons.trending_up
                    : Icons.trending_down,
                color: (i['delta_cents'] as int) > 0
                    ? theme.colorScheme.error
                    : theme.colorScheme.primary,
              ),
              title: Text('${i['label']}'),
              subtitle: Text(
                '${Money.format(i['previous_cents'] as int)} → '
                '${Money.format(i['now_cents'] as int)}',
              ),
              trailing: Text(
                ((i['delta_cents'] as int) > 0 ? '+' : '−') +
                    Money.format((i['delta_cents'] as int).abs()),
                style: theme.textTheme.bodyMedium,
              ),
            ),
        ],
      ),
    );
  }
}

class _UnusualCard extends StatelessWidget {
  final InsightCard card;
  const _UnusualCard({required this.card});

  @override
  Widget build(BuildContext context) => _Card(
        title: card.title,
        subtitle: 'Measured against your own months, not against anyone else',
        child: Column(
          children: [
            for (final i in card.listAt('items'))
              ListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.priority_high),
                title: Text('${i['label']}'),
                subtitle: Text(
                  '${Money.format(i['cents'] as int)} this period, '
                  'usually around ${Money.format(i['typical_cents'] as int)}',
                ),
                trailing: Text('${i['ratio']}×'),
              ),
          ],
        ),
      );
}

class _RecurringCard extends StatelessWidget {
  final InsightCard card;
  const _RecurringCard({required this.card});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final ended = card.listAt('ended');
    return _Card(
      title: card.title,
      subtitle: '${card.intAt('count')} charges, '
          '${Money.format(card.intAt('monthly_cents'))} a month · '
          '${Money.format(card.intAt('annual_cents'))} a year',
      child: Column(
        children: [
          for (final i in card.listAt('items').where((i) => i['likely_ended'] != true))
            ListTile(
              dense: true,
              contentPadding: EdgeInsets.zero,
              title: Text('${i['merchant']}'),
              subtitle: Text(
                '${i['cadence']}'
                '${i['next_expected'] == null ? '' : ' · next ${i['next_expected']}'}',
              ),
              trailing: Text(Money.format(i['typical_cents'] as int)),
            ),
          if (ended.isNotEmpty) ...[
            const Divider(height: 20),
            Align(
              alignment: Alignment.centerLeft,
              child: Text('Looks stopped', style: theme.textTheme.bodySmall),
            ),
            for (final i in ended)
              ListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                title: Text('${i['merchant']}',
                    style: TextStyle(color: theme.disabledColor)),
                subtitle: Text('last charged ${i['last_seen']}'),
              ),
          ],
        ],
      ),
    );
  }
}

class _EnvelopesCard extends StatelessWidget {
  final InsightCard card;
  const _EnvelopesCard({required this.card});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return _Card(
      title: card.title,
      subtitle: 'An envelope that is always overspent is a plan to change, '
          'not a bad month',
      child: Column(
        children: [
          for (final i in card.listAt('items'))
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 6),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(child: Text('${i['name']}')),
                      Text(
                        '${Money.format(i['spent_cents'] as int)}'
                        ' of ${Money.format(i['funded_cents'] as int)}',
                        style: theme.textTheme.bodySmall?.copyWith(
                          color: i['overspent'] == true
                              ? theme.colorScheme.error
                              : null,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 4),
                  ClipRRect(
                    borderRadius: BorderRadius.circular(3),
                    child: LinearProgressIndicator(
                      value: (((i['used_pct'] as num?) ?? 0) / 100).clamp(0, 1),
                      minHeight: 6,
                      color: i['overspent'] == true
                          ? theme.colorScheme.error
                          : null,
                      backgroundColor: theme.colorScheme.surfaceContainerHighest,
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

class _MerchantsCard extends StatelessWidget {
  final InsightCard card;
  const _MerchantsCard({required this.card});

  @override
  Widget build(BuildContext context) => _Card(
        title: card.title,
        child: Column(
          children: [
            for (final g in card.listAt('items'))
              ListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                title: Text('${g['label']}',
                    maxLines: 1, overflow: TextOverflow.ellipsis),
                subtitle: Text('${g['count']} times · ${g['share_pct']}% of spending'),
                trailing: Text(Money.format(g['cents'] as int)),
                onTap: () => _openDrill(context, '${g['label']}',
                    merchant: '${g['label']}'),
              ),
          ],
        ),
      );
}

/// How much of the total has no kind yet.
///
/// Shown deliberately: every other card is only as trustworthy as this
/// number is small, and hiding it would let the screen look
/// authoritative while describing a fraction of the money.
class _UntypedCard extends ConsumerWidget {
  final InsightCard card;
  const _UntypedCard({required this.card});

  @override
  Widget build(BuildContext context, WidgetRef ref) => _Card(
        title: card.title,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '${Money.format(card.intAt('cents'))} across '
              '${card.intAt('count')} transactions '
              '(${card.dblAt('share_pct')}% of spending) has no kind assigned, '
              'so the breakdown above is incomplete.',
            ),
            const SizedBox(height: 12),
            Align(
              alignment: Alignment.centerLeft,
              child: FilledButton.tonalIcon(
                onPressed: () => Navigator.of(context).push(
                  MaterialPageRoute<void>(
                    builder: (_) => const ExpenseTypesScreen(startOnUntyped: true),
                  ),
                ),
                icon: const Icon(Icons.label_outline),
                label: const Text('Label them'),
              ),
            ),
          ],
        ),
      );
}

class _GenericCard extends StatelessWidget {
  final InsightCard card;
  const _GenericCard({required this.card});

  @override
  Widget build(BuildContext context) =>
      _Card(title: card.title, child: const SizedBox.shrink());
}

// --------------------------------------------------------------------- //
void _openDrill(
  BuildContext context,
  String title, {
  String? typeId,
  String? merchant,
}) {
  Navigator.of(context).push(
    MaterialPageRoute<void>(
      builder: (_) => _DrillScreen(title: title, typeId: typeId, merchant: merchant),
    ),
  );
}

/// The transactions behind one figure.
///
/// Every total on the insights screen opens into this. An insight you
/// cannot audit is a claim, not information.
class _DrillScreen extends ConsumerWidget {
  final String title;
  final String? typeId;
  final String? merchant;

  const _DrillScreen({required this.title, this.typeId, this.merchant});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final period = ref.watch(insightsPeriodProvider);
    return Scaffold(
      appBar: AppBar(title: Text(title)),
      body: FutureBuilder<List<SpendRow>>(
        future: ref.read(apiProvider).spendTransactions(
              period: period,
              expenseTypeIds: typeId == null ? const [] : [typeId!],
              merchant: merchant,
            ),
        builder: (context, snap) {
          if (snap.connectionState != ConnectionState.done) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snap.hasError) {
            return Center(child: Text('${snap.error}'));
          }
          final rows = snap.data ?? const <SpendRow>[];
          if (rows.isEmpty) {
            return const Center(child: Text('Nothing in this period.'));
          }
          final total = rows.fold<int>(0, (a, r) => a + r.amountCents);
          return ListView(
            children: [
              ListTile(
                title: const Text('Total'),
                trailing: Text(Money.format(total),
                    style: Theme.of(context).textTheme.titleMedium),
              ),
              const Divider(height: 1),
              for (final r in rows)
                ListTile(
                  title: Text(r.merchant),
                  subtitle: Text('${r.date} · ${r.category}'
                      '${r.expenseType.isEmpty ? '' : ' · ${r.expenseType}'}'),
                  trailing: Text(MoneyFlowFormat.flow(r.amountCents)),
                ),
            ],
          );
        },
      ),
    );
  }
}

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../core/theme.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';

/// Everything about one transaction, including why the AI filed it
/// where it did.
///
/// That reasoning was being recorded all along and shown only as a
/// subtitle that ellipsised after a few words. It is the main feedback
/// loop the user has: seeing "Amazon sells everything from groceries
/// to cloud services" is what tells you a category's spend criteria
/// needs tightening.
Future<void> showTransactionDetail(
  BuildContext context,
  WidgetRef ref,
  String transactionId,
) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (_) => DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.7,
      maxChildSize: 0.95,
      builder: (_, controller) =>
          _Body(transactionId: transactionId, controller: controller),
    ),
  );
}

class _Body extends ConsumerWidget {
  final String transactionId;
  final ScrollController controller;
  const _Body({required this.transactionId, required this.controller});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    return FutureBuilder<TxnDetail>(
      future: ref.read(apiProvider).transactionDetail(transactionId),
      builder: (context, snap) {
        if (snap.connectionState != ConnectionState.done) {
          return const SizedBox(
            height: 200,
            child: Center(child: CircularProgressIndicator()),
          );
        }
        if (snap.hasError) {
          return Padding(
            padding: const EdgeInsets.all(24),
            child: Text('${snap.error}'),
          );
        }
        final d = snap.data!;
        final flow = MoneyFlow.ofSigned(d.amountCents);

        return ListView(
          controller: controller,
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 28),
          children: [
            Text(d.title, style: theme.textTheme.titleLarge),
            const SizedBox(height: 2),
            Text(
              MoneyFlowFormat.flow(d.amountCents),
              style: theme.textTheme.headlineSmall?.copyWith(
                fontWeight: FontWeight.w700,
                color: flow.isInto ? AppTheme.positive(context) : null,
              ),
            ),
            if (d.pending)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: _Tag(
                  icon: Icons.schedule,
                  text: 'Still processing at the bank — the amount can change',
                  colour: theme.colorScheme.onSurfaceVariant,
                ),
              ),
            const SizedBox(height: 18),

            if (d.ai != null) _AiCard(ai: d.ai!),

            _Section(title: 'Where it went'),
            if (d.netByCategory.isEmpty)
              Text('Not filed anywhere yet.',
                  style: theme.textTheme.bodyMedium)
            else
              for (final e in d.netByCategory.entries)
                _Row(label: e.key, value: Money.format(e.value.abs())),

            const SizedBox(height: 18),
            _Section(title: 'Details'),
            _Row(label: 'Date', value: d.date),
            // The full bank string, not the tidied merchant name: this
            // is what the matchers and the model actually see, so it
            // is what you need in order to write a matcher.
            _Row(label: 'On your statement', value: d.name, wrap: true),
            if (d.account != null) _Row(label: 'Account', value: d.account!),
            if (d.plaidCategory != null)
              _Row(label: 'Bank category', value: d.plaidCategory!),
            _Row(label: 'Treated as', value: d.treatmentReason),

            if (d.allocations.length > 1) ...[
              const SizedBox(height: 18),
              _Section(title: 'History'),
              Text(
                'The ledger is append-only, so a correction appears as a '
                'new entry rather than changing the old one.',
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
              ),
              const SizedBox(height: 8),
              for (final a in d.allocations) _LedgerRow(entry: a),
            ],
          ],
        );
      },
    );
  }
}

class _AiCard extends StatelessWidget {
  final TxnAiVerdict ai;
  const _AiCard({required this.ai});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      color: theme.colorScheme.surfaceContainerHighest,
      margin: const EdgeInsets.only(bottom: 18),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              Icon(Icons.auto_awesome,
                  size: 16, color: theme.colorScheme.primary),
              const SizedBox(width: 8),
              Expanded(
                  child: Text(ai.headline,
                      style: theme.textTheme.titleSmall)),
            ]),
            if (ai.why != null && ai.why!.trim().isNotEmpty) ...[
              const SizedBox(height: 10),
              // Not truncated. This is the whole point of the sheet.
              Text('“${ai.why!.trim()}”',
                  style: theme.textTheme.bodyMedium
                      ?.copyWith(fontStyle: FontStyle.italic)),
            ],
            if (ai.categoryName != null) ...[
              const SizedBox(height: 8),
              Text('It chose ${ai.categoryName}',
                  style: theme.textTheme.bodySmall),
            ],
            if (ai.confidence != null)
              Text('Confidence: ${ai.confidence}',
                  style: theme.textTheme.bodySmall?.copyWith(
                      color: theme.colorScheme.onSurfaceVariant)),
            if (ai.error != null) ...[
              const SizedBox(height: 8),
              Text(ai.error!,
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.error)),
            ],
          ],
        ),
      ),
    );
  }
}

class _LedgerRow extends StatelessWidget {
  final TxnLedgerEntry entry;
  const _LedgerRow({required this.entry});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final added = entry.amountCents > 0;
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(added ? Icons.undo : Icons.arrow_outward,
              size: 14, color: theme.colorScheme.onSurfaceVariant),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('${entry.sourceLabel} · ${entry.category}',
                    style: theme.textTheme.bodyMedium),
                if (entry.note != null && entry.note!.trim().isNotEmpty)
                  Text(entry.note!.trim(),
                      style: theme.textTheme.bodySmall?.copyWith(
                          color: theme.colorScheme.onSurfaceVariant)),
              ],
            ),
          ),
          const SizedBox(width: 8),
          Text(MoneyFlowFormat.flow(-entry.amountCents),
              style: theme.textTheme.bodySmall),
        ],
      ),
    );
  }
}

class _Section extends StatelessWidget {
  final String title;
  const _Section({required this.title});

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Text(title,
            style: Theme.of(context).textTheme.titleSmall?.copyWith(
                color: Theme.of(context).colorScheme.onSurfaceVariant)),
      );
}

class _Row extends StatelessWidget {
  final String label;
  final String value;
  final bool wrap;
  const _Row({required this.label, required this.value, this.wrap = false});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 128,
            child: Text(label,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          ),
          Expanded(
            child: SelectableText(value, style: theme.textTheme.bodyMedium),
          ),
        ],
      ),
    );
  }
}

class _Tag extends StatelessWidget {
  final IconData icon;
  final String text;
  final Color colour;
  const _Tag({required this.icon, required this.text, required this.colour});

  @override
  Widget build(BuildContext context) => Row(children: [
        Icon(icon, size: 13, color: colour),
        const SizedBox(width: 6),
        Expanded(
          child: Text(text,
              style: Theme.of(context)
                  .textTheme
                  .bodySmall
                  ?.copyWith(color: colour)),
        ),
      ]);
}

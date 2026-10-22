import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../core/theme.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';

/// Record a paycheck — but show exactly what it will do first.
///
/// Splitting a paycheck moves real money across every envelope at once
/// and is the least reversible-feeling thing the app does. The preview
/// writes nothing, so there is no reason to make anyone guess.
class PaycheckScreen extends ConsumerStatefulWidget {
  const PaycheckScreen({super.key});

  @override
  ConsumerState<PaycheckScreen> createState() => _PaycheckScreenState();
}

class _PaycheckScreenState extends ConsumerState<PaycheckScreen> {
  final _amount = TextEditingController();
  bool _busy = false;
  String? _error;
  PaycheckPreview? _preview;

  @override
  void dispose() {
    _amount.dispose();
    super.dispose();
  }

  int? get _cents => Money.parse(_amount.text);

  Future<void> _previewIt() async {
    final cents = _cents;
    if (cents == null || cents <= 0) return;
    setState(() {
      _busy = true;
      _error = null;
      _preview = null;
    });
    try {
      final p = await ref.read(apiProvider).previewPaycheck(cents);
      if (mounted) setState(() => _preview = p);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
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
            Card(
              child: Column(children: [
                for (var i = 0; i < p.lines.length; i++) ...[
                  if (i > 0) const Divider(height: 1, indent: 16, endIndent: 16),
                  _PreviewRow(line: p.lines[i]),
                ],
              ]),
            ),
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
  const _PreviewRow({required this.line});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final nothing = line.gotNothing;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 22,
            child: Text('${line.orderIndex}',
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
        ],
      ),
    );
  }
}

import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter/services.dart' show PlatformException;
import 'package:image_picker/image_picker.dart';

import '../../core/money.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../domain/insights.dart';
import '../../domain/models.dart';

/// Split one charge across envelopes by photographing its receipt.
///
/// Reached two ways, because they are two different needs:
///   - from the review queue, for a charge nobody has filed yet
///   - from history, for one that filed correctly into a single
///     envelope and really covers several
///
/// Both end in the same place: a set of splits that sum to the charge.
/// The backend guarantees that sum (app/ai/receipt_loop.py), so this
/// screen's job is to show the split, mark what needs a human eye, and
/// let it be corrected before anything is written.
class ScanReceiptScreen extends ConsumerStatefulWidget {
  /// The transaction being split.
  final String transactionId;

  /// What the bank charged, budget-signed (positive == money out).
  final int amountCents;
  final String title;

  /// True when this charge is already filed and we are re-splitting it.
  /// Only changes the wording and which endpoint is called.
  final bool alreadyFiled;

  const ScanReceiptScreen({
    super.key,
    required this.transactionId,
    required this.amountCents,
    required this.title,
    this.alreadyFiled = false,
  });

  @override
  ConsumerState<ScanReceiptScreen> createState() => _ScanReceiptScreenState();
}

class _ScanReceiptScreenState extends ConsumerState<ScanReceiptScreen> {
  ReceiptScan? _scan;
  List<ScannedLine> _lines = [];
  bool _busy = false;
  String _stage = '';
  String? _error;

  int get _sum => _lines.fold(0, (a, l) => a + l.amountCents);
  bool get _balances => _sum == widget.amountCents;

  Future<void> _pick(ImageSource source) async {
    final picker = ImagePicker();
    final XFile? file;
    try {
      file = await picker.pickImage(
        source: source,
        // 1200px, not 2000. Resolution made no measurable difference to
        // accuracy when tested, and the image is re-sent with the WHOLE
        // conversation on every tool call -- a 39-line receipt took 198
        // seconds largely because ~850KB of photo was reprocessed
        // fourteen times. Smaller image, same answer, far less waiting.
        maxWidth: 1200,
        imageQuality: 80,
      );
    } on PlatformException catch (e) {
      setState(() => _error = 'Could not open the camera: ${e.message}');
      return;
    }
    if (file == null) return;

    setState(() {
      _busy = true;
      _error = null;
      _stage = 'Waking the assistant…';
    });
    try {
      final bytes = await File(file.path).readAsBytes();
      final scan = await ref.read(apiProvider).scanReceipt(
            imageBase64: base64Encode(bytes),
            amountCents: widget.amountCents,
            onProgress: (AiJob j) {
              if (!mounted) return;
              setState(() => _stage = j.status == 'running'
                  ? 'Reading the receipt…'
                  : 'Waking the assistant…');
            },
          );
      if (!mounted) return;
      setState(() {
        _scan = scan;
        _lines = List.of(scan.items);
      });
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _submit() async {
    if (!_balances) return;
    setState(() {
      _busy = true;
      _stage = 'Filing…';
    });

    // Several lines in one envelope is one allocation, not several.
    final totals = <String, int>{};
    final names = <String, String>{};
    for (final l in _lines) {
      final id = l.categoryId;
      if (id == null) continue;
      totals[id] = (totals[id] ?? 0) + l.amountCents;
      names[id] = l.category;
    }
    if (totals.values.fold(0, (a, b) => a + b) != widget.amountCents) {
      setState(() {
        _busy = false;
        _error = 'Some lines have no category. Give every line one first.';
      });
      return;
    }

    try {
      await ref.read(apiProvider).refileTransaction(
            widget.transactionId,
            [
              for (final e in totals.entries)
                (categoryId: e.key, amountCents: e.value, description: names[e.key])
            ],
          );
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      ref.invalidate(reviewProvider);
      ref.invalidate(insightsProvider);
      if (!mounted) return;
      Navigator.of(context).pop(true);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('Split across ${totals.length} '
            'categor${totals.length == 1 ? "y" : "ies"}'),
      ));
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = e.message;
        });
      }
    }
  }

  Future<void> _editLine(int index) async {
    final cats = await ref.read(categoriesProvider.future);
    if (!mounted) return;
    final line = _lines[index];
    final result = await showModalBottomSheet<ScannedLine>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => _LineSheet(line: line, categories: cats),
    );
    if (result != null) setState(() => _lines[index] = result);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scan = _scan;

    return Scaffold(
      appBar: AppBar(
        title: Text(widget.alreadyFiled ? 'Split this charge' : 'Itemise'),
      ),
      bottomNavigationBar: scan == null
          ? null
          : SafeArea(
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    _BalanceBar(
                      sum: _sum,
                      target: widget.amountCents,
                      balances: _balances,
                    ),
                    const SizedBox(height: 8),
                    SizedBox(
                      width: double.infinity,
                      child: FilledButton(
                        onPressed: (_balances && !_busy) ? _submit : null,
                        child: Text(widget.alreadyFiled
                            ? 'Replace the current split'
                            : 'File these'),
                      ),
                    ),
                  ],
                ),
              ),
            ),
      body: _busy
          ? _Busy(stage: _stage)
          : scan == null
              ? _Start(
                  title: widget.title,
                  amountCents: widget.amountCents,
                  alreadyFiled: widget.alreadyFiled,
                  error: _error,
                  onCamera: () => _pick(ImageSource.camera),
                  onGallery: () => _pick(ImageSource.gallery),
                )
              : ListView(
                  padding: const EdgeInsets.fromLTRB(12, 12, 12, 24),
                  children: [
                    if (_error != null)
                      Card(
                        color: theme.colorScheme.errorContainer,
                        child: Padding(
                          padding: const EdgeInsets.all(12),
                          child: Text(_error!),
                        ),
                      ),
                    _SplitSummary(lines: _lines),
                    if (scan.remainderCents != 0) _RemainderNote(scan: scan),
                    const SizedBox(height: 12),
                    Text('Lines', style: theme.textTheme.titleSmall),
                    const SizedBox(height: 4),
                    for (var i = 0; i < _lines.length; i++)
                      _LineRow(
                        line: _lines[i],
                        onTap: () => _editLine(i),
                      ),
                    const SizedBox(height: 10),
                    Center(
                      child: Text(
                        'Read in ${scan.seconds.toStringAsFixed(0)}s '
                        '· ${scan.toolCalls} steps',
                        style: theme.textTheme.bodySmall,
                      ),
                    ),
                  ],
                ),
    );
  }
}

class _Start extends StatelessWidget {
  final String title;
  final int amountCents;
  final bool alreadyFiled;
  final String? error;
  final VoidCallback onCamera;
  final VoidCallback onGallery;

  const _Start({
    required this.title,
    required this.amountCents,
    required this.alreadyFiled,
    required this.error,
    required this.onCamera,
    required this.onGallery,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ListView(
      padding: const EdgeInsets.all(20),
      children: [
        Text(title, style: theme.textTheme.titleMedium),
        const SizedBox(height: 4),
        Text(Money.format(amountCents.abs()),
            style: theme.textTheme.headlineMedium),
        const SizedBox(height: 20),
        Text(
          alreadyFiled
              ? 'This charge is already filed in one place. Photograph the '
                  'receipt to split it across the envelopes it really covers.'
              : 'Photograph the receipt and each line will be read and '
                  'assigned to an envelope.',
          style: theme.textTheme.bodyMedium,
        ),
        const SizedBox(height: 8),
        Text(
          'The lines always add up to ${Money.format(amountCents.abs())} — '
          'anything that cannot be read is listed separately rather than '
          'quietly absorbed.',
          style: theme.textTheme.bodySmall,
        ),
        const SizedBox(height: 24),
        FilledButton.icon(
          onPressed: onCamera,
          icon: const Icon(Icons.photo_camera_outlined),
          label: const Text('Take a photo'),
        ),
        const SizedBox(height: 10),
        OutlinedButton.icon(
          onPressed: onGallery,
          icon: const Icon(Icons.photo_library_outlined),
          label: const Text('Choose a photo'),
        ),
        if (error != null) ...[
          const SizedBox(height: 16),
          Text(error!, style: TextStyle(color: theme.colorScheme.error)),
        ],
      ],
    );
  }
}

class _Busy extends StatelessWidget {
  final String stage;
  const _Busy({required this.stage});

  @override
  Widget build(BuildContext context) => Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const CircularProgressIndicator(),
            const SizedBox(height: 16),
            Text(stage),
            const SizedBox(height: 6),
            Text('Receipts take a few seconds to read.',
                style: Theme.of(context).textTheme.bodySmall),
          ],
        ),
      );
}

/// The category split, shown first because it is the thing that matters.
/// Individual line errors rarely change which envelope pays.
class _SplitSummary extends StatelessWidget {
  final List<ScannedLine> lines;
  const _SplitSummary({required this.lines});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final split = <String, int>{};
    for (final l in lines) {
      split[l.category] = (split[l.category] ?? 0) + l.amountCents;
    }
    final rows = split.entries.toList()
      ..sort((a, b) => b.value.abs().compareTo(a.value.abs()));
    final total = rows.fold(0, (a, r) => a + r.value);

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Where it goes', style: theme.textTheme.titleMedium),
            const SizedBox(height: 12),
            for (final r in rows)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 5),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Expanded(
                          child: Text(r.key,
                              maxLines: 1, overflow: TextOverflow.ellipsis),
                        ),
                        Text(Money.format(r.value),
                            style: theme.textTheme.bodyMedium),
                      ],
                    ),
                    const SizedBox(height: 4),
                    ClipRRect(
                      borderRadius: BorderRadius.circular(3),
                      child: LinearProgressIndicator(
                        value: total == 0 ? 0 : (r.value.abs() / total.abs()).clamp(0, 1),
                        minHeight: 6,
                        backgroundColor: theme.colorScheme.surfaceContainerHighest,
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _RemainderNote extends StatelessWidget {
  final ReceiptScan scan;
  const _RemainderNote({required this.scan});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      color: theme.colorScheme.surfaceContainerHighest,
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.info_outline, size: 18, color: theme.colorScheme.outline),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                '${Money.format(scan.remainderCents.abs())} could not be read '
                'off the receipt and is listed as its own line. The total '
                'still matches the charge — adjust that line if you know what '
                'it was.',
                style: theme.textTheme.bodySmall,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _LineRow extends StatelessWidget {
  final ScannedLine line;
  final VoidCallback onTap;
  const _LineRow({required this.line, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final unplaced = line.categoryId == null;
    return ListTile(
      dense: true,
      contentPadding: EdgeInsets.zero,
      onTap: onTap,
      title: Text(line.description,
          maxLines: 1, overflow: TextOverflow.ellipsis),
      subtitle: Row(
        children: [
          Flexible(
            child: Text(
              unplaced ? 'Tap to choose a category' : line.category,
              style: theme.textTheme.bodySmall?.copyWith(
                color: unplaced ? theme.colorScheme.error : null,
              ),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
          ),
          if (line.needsReview) ...[
            const SizedBox(width: 6),
            Icon(Icons.flag_outlined, size: 13, color: theme.colorScheme.error),
          ],
        ],
      ),
      trailing: Text(
        Money.format(line.amountCents),
        style: theme.textTheme.bodyMedium?.copyWith(
          fontFeatures: const [FontFeature.tabularFigures()],
        ),
      ),
    );
  }
}

class _BalanceBar extends StatelessWidget {
  final int sum;
  final int target;
  final bool balances;
  const _BalanceBar({
    required this.sum,
    required this.target,
    required this.balances,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final diff = target - sum;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: balances
            ? theme.colorScheme.surfaceContainerHighest
            : theme.colorScheme.errorContainer,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        children: [
          Icon(balances ? Icons.check_circle_outline : Icons.error_outline,
              size: 18,
              color: balances ? null : theme.colorScheme.onErrorContainer),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              balances
                  ? 'Lines add up to ${Money.format(target.abs())}'
                  : '${Money.format(diff.abs())} '
                      '${diff > 0 ? "missing" : "too much"}',
              style: theme.textTheme.bodyMedium,
            ),
          ),
        ],
      ),
    );
  }
}

/// Edit one line: its amount and its envelope.
class _LineSheet extends StatefulWidget {
  final ScannedLine line;
  final List<Category> categories;
  const _LineSheet({required this.line, required this.categories});

  @override
  State<_LineSheet> createState() => _LineSheetState();
}

class _LineSheetState extends State<_LineSheet> {
  late final _amount = TextEditingController(
      text: (widget.line.amountCents / 100).toStringAsFixed(2));
  late String? _categoryId = widget.line.categoryId;

  @override
  void dispose() {
    _amount.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final live = widget.categories.where((c) => !c.archived).toList();
    return Padding(
      padding: EdgeInsets.fromLTRB(
          16, 0, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(widget.line.description, style: theme.textTheme.titleMedium),
          const SizedBox(height: 12),
          TextField(
            controller: _amount,
            keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true),
            decoration: const InputDecoration(
              labelText: 'Amount',
              prefixText: r'$ ',
              border: OutlineInputBorder(),
              helperText: 'Negative for a refund or voided line',
            ),
          ),
          const SizedBox(height: 14),
          Text('Envelope', style: theme.textTheme.titleSmall),
          const SizedBox(height: 8),
          ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 220),
            child: SingleChildScrollView(
              child: Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final c in live)
                    ChoiceChip(
                      label: Text(c.name),
                      selected: _categoryId == c.id,
                      onSelected: (_) => setState(() => _categoryId = c.id),
                    ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              onPressed: () {
                final cents = Money.parse(_amount.text);
                if (cents == null) return;
                final cat = live.where((c) => c.id == _categoryId).firstOrNull;
                Navigator.of(context).pop(widget.line.copyWith(
                  amountCents: cents,
                  categoryId: cat?.id,
                  category: cat?.name ?? widget.line.category,
                ));
              },
              child: const Text('Save'),
            ),
          ),
        ],
      ),
    );
  }
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/money.dart';
import '../../core/theme.dart';
import '../../data/api_client.dart';
import '../../data/providers.dart';
import '../../data/receipt_ocr.dart';
import '../../domain/models.dart';

/// Itemise a receipt and file each line against a category.
///
/// Three stages, deliberately separate: get the text, read it into
/// items, decide where each one goes. The split is only submitted once
/// every line has a home and the amounts reconcile with the charge —
/// a half-filed receipt would leave the ledger disagreeing with the
/// bank, which is the one thing the ledger must never do.
class ReceiptScreen extends ConsumerStatefulWidget {
  /// The charge being itemised. Its amount is what the lines must add
  /// up to.
  final ReviewItem item;

  const ReceiptScreen({super.key, required this.item});

  @override
  ConsumerState<ReceiptScreen> createState() => _ReceiptScreenState();
}

class _ReceiptScreenState extends ConsumerState<ReceiptScreen> {
  final _ocr = ReceiptOcr();
  final _pasted = TextEditingController();

  File? _photo;
  ParsedReceipt? _receipt;
  bool _busy = false;
  String _stage = '';
  String? _error;

  /// Lines the user has not yet placed, by 1-based line number.
  final Map<int, String> _chosenId = {};
  final Map<int, String> _chosenName = {};

  /// Tax and tip are not line items but still have to land somewhere.
  String? _extrasCategoryId;
  String? _extrasCategoryName;

  @override
  void dispose() {
    _pasted.dispose();
    super.dispose();
  }

  int get _targetCents => widget.item.amountCents;

  int get _extrasCents =>
      (_receipt?.taxCents ?? 0) + (_receipt?.tipCents ?? 0);

  /// What the lines and extras currently account for.
  int get _placedCents {
    final r = _receipt;
    if (r == null) return 0;
    var sum = 0;
    for (var i = 0; i < r.items.length; i++) {
      if (_chosenId.containsKey(i + 1)) sum += r.items[i].amountCents;
    }
    if (_extrasCategoryId != null) sum += _extrasCents;
    return sum;
  }

  int get _unplacedCount {
    final r = _receipt;
    if (r == null) return 0;
    var n = 0;
    for (var i = 0; i < r.items.length; i++) {
      if (!_chosenId.containsKey(i + 1)) n++;
    }
    if (_extrasCents != 0 && _extrasCategoryId == null) n++;
    return n;
  }

  /// The gap between what the receipt accounts for and what the bank
  /// charged. Must be zero before anything is filed.
  int get _differenceCents => _targetCents - _placedCents;

  bool get _canSubmit =>
      _receipt != null && _unplacedCount == 0 && _differenceCents == 0;

  // ---- getting the text ------------------------------------------- //

  Future<void> _fromPhoto({required bool camera}) async {
    setState(() {
      _busy = true;
      _error = null;
      _stage = camera ? 'Opening the camera…' : 'Opening your photos…';
    });
    try {
      final file = await _ocr.pick(fromCamera: camera);
      if (file == null) {
        if (mounted) setState(() => _busy = false);
        return;
      }
      if (mounted) {
        setState(() {
          _photo = file;
          _stage = 'Reading the receipt…';
        });
      }
      final text = await _ocr.read(file);
      if (text.trim().isEmpty) {
        if (mounted) {
          setState(() {
            _busy = false;
            _error = 'No text could be read from that photo. Try again with '
                'the receipt flat, filling the frame, in even light — or '
                'type it in below.';
          });
        }
        return;
      }
      _pasted.text = text;
      await _parse(text);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _parse(String text) async {
    setState(() {
      _busy = true;
      _error = null;
      _stage = 'Working out the items…';
    });
    try {
      final parsed = await ref.read(apiProvider).parseReceipt(
            text: text,
            expectedCents: _targetCents,
            onProgress: (j) {
              if (mounted) setState(() => _stage = j.progressLabel);
            },
          );
      if (!mounted) return;
      setState(() {
        _receipt = parsed;
        _chosenId.clear();
        _chosenName.clear();
      });
      if (parsed.items.isNotEmpty) await _suggestCategories();
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _suggestCategories() async {
    final r = _receipt;
    if (r == null) return;
    setState(() {
      _busy = true;
      _stage = 'Sorting the items…';
    });
    try {
      final picks = await ref.read(apiProvider).categoriseItems(
            merchant: r.merchant.isEmpty ? widget.item.title : r.merchant,
            items: r.items,
          );
      if (!mounted) return;
      setState(() {
        for (final e in picks.entries) {
          _chosenId[e.key] = e.value.$1;
          _chosenName[e.key] = e.value.$2;
        }
        // Tax and tip follow wherever most of the receipt went.
        if (_extrasCents != 0 && _chosenId.isNotEmpty) {
          final tally = <String, int>{};
          for (final id in _chosenId.values) {
            tally[id] = (tally[id] ?? 0) + 1;
          }
          final top = tally.entries.reduce((a, b) => a.value >= b.value ? a : b);
          _extrasCategoryId = top.key;
          _extrasCategoryName = _chosenName.entries
              .firstWhere((e) => _chosenId[e.key] == top.key)
              .value;
        }
      });
    } catch (e) {
      // Not fatal: every line can still be placed by hand.
      if (mounted) setState(() => _error = 'Could not sort the items: $e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  // ---- placing lines ----------------------------------------------- //

  Future<void> _choose({int? line, bool extras = false}) async {
    final cats = await ref.read(categoriesProvider.future);
    if (!mounted) return;
    final picked = await showModalBottomSheet<Category>(
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
                onTap: () => Navigator.pop(ctx, c),
              ),
          ],
        ),
      ),
    );
    if (picked == null) return;
    setState(() {
      if (extras) {
        _extrasCategoryId = picked.id;
        _extrasCategoryName = picked.name;
      } else if (line != null) {
        _chosenId[line] = picked.id;
        _chosenName[line] = picked.name;
      }
    });
  }

  /// Put everything in one place, for a receipt that is all one thing.
  Future<void> _assignAll() async {
    final cats = await ref.read(categoriesProvider.future);
    if (!mounted) return;
    final picked = await showModalBottomSheet<Category>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          children: [
            for (final c in cats)
              ListTile(
                title: Text(c.name),
                onTap: () => Navigator.pop(ctx, c),
              ),
          ],
        ),
      ),
    );
    if (picked == null) return;
    final r = _receipt;
    if (r == null) return;
    setState(() {
      for (var i = 1; i <= r.items.length; i++) {
        _chosenId[i] = picked.id;
        _chosenName[i] = picked.name;
      }
      if (_extrasCents != 0) {
        _extrasCategoryId = picked.id;
        _extrasCategoryName = picked.name;
      }
    });
  }

  Future<void> _submit() async {
    final r = _receipt;
    if (r == null || !_canSubmit) return;
    setState(() {
      _busy = true;
      _stage = 'Filing…';
    });

    // Group by category: several lines in one envelope is one entry.
    final totals = <String, int>{};
    for (var i = 0; i < r.items.length; i++) {
      final id = _chosenId[i + 1];
      if (id == null) continue;
      totals[id] = (totals[id] ?? 0) + r.items[i].amountCents;
    }
    if (_extrasCategoryId != null && _extrasCents != 0) {
      totals[_extrasCategoryId!] =
          (totals[_extrasCategoryId!] ?? 0) + _extrasCents;
    }

    try {
      await ref.read(apiProvider).resolveReview(
            widget.item.id,
            [
              for (final e in totals.entries)
                (categoryId: e.key, amountCents: e.value)
            ],
          );
      ref.invalidate(reviewProvider);
      ref.invalidate(summaryProvider);
      ref.invalidate(categoriesProvider);
      if (!mounted) return;
      Navigator.of(context).pop(true);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('${r.items.length} items filed across '
            '${totals.length} categor${totals.length == 1 ? "y" : "ies"}'),
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
    final r = _receipt;

    return Scaffold(
      appBar: AppBar(title: const Text('Itemise')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Row(children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(widget.item.title,
                          style: theme.textTheme.titleMedium
                              ?.copyWith(fontWeight: FontWeight.w700)),
                      Text(widget.item.date,
                          style: theme.textTheme.bodySmall?.copyWith(
                              color: theme.colorScheme.onSurfaceVariant)),
                    ],
                  ),
                ),
                Text(MoneyFlowFormat.flow(_targetCents),
                    style: theme.textTheme.titleLarge
                        ?.copyWith(fontWeight: FontWeight.w700)),
              ]),
            ),
          ),
          const SizedBox(height: 16),

          if (r == null) ...[
            Text('Where is the receipt?',
                style: theme.textTheme.titleMedium
                    ?.copyWith(fontWeight: FontWeight.w600)),
            const SizedBox(height: 4),
            Text(
              'The photo is read on this phone and never uploaded. Only '
              'the text goes to the assistant.',
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
            const SizedBox(height: 12),
            Row(children: [
              Expanded(
                child: FilledButton.icon(
                  onPressed: _busy ? null : () => _fromPhoto(camera: true),
                  icon: const Icon(Icons.photo_camera_outlined),
                  label: const Text('Take a photo'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: _busy ? null : () => _fromPhoto(camera: false),
                  icon: const Icon(Icons.photo_library_outlined),
                  label: const Text('Choose one'),
                ),
              ),
            ]),
            const SizedBox(height: 20),
            TextField(
              controller: _pasted,
              maxLines: 8,
              minLines: 4,
              decoration: const InputDecoration(
                labelText: 'Or paste the receipt',
                hintText: 'Typed, emailed, or copied from a photo.',
                alignLabelWithHint: true,
              ),
            ),
            const SizedBox(height: 12),
            FilledButton.tonal(
              onPressed: _busy || _pasted.text.trim().isEmpty
                  ? null
                  : () => _parse(_pasted.text),
              child: const Text('Read these items'),
            ),
          ],

          if (_busy) ...[
            const SizedBox(height: 24),
            Column(children: [
              const CircularProgressIndicator(),
              const SizedBox(height: 12),
              Text(_stage,
                  style: theme.textTheme.bodyMedium?.copyWith(
                      color: theme.colorScheme.onSurfaceVariant)),
            ]),
          ],

          if (_error != null) ...[
            const SizedBox(height: 16),
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
          ],

          if (r != null) ...[
            for (final n in r.notices)
              Container(
                width: double.infinity,
                margin: const EdgeInsets.only(bottom: 10),
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: theme.colorScheme.errorContainer,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Icon(Icons.warning_amber_rounded, size: 18,
                          color: theme.colorScheme.onErrorContainer),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(n,
                            style: TextStyle(
                                color: theme.colorScheme.onErrorContainer)),
                      ),
                    ]),
              ),

            if (_photo != null) ...[
              ClipRRect(
                borderRadius: BorderRadius.circular(12),
                child: Image.file(_photo!,
                    height: 120, width: double.infinity, fit: BoxFit.cover),
              ),
              const SizedBox(height: 12),
            ],
            Row(children: [
              Expanded(
                child: Text('${r.items.length} items',
                    style: theme.textTheme.titleMedium
                        ?.copyWith(fontWeight: FontWeight.w600)),
              ),
              TextButton(
                onPressed: _busy ? null : _assignAll,
                child: const Text('All one category'),
              ),
            ]),
            const SizedBox(height: 4),

            Card(
              child: Column(children: [
                for (var i = 0; i < r.items.length; i++) ...[
                  if (i > 0) const Divider(height: 1, indent: 16, endIndent: 16),
                  _ItemRow(
                    item: r.items[i],
                    categoryName: _chosenName[i + 1],
                    onTap: _busy ? null : () => _choose(line: i + 1),
                  ),
                ],
                if (_extrasCents != 0) ...[
                  const Divider(height: 1, indent: 16, endIndent: 16),
                  _ItemRow(
                    item: ReceiptItem(
                      description: r.tipCents != 0 ? 'Tax and tip' : 'Tax',
                      amountCents: _extrasCents,
                    ),
                    categoryName: _extrasCategoryName,
                    onTap: _busy ? null : () => _choose(extras: true),
                  ),
                ],
              ]),
            ),

            const SizedBox(height: 16),
            _Reconciliation(
              targetCents: _targetCents,
              placedCents: _placedCents,
              unplaced: _unplacedCount,
            ),
            const SizedBox(height: 16),
            FilledButton(
              onPressed: _canSubmit && !_busy ? _submit : null,
              child: Text(_canSubmit
                  ? 'File these ${r.items.length} items'
                  : _unplacedCount > 0
                      ? '$_unplacedCount still to place'
                      : 'The amounts do not add up yet'),
            ),
            const SizedBox(height: 8),
            TextButton(
              onPressed: _busy
                  ? null
                  : () => setState(() {
                        _receipt = null;
                        _photo = null;
                        _error = null;
                      }),
              child: const Text('Start over'),
            ),
          ],
        ],
      ),
    );
  }
}

class _ItemRow extends StatelessWidget {
  final ReceiptItem item;
  final String? categoryName;
  final VoidCallback? onTap;

  const _ItemRow({required this.item, this.categoryName, this.onTap});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final placed = categoryName != null;
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Row(children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  item.quantity > 1
                      ? '${item.description}  ×${item.quantity}'
                      : item.description,
                  style: theme.textTheme.bodyLarge,
                  overflow: TextOverflow.ellipsis,
                ),
                const SizedBox(height: 2),
                Row(children: [
                  Icon(placed ? Icons.check_circle : Icons.help_outline,
                      size: 13,
                      color: placed
                          ? AppTheme.healthy
                          : theme.colorScheme.onSurfaceVariant),
                  const SizedBox(width: 4),
                  Text(categoryName ?? 'tap to choose',
                      style: theme.textTheme.bodySmall?.copyWith(
                        color: placed
                            ? theme.colorScheme.onSurface
                            : theme.colorScheme.onSurfaceVariant,
                        fontStyle: placed ? null : FontStyle.italic,
                      )),
                ]),
              ],
            ),
          ),
          const SizedBox(width: 8),
          Text(
            item.isCredit
                ? '−${Money.format(item.amountCents.abs())}'
                : Money.format(item.amountCents),
            style: theme.textTheme.titleSmall?.copyWith(
              fontWeight: FontWeight.w600,
              color: item.isCredit ? AppTheme.positive(context) : null,
            ),
          ),
        ]),
      ),
    );
  }
}

/// The parts against the whole.
///
/// Nothing is filed until this balances: the review endpoint refuses a
/// partial split, and rightly — the ledger has to agree with the bank.
class _Reconciliation extends StatelessWidget {
  final int targetCents;
  final int placedCents;
  final int unplaced;

  const _Reconciliation({
    required this.targetCents,
    required this.placedCents,
    required this.unplaced,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final diff = targetCents - placedCents;
    final balanced = diff == 0 && unplaced == 0;

    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: balanced
            ? AppTheme.healthy.withValues(alpha: 0.12)
            : theme.colorScheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(children: [
        _row(theme, 'The charge', Money.format(targetCents)),
        const SizedBox(height: 4),
        _row(theme, 'Placed so far', Money.format(placedCents)),
        if (diff != 0) ...[
          const Divider(height: 16),
          _row(
            theme,
            diff > 0 ? 'Still to account for' : 'Over by',
            Money.format(diff.abs()),
            emphasis: true,
            colour: theme.colorScheme.error,
          ),
        ],
        if (balanced) ...[
          const SizedBox(height: 6),
          Row(mainAxisAlignment: MainAxisAlignment.center, children: [
            Icon(Icons.check_circle, size: 16, color: AppTheme.healthy),
            const SizedBox(width: 6),
            Text('Adds up exactly',
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: AppTheme.healthy)),
          ]),
        ],
      ]),
    );
  }

  Widget _row(ThemeData theme, String label, String value,
      {bool emphasis = false, Color? colour}) {
    return Row(children: [
      Text(label,
          style: theme.textTheme.bodyMedium?.copyWith(
              color: colour ?? theme.colorScheme.onSurfaceVariant)),
      const Spacer(),
      Text(value,
          style: theme.textTheme.bodyMedium?.copyWith(
            fontWeight: emphasis ? FontWeight.w700 : FontWeight.w600,
            color: colour,
          )),
    ]);
  }
}

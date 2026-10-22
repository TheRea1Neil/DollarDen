import 'package:flutter/material.dart';

import '../../core/money.dart';
import '../../domain/models.dart';

/// Edit one category's funding rule.
///
/// Each rule type needs different fields, so the form changes with the
/// type rather than showing every field greyed out. The plain-English
/// line under the picker is the point of the whole widget: ADR 0006 found
/// that the mistakes people and models both make here are inverted
/// comparisons and unset thresholds, and neither is visible in JSON.
class FundingEditor extends StatefulWidget {
  final Funding? initial;
  final int categoryCount;

  /// Called with (ruleType, params, position, complete). `complete` is
  /// false while a required field is still blank, so the caller can
  /// refuse to save a rule the engine could not run.
  final void Function(String?, Map<String, dynamic>, int?, bool) onChanged;

  const FundingEditor({
    super.key,
    required this.initial,
    required this.categoryCount,
    required this.onChanged,
  });

  @override
  State<FundingEditor> createState() => _FundingEditorState();
}

class _FundingEditorState extends State<FundingEditor> {
  late String _type;
  late TextEditingController _rate;
  late TextEditingController _amount;
  late TextEditingController _target;
  late TextEditingController _cap;
  late TextEditingController _threshold;
  late TextEditingController _thenAmount;
  late String _of;
  late String _op;
  int? _position;

  static const _none = 'none';

  @override
  void initState() {
    super.initState();
    final f = widget.initial;
    _type = f?.ruleType ?? _none;
    _of = f?.of ?? 'gross';
    _op = f?.whenOp ?? '<';
    _position = f?.position;

    String money(int? c) => c == null ? '' : (c / 100).toStringAsFixed(2);
    // A percent rule stores "0.10"; people think in "10".
    final r = f?.rate;
    _rate = TextEditingController(
        text: r == null ? '' : _pctText(r));
    _amount = TextEditingController(text: money(f?.amountCents));
    _target = TextEditingController(text: money(f?.targetCents));
    _cap = TextEditingController(text: money(f?.maxContributionCents));
    _threshold = TextEditingController(text: money(f?.whenValueCents));
    _thenAmount = TextEditingController(text: money(f?.thenAmountCents));

    for (final c in [_rate, _amount, _target, _cap, _threshold, _thenAmount]) {
      c.addListener(_emit);
    }
    // Emit once, before anything is touched. Without this the parent
    // holds whatever it guessed at construction, and a save that
    // happens to not trigger a listener silently writes the OLD rule
    // back -- which looks exactly like "I had to do it twice".
    WidgetsBinding.instance.addPostFrameCallback((_) => _emit());
  }

  static String _pctText(String rate) {
    final v = double.tryParse(rate);
    if (v == null) return rate;
    final pct = v * 100;
    return pct == pct.roundToDouble()
        ? pct.round().toString()
        : pct.toStringAsFixed(2);
  }

  @override
  void dispose() {
    for (final c in [_rate, _amount, _target, _cap, _threshold, _thenAmount]) {
      c.dispose();
    }
    super.dispose();
  }

  int? _cents(TextEditingController c) {
    final t = c.text.trim().replaceAll(',', '').replaceAll('\$', '');
    if (t.isEmpty) return null;
    final v = double.tryParse(t);
    return v == null ? null : (v * 100).round();
  }

  Map<String, dynamic> _params() {
    switch (_type) {
      case 'percent':
        final pct = double.tryParse(_rate.text.trim());
        return {
          'rate': pct == null ? '' : (pct / 100).toStringAsFixed(4),
          'of': _of,
        };
      case 'fixed':
        return {'amount_cents': _cents(_amount)};
      case 'refill_to':
        final cap = _cents(_cap);
        return {
          'target_cents': _cents(_target),
          if (cap != null) 'max_contribution_cents': cap,
        };
      case 'conditional':
        return {
          'when': {
            'field': 'balance',
            'op': _op,
            'value_cents': _cents(_threshold),
          },
          'then': {
            'rule_type': 'fixed',
            'params': {'amount_cents': _cents(_thenAmount)},
          },
        };
      default:
        return {};
    }
  }

  /// Whether every field this rule type needs has a value.
  bool get _complete {
    switch (_type) {
      case _none:
      case 'remainder':
        return true;
      case 'percent':
        final v = double.tryParse(_rate.text.trim());
        return v != null && v > 0;
      case 'fixed':
        return (_cents(_amount) ?? 0) > 0;
      case 'refill_to':
        return (_cents(_target) ?? 0) > 0;
      case 'conditional':
        return (_cents(_threshold) ?? -1) >= 0 && (_cents(_thenAmount) ?? 0) > 0;
    }
    return false;
  }

  void _emit() => widget.onChanged(
      _type == _none ? null : _type, _params(), _position, _complete);

  /// The same sentence the API renders, built locally so it updates as
  /// the user types rather than only after saving.
  String get _summary {
    switch (_type) {
      case _none:
        return 'Nothing is added automatically. You allocate to it by hand.';
      case 'percent':
        final p = _rate.text.trim();
        if (p.isEmpty) return 'Set a percentage.';
        final base = _of == 'remaining'
            ? 'what is left when its turn comes'
            : 'your gross pay';
        return 'Adds $p% of $base, every paycheck.';
      case 'fixed':
        final c = _cents(_amount);
        return c == null
            ? 'Set an amount.'
            : 'Adds ${Money.format(c)} every paycheck.';
      case 'refill_to':
        final t = _cents(_target);
        if (t == null) return 'Set the amount a full envelope holds.';
        final cap = _cents(_cap);
        final base = 'Tops it back up to ${Money.format(t)} each paycheck, '
            'adding only what is missing';
        return cap == null
            ? '$base.'
            : '$base — but never more than ${Money.format(cap)} at once, so a '
                'big shortfall fills over several paychecks.';
      case 'conditional':
        final v = _cents(_threshold);
        final a = _cents(_thenAmount);
        if (v == null || a == null) {
          return 'Set both the threshold and the amount.';
        }
        const words = {
          '<': 'less than', '<=': 'at most', '>': 'more than',
          '>=': 'at least', '==': 'exactly',
        };
        return 'Adds ${Money.format(a)} each paycheck, but only while this '
            'category holds ${words[_op]} ${Money.format(v)}.';
      case 'remainder':
        return 'Takes whatever is left after every category above it. '
            'Only one category can do this, and it is always funded last.';
    }
    return '';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        DropdownButtonFormField<String>(
          initialValue: _type,
          decoration: const InputDecoration(labelText: 'How it gets funded'),
          items: const [
            DropdownMenuItem(value: _none, child: Text('By hand only')),
            DropdownMenuItem(value: 'fixed', child: Text('A fixed amount')),
            DropdownMenuItem(value: 'percent', child: Text('A percentage of pay')),
            DropdownMenuItem(value: 'refill_to', child: Text('Top up to a target')),
            DropdownMenuItem(
                value: 'conditional', child: Text('Only below a threshold')),
            DropdownMenuItem(
                value: 'remainder', child: Text('Everything left over')),
          ],
          onChanged: (v) {
            if (v == null) return;
            setState(() => _type = v);
            _emit();
          },
        ),
        const SizedBox(height: 12),

        if (_type == 'percent') ...[
          Row(children: [
            Expanded(
              child: TextField(
                controller: _rate,
                keyboardType: const TextInputType.numberWithOptions(decimal: true),
                decoration: const InputDecoration(
                    labelText: 'Percentage', suffixText: '%'),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: DropdownButtonFormField<String>(
                initialValue: _of,
                decoration: const InputDecoration(labelText: 'Of'),
                items: const [
                  DropdownMenuItem(value: 'gross', child: Text('Gross pay')),
                  DropdownMenuItem(
                      value: 'remaining', child: Text("What's left")),
                ],
                onChanged: (v) {
                  setState(() => _of = v ?? _of);
                  _emit();
                },
              ),
            ),
          ]),
        ],

        if (_type == 'fixed')
          TextField(
            controller: _amount,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            decoration: const InputDecoration(
                labelText: 'Amount per paycheck', prefixText: '\$'),
          ),

        if (_type == 'refill_to') ...[
          TextField(
            controller: _target,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            decoration: const InputDecoration(
              labelText: 'Full envelope holds',
              prefixText: '\$',
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _cap,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            decoration: const InputDecoration(
              labelText: 'Cap per paycheck (optional)',
              prefixText: '\$',
              helperText: 'Spreads a big shortfall over several paychecks '
                  'instead of swallowing one whole.',
              helperMaxLines: 3,
            ),
          ),
        ],

        if (_type == 'conditional') ...[
          Row(children: [
            Expanded(
              flex: 4,
              child: DropdownButtonFormField<String>(
                initialValue: _op,
                decoration: const InputDecoration(labelText: 'Only while it holds'),
                items: const [
                  DropdownMenuItem(value: '<', child: Text('less than')),
                  DropdownMenuItem(value: '<=', child: Text('at most')),
                  DropdownMenuItem(value: '>', child: Text('more than')),
                  DropdownMenuItem(value: '>=', child: Text('at least')),
                  DropdownMenuItem(value: '==', child: Text('exactly')),
                ],
                onChanged: (v) {
                  setState(() => _op = v ?? _op);
                  _emit();
                },
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              flex: 3,
              child: TextField(
                controller: _threshold,
                keyboardType:
                    const TextInputType.numberWithOptions(decimal: true),
                decoration: const InputDecoration(
                    labelText: 'Threshold', prefixText: '\$'),
              ),
            ),
          ]),
          const SizedBox(height: 12),
          TextField(
            controller: _thenAmount,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            decoration: const InputDecoration(
                labelText: 'Then add', prefixText: '\$'),
          ),
        ],

        const SizedBox(height: 12),
        // Always visible, always current. An inverted comparison reads
        // obviously wrong here in a way it never does as {"op": "<"}.
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: theme.colorScheme.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(12),
          ),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Icon(Icons.subdirectory_arrow_right,
                size: 18, color: theme.colorScheme.onSurfaceVariant),
            const SizedBox(width: 8),
            Expanded(
              child: Text(_summary, style: theme.textTheme.bodyMedium),
            ),
          ]),
        ),

        if (_type != _none && _type != 'remainder') ...[
          const SizedBox(height: 16),
          Text('Funded ${_position ?? widget.categoryCount} of '
              '${widget.categoryCount}',
              style: theme.textTheme.labelLarge
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          Text(
            'Earlier means it gets paid first when a paycheck is short.',
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
          ),
          Slider(
            value: (_position ?? widget.categoryCount)
                .clamp(1, widget.categoryCount)
                .toDouble(),
            min: 1,
            max: widget.categoryCount.toDouble(),
            divisions: widget.categoryCount > 1 ? widget.categoryCount - 1 : null,
            label: '${_position ?? widget.categoryCount}',
            onChanged: widget.categoryCount < 2
                ? null
                : (v) {
                    setState(() => _position = v.round());
                    _emit();
                  },
          ),
        ],
      ],
    );
  }
}

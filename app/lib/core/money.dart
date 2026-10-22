import 'package:intl/intl.dart';

/// Money is integer cents everywhere, matching the backend (ADR 0002).
///
/// Nothing in this app stores a monetary value as a double. Conversion to a
/// display string happens here and nowhere else, so a rounding mistake has
/// exactly one place to hide.
class Money {
  const Money._();

  static final NumberFormat _full = NumberFormat.currency(symbol: r'$');
  static final NumberFormat _compact = NumberFormat.currency(symbol: r'$', decimalDigits: 0);

  /// `421000` -> `$4,210.00`
  static String format(int cents) => _full.format(cents / 100);

  /// `421000` -> `$4,210` — for dense lists where cents are noise.
  static String formatCompact(int cents) => _compact.format(cents / 100);

  /// Signed, for ledger entries: `+$50.00` / `-$12.99`.
  static String formatSigned(int cents) {
    final sign = cents > 0 ? '+' : '';
    return '$sign${format(cents)}';
  }

  /// Parse user input into cents. Returns null if it is not a valid amount.
  ///
  /// Goes through the string rather than double arithmetic: `19.99 * 100`
  /// is `1998.9999...` in binary floating point, and truncating that loses
  /// a cent.
  static int? parse(String input) {
    final cleaned = input.replaceAll(RegExp(r'[^0-9.\-]'), '').trim();
    if (cleaned.isEmpty || cleaned == '-' || cleaned == '.') return null;

    final negative = cleaned.startsWith('-');
    final digits = negative ? cleaned.substring(1) : cleaned;

    final parts = digits.split('.');
    if (parts.length > 2) return null;

    final whole = parts[0].isEmpty ? '0' : parts[0];
    var frac = parts.length == 2 ? parts[1] : '';
    if (frac.length > 2) return null;
    frac = frac.padRight(2, '0');

    final value = int.tryParse(whole);
    final fracValue = int.tryParse(frac.isEmpty ? '0' : frac);
    if (value == null || fracValue == null) return null;

    final cents = value * 100 + fracValue;
    return negative ? -cents : cents;
  }
}

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

/// Which way money moved, for a *budget-signed* amount.
///
/// The backend's convention is `positive == money OUT` (outflow - inflow),
/// which is the opposite of what a person expects from a leading minus
/// sign: a $20 refund arrives as `-2000` and would render as "-$20.00",
/// reading like a loss. Every screen that shows one of these amounts goes
/// through here so the convention is inverted in exactly one place.
enum MoneyFlow {
  /// Spending. Reduces the category it is filed against.
  out,

  /// A refund, or income. Increases the category.
  into,

  /// Exactly zero — rare, but it must not be coloured as either.
  none;

  static MoneyFlow ofSigned(int signedCents) => signedCents > 0
      ? MoneyFlow.out
      : signedCents < 0
          ? MoneyFlow.into
          : MoneyFlow.none;

  bool get isOut => this == MoneyFlow.out;
  bool get isInto => this == MoneyFlow.into;

  /// "takes out of" / "adds to" — the phrase a category screen needs.
  String get verb => switch (this) {
        MoneyFlow.out => 'takes out of',
        MoneyFlow.into => 'adds to',
        MoneyFlow.none => 'does not change',
      };

  String get noun => switch (this) {
        MoneyFlow.out => 'Spending',
        MoneyFlow.into => 'Money in',
        MoneyFlow.none => 'No change',
      };
}

extension MoneyFlowFormat on Money {
  /// A budget-signed amount rendered the way a person reads it:
  /// spending as `−$14.50`, a refund as `+$20.00`.
  static String flow(int signedCents) {
    if (signedCents == 0) return Money.format(0);
    final magnitude = Money.format(signedCents.abs());
    // Minus for money leaving, plus for money arriving — the inverse of
    // the stored sign.
    return signedCents > 0 ? '−$magnitude' : '+$magnitude';
  }
}

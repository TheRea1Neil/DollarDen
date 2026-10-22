import 'package:flutter/material.dart';

/// A calm, legible palette. Money apps are read quickly and often under mild
/// stress, so the colour vocabulary is small and consistent:
///
///   green  = healthy / funded
///   amber  = approaching a threshold
///   red    = overspent
///
/// Those three meanings are never used for anything else.
class AppTheme {
  static const seed = Color(0xFF2E7D6B);

  static const healthy = Color(0xFF2E7D32);
  static const warning = Color(0xFFB26A00);
  static const danger = Color(0xFFC62828);

  static ThemeData light() => _build(Brightness.light);
  static ThemeData dark() => _build(Brightness.dark);

  static ThemeData _build(Brightness brightness) {
    final scheme = ColorScheme.fromSeed(seedColor: seed, brightness: brightness);
    return ThemeData(
      useMaterial3: true,
      colorScheme: scheme,
      scaffoldBackgroundColor: scheme.surface,
      appBarTheme: AppBarTheme(
        backgroundColor: scheme.surface,
        surfaceTintColor: Colors.transparent,
        centerTitle: false,
        elevation: 0,
        titleTextStyle: TextStyle(
          color: scheme.onSurface,
          fontSize: 22,
          fontWeight: FontWeight.w600,
        ),
      ),
      cardTheme: CardThemeData(
        elevation: 0,
        color: scheme.surfaceContainerLow,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
        margin: EdgeInsets.zero,
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: scheme.surfaceContainerHighest.withValues(alpha: 0.4),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: BorderSide.none,
        ),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          minimumSize: const Size.fromHeight(50),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
      ),
    );
  }

  /// Money arriving — a refund or income. Deliberately the same green as
  /// a healthy balance, since both mean "this category is better off".
  /// Readable on both light and dark surfaces.
  static Color positive(BuildContext context) =>
      Theme.of(context).brightness == Brightness.dark
          ? const Color(0xFF6ABF69)
          : healthy;

  /// Status colour for a category balance. Overspent beats low, because a
  /// negative balance is the more urgent fact.
  static Color statusColor(BuildContext context,
      {required bool overspent, required bool belowAlert}) {
    if (overspent) return danger;
    if (belowAlert) return warning;
    return healthy;
  }
}

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
      // Size.fromHeight(50) is Size(double.infinity, 50): every filled
      // button fills its parent's width. Right in a Column, fatal in a
      // Row -- see [inlineFilled] at the bottom of this file.
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

  /// Stops for the fullness ramp, from empty to full.
  ///
  /// Chosen so lightness falls as the envelope empties -- a light green
  /// when there is plenty, a deep red when there is nothing. That makes
  /// the bar readable as "getting worse" even where the hue difference
  /// is hard to see, which matters for red-green colour blindness and
  /// in bright sunlight.
  static const _fullnessStops = <(double, Color)>[
    (0.00, Color(0xFFB71C1C)), // deep red  — empty
    (0.15, Color(0xFFE64A19)), // deep orange
    (0.30, Color(0xFFF57C00)), // orange
    (0.50, Color(0xFFFBC02D)), // yellow
    (0.70, Color(0xFF9CCC65)), // yellow-green
    (1.00, Color(0xFF66BB6A)), // light green — full
  ];

  /// A continuous colour for how full an envelope is, 0 empty to 1 full.
  ///
  /// Interpolated between neighbouring stops rather than switched at
  /// thresholds, so spending gradually shifts the bar instead of
  /// snapping from green to red at one invisible boundary. Several
  /// close stops beat two distant ones: a straight red-to-green lerp
  /// passes through a muddy brown around the middle.
  static Color fullnessColor(double ratio) {
    final r = ratio.isNaN ? 0.0 : ratio.clamp(0.0, 1.0);
    for (var i = 0; i < _fullnessStops.length - 1; i++) {
      final (lo, loColour) = _fullnessStops[i];
      final (hi, hiColour) = _fullnessStops[i + 1];
      if (r <= hi) {
        final t = hi == lo ? 0.0 : (r - lo) / (hi - lo);
        return Color.lerp(loColour, hiColour, t)!;
      }
    }
    return _fullnessStops.last.$2;
  }
}

/// The style a FilledButton needs when it sits inside a Row.
///
/// The theme gives every filled button an infinite minimum WIDTH so it fills
/// a Column. A Row hands its children unbounded width, so that same style
/// trips `BoxConstraints forces an infinite width` during layout.
///
/// That failure is worse than it sounds: the assertion aborts layout for the
/// whole subtree, so the screen renders BLANK -- no error, no partial UI,
/// nothing in release logs. It shipped. Any user whose setup was unfinished
/// got an empty Budget tab, because SetupGuide puts a FilledButton in a Row
/// and only appears before setup is complete.
///
/// Keeps the 50px height; drops only the infinite width.
ButtonStyle inlineFilled() => FilledButton.styleFrom(minimumSize: const Size(0, 50));

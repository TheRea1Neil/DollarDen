import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:dollarden/core/theme.dart';

/// The bar must change gradually. A threshold switch is what it replaced:
/// green until some invisible line, then red, which tells you nothing
/// about getting closer to it.
void main() {
  group('fullness ramp', () {
    test('ends at deep red and light green', () {
      expect(AppTheme.fullnessColor(0).toARGB32(), 0xFFB71C1C);
      expect(AppTheme.fullnessColor(1).toARGB32(), 0xFF66BB6A);
    });

    test('it is continuous — no step bigger than a small nudge', () {
      Color? prev;
      var worst = 0;
      for (var i = 0; i <= 100; i++) {
        final c = AppTheme.fullnessColor(i / 100);
        if (prev != null) {
          final d = (c.r - prev.r).abs() + (c.g - prev.g).abs() + (c.b - prev.b).abs();
          worst = worst > (d * 255).round() ? worst : (d * 255).round();
        }
        prev = c;
      }
      // A visible jump would be the bug; 1% of the range should be subtle.
      expect(worst, lessThan(20),
          reason: 'a $worst/255 jump between adjacent steps would read as a snap');
    });

    test('hue climbs red -> orange -> yellow -> green, without turning back', () {
      // Hue is the invariant, not any single channel. Yellow has a
      // HIGHER green channel than green does, and is brighter than
      // both neighbours, so asserting on g or on luminance fails on
      // correct output -- which is how these two tests were written
      // the first time.
      double hue(double r) => HSLColor.fromColor(AppTheme.fullnessColor(r)).hue;
      var prev = hue(0);
      for (var i = 1; i <= 100; i++) {
        final h = hue(i / 100);
        expect(h, greaterThanOrEqualTo(prev - 0.01),
            reason: 'hue went backwards at ${i / 100}: $prev -> $h');
        prev = h;
      }
      expect(hue(0), lessThan(20));    // red
      expect(hue(1), greaterThan(90)); // green
    });

    test('it gets lighter as it drains toward empty', () {
      // True across the half that matters -- the warning range. Beyond
      // yellow it reverses, because yellow is the brightest hue there
      // is; claiming otherwise would be asserting a falsehood.
      double lum(Color c) => 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b;
      expect(lum(AppTheme.fullnessColor(0.0)),
          lessThan(lum(AppTheme.fullnessColor(0.3))));
      expect(lum(AppTheme.fullnessColor(0.3)),
          lessThan(lum(AppTheme.fullnessColor(0.5))));
    });

    test('out-of-range and NaN are clamped, not crashes', () {
      expect(AppTheme.fullnessColor(-5).toARGB32(), 0xFFB71C1C);
      expect(AppTheme.fullnessColor(42).toARGB32(), 0xFF66BB6A);
      expect(AppTheme.fullnessColor(double.nan).toARGB32(), 0xFFB71C1C);
    });
  });
}

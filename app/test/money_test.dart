import 'package:flutter_test/flutter_test.dart';
import 'package:nnf/core/money.dart';

void main() {
  group('Money.format', () {
    test('renders cents as currency', () {
      expect(Money.format(0), r'$0.00');
      expect(Money.format(1), r'$0.01');
      expect(Money.format(421000), r'$4,210.00');
      expect(Money.format(-120000), r'-$1,200.00');
    });

    test('signed form marks inflows', () {
      expect(Money.formatSigned(5000), r'+$50.00');
      expect(Money.formatSigned(-1299), r'-$12.99');
    });
  });

  group('Money.parse', () {
    test('parses the obvious forms', () {
      expect(Money.parse('0'), 0);
      expect(Money.parse('1'), 100);
      expect(Money.parse('19.99'), 1999);
      expect(Money.parse('0.01'), 1);
      expect(Money.parse(r'$1,234.56'), 123456);
      expect(Money.parse('-42.10'), -4210);
    });

    test('does not lose a cent to binary floating point', () {
      // int(19.99 * 100) is 1998 in IEEE-754. Going through the string
      // avoids that entirely.
      expect((19.99 * 100).toInt(), 1998);
      expect(Money.parse('19.99'), 1999);

      for (final v in ['0.10', '0.20', '0.30', '0.70', '1.15', '2.675']) {
        final parsed = Money.parse(v);
        if (parsed != null) {
          expect(parsed, (double.parse(v) * 100).round(),
              reason: 'disagreed on $v');
        }
      }
    });

    test('pads a single decimal place', () {
      expect(Money.parse('5.5'), 550);
      expect(Money.parse('5.05'), 505);
    });

    test('rejects nonsense rather than guessing', () {
      expect(Money.parse(''), isNull);
      expect(Money.parse('.'), isNull);
      expect(Money.parse('-'), isNull);
      expect(Money.parse('abc'), isNull);
      expect(Money.parse('1.2.3'), isNull);
      // Three decimal places is not a cent amount; refusing beats rounding
      // silently in a money app.
      expect(Money.parse('1.234'), isNull);
    });

    test('round-trips through format', () {
      for (final cents in [0, 1, 99, 100, 1999, 123456, 100000000]) {
        expect(Money.parse(Money.format(cents)), cents, reason: 'failed at $cents');
      }
    });
  });
}

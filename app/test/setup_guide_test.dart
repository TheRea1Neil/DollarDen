// The guide that greets a brand-new user has to actually render.
//
// It did not. The app theme gives every FilledButton an infinite minimum
// WIDTH (`Size.fromHeight(50)`) so buttons fill a Column; SetupGuide puts one
// in a Row, which hands children unbounded width, and the resulting
// "BoxConstraints forces an infinite width" assertion aborted layout for the
// whole subtree. The Budget tab rendered BLANK -- no error, no partial UI.
//
// It only showed before setup was complete, so the author never saw it and
// every new user would have. Found by screenshotting a fresh instance.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:dollarden/core/theme.dart';
import 'package:dollarden/ui/widgets/setup_guide.dart';

SetupState _state(SetupStage stage) => SetupState(
      stage: stage,
      availableCents: 487213,
      allocatedCents: 412216,
      cashCents: 487213,
      cardDebtCents: 0,
      realCategoryCount: 8,
      pendingCents: 0,
    );

Widget _host(SetupState state) => MaterialApp(
      theme: AppTheme.light(),
      home: Scaffold(
        body: SingleChildScrollView(
          child: SetupGuide(
            state: state,
            onConnectBank: () {},
            onChooseAccounts: () {},
            onNewCategory: () {},
            onAllocate: () {},
            onDescribeBudget: () {},
          ),
        ),
      ),
    );

void main() {
  for (final stage in SetupStage.values) {
    testWidgets('SetupGuide lays out at stage ${stage.name}', (tester) async {
      await tester.pumpWidget(_host(_state(stage)));
      await tester.pump();

      // tester.takeException() returns the layout assertion if one fired.
      // Without the fix this is "BoxConstraints forces an infinite width".
      expect(tester.takeException(), isNull,
          reason: 'stage ${stage.name} threw during layout, which blanks the '
              'whole Budget tab rather than showing an error');
    });
  }

  testWidgets('a filled button in a Row does not demand infinite width',
      (tester) async {
    // The rule itself, so the next Row-with-a-button is caught here.
    await tester.pumpWidget(MaterialApp(
      theme: AppTheme.light(),
      home: Scaffold(
        body: Row(children: [
          FilledButton(style: inlineFilled(), onPressed: () {}, child: const Text('Go')),
        ]),
      ),
    ));
    expect(tester.takeException(), isNull);
  });
}

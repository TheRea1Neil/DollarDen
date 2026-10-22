import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'core/theme.dart';
import 'data/providers.dart';
import 'ui/screens/home_screen.dart';
import 'ui/screens/connections_screen.dart';
import 'ui/screens/login_screen.dart';
import 'ui/screens/review_screen.dart';

void main() => runApp(const ProviderScope(child: NnfApp()));

class NnfApp extends ConsumerWidget {
  const NnfApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final signedIn = ref.watch(authStateProvider);

    return MaterialApp(
      title: 'no-nonsense-finance',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.light(),
      darkTheme: AppTheme.dark(),
      // Null means "still checking for a session" -- showing a splash avoids
      // flashing the login screen at an already-signed-in user.
      home: switch (signedIn) {
        null => const _Splash(),
        false => const LoginScreen(),
        true => const _Shell(),
      },
    );
  }
}

class _Splash extends StatelessWidget {
  const _Splash();

  @override
  Widget build(BuildContext context) =>
      const Scaffold(body: Center(child: CircularProgressIndicator()));
}

class _Shell extends ConsumerStatefulWidget {
  const _Shell();

  @override
  ConsumerState<_Shell> createState() => _ShellState();
}

class _ShellState extends ConsumerState<_Shell> {
  int _tab = 0;

  @override
  Widget build(BuildContext context) {
    final pending = ref.watch(summaryProvider).maybeWhen(
          data: (s) => s.pendingReview,
          orElse: () => 0,
        );

    return Scaffold(
      body: IndexedStack(
        index: _tab,
        children: const [HomeScreen(), ReviewScreen(), ConnectionsScreen()],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (i) => setState(() => _tab = i),
        destinations: [
          const NavigationDestination(
            icon: Icon(Icons.account_balance_wallet_outlined),
            selectedIcon: Icon(Icons.account_balance_wallet),
            label: 'Budget',
          ),
          NavigationDestination(
            icon: Badge.count(
              count: pending,
              isLabelVisible: pending > 0,
              child: const Icon(Icons.inbox_outlined),
            ),
            selectedIcon: const Icon(Icons.inbox),
            label: 'Review',
          ),
          const NavigationDestination(
            icon: Icon(Icons.account_balance_outlined),
            selectedIcon: Icon(Icons.account_balance),
            label: 'Accounts',
          ),
        ],
      ),
    );
  }
}

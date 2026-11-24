import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'core/theme.dart';
import 'data/providers.dart';
import 'ui/screens/home_screen.dart';
import 'ui/screens/insights_screen.dart';
import 'ui/screens/connections_screen.dart';
import 'ui/screens/login_screen.dart';
import 'ui/screens/server_screen.dart';
import 'ui/screens/transactions_screen.dart';

void main() => runApp(const ProviderScope(child: DollarDenApp()));

class DollarDenApp extends ConsumerWidget {
  const DollarDenApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final baseUrl = ref.watch(baseUrlProvider);

    return MaterialApp(
      title: 'dollarden',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.light(),
      darkTheme: AppTheme.dark(),
      // Which server, then who you are. There is nothing to sign in to
      // until the first is answered, and `authStateProvider` is
      // deliberately not read until then -- reading it would start a
      // session check against a server that has not been chosen.
      home: switch (baseUrl) {
        null => const _Splash(),
        '' => const ServerScreen(),
        _ => const _AuthGate(),
      },
    );
  }
}

/// Signed in or not, for a server that is already known.
class _AuthGate extends ConsumerWidget {
  const _AuthGate();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final signedIn = ref.watch(authStateProvider);
    // Null means "still checking for a session" -- showing a splash avoids
    // flashing the login screen at an already-signed-in user.
    return switch (signedIn) {
      null => const _Splash(),
      false => const LoginScreen(),
      true => const _Shell(),
    };
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

class _ShellState extends ConsumerState<_Shell> with WidgetsBindingObserver {
  int _tab = 0;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    // Webhooks land on the SERVER. The phone has no idea anything
    // happened, so a charge could be synced and auto-categorised while
    // the app sat there showing yesterday's numbers until someone
    // pulled down. This is the cheap half of fixing that: re-read the
    // server periodically while the app is actually on screen. It hits
    // no bank and no model -- just this server's own summary.
    _poll = Timer.periodic(const Duration(seconds: 45), (_) => _refetch());
  }

  @override
  void dispose() {
    _poll?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // Coming back to the app is the moment the numbers are most likely
    // to be stale, and the moment someone is most likely to look.
    if (state == AppLifecycleState.resumed) _refetch();
  }

  void _refetch() {
    if (!mounted) return;
    ref.invalidate(summaryProvider);
    ref.invalidate(reviewProvider);
  }

  @override
  Widget build(BuildContext context) {
    final pending = ref.watch(summaryProvider).maybeWhen(
          data: (s) => s.pendingReview,
          orElse: () => 0,
        );

    return Scaffold(
      body: IndexedStack(
        index: _tab,
        children: const [
          HomeScreen(),
          TransactionsScreen(),
          InsightsScreen(),
          ConnectionsScreen(),
        ],
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
            // The count stays on the bar, not just on the Review
            // section inside: it is what makes outstanding work visible
            // without opening the tab at all.
            icon: Badge.count(
              count: pending,
              isLabelVisible: pending > 0,
              child: const Icon(Icons.receipt_long_outlined),
            ),
            selectedIcon: const Icon(Icons.receipt_long),
            label: 'Transactions',
          ),
          const NavigationDestination(
            icon: Icon(Icons.insights_outlined),
            selectedIcon: Icon(Icons.insights),
            label: 'Insights',
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

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../domain/insights.dart';
import '../domain/models.dart';
import 'api_client.dart';
import 'plaid_link_service.dart';
import 'token_store.dart';

/// Which backend this install talks to.
///
/// A preference, not a secret, so SharedPreferences is the right home.
///
/// Three states, and the difference matters:
///
///   null  — still reading the preference
///   ''    — no server chosen yet; ask
///   url   — chosen
///
/// Collapsing the first two would flash the server picker at someone
/// who already has one, every cold start, for as long as the read takes.
final baseUrlProvider = StateNotifierProvider<BaseUrlNotifier, String?>(
  (ref) => BaseUrlNotifier(),
);

class BaseUrlNotifier extends StateNotifier<String?> {
  static const _key = 'dollarden.base_url';
  BaseUrlNotifier() : super(null) {
    _load();
  }

  Future<void> _load() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final stored = prefs.getString(_key);
      state = (stored != null && stored.isNotEmpty)
          ? stored
          // Empty unless this build was given one with
          // --dart-define=DOLLARDEN_SERVER_URL.
          : ApiClient.defaultBaseUrl;
    } catch (_) {
      state = ApiClient.defaultBaseUrl;
    }
  }

  Future<void> set(String url) async {
    final cleaned = url.trim().replaceAll(RegExp(r'/+$'), '');
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_key, cleaned);
    state = cleaned;
  }

  /// Forget the server, and with it the session.
  ///
  /// Tokens are issued BY a server and mean nothing to another one, so
  /// they go too. Without this, pointing the app at a second instance
  /// leaves a stale token that `hasSession()` reads as "signed in" —
  /// and the app opens a budget screen that 401s on every request.
  Future<void> forget() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_key);
    await TokenStore().clear();
    state = '';
  }
}

final apiProvider = Provider<ApiClient>((ref) {
  final url = ref.watch(baseUrlProvider);
  return ApiClient(baseUrl: url ?? '');
});

/// Whether a session exists. Null while we are still finding out, so the
/// UI can show a splash instead of flashing the login screen.
final authStateProvider = StateNotifierProvider<AuthNotifier, bool?>(
  (ref) {
    // Rebuilt when the server changes: a session belongs to one backend,
    // so pointing elsewhere has to re-ask "am I signed in here?" rather
    // than carry the previous answer across.
    ref.watch(baseUrlProvider);
    return AuthNotifier(ref)..bootstrap();
  },
);

class AuthNotifier extends StateNotifier<bool?> {
  final Ref _ref;
  AuthNotifier(this._ref) : super(null);

  ApiClient get _api => _ref.read(apiProvider);

  Future<void> bootstrap() async {
    state = await _api.hasSession();
  }

  Future<void> login(String email, String password) async {
    await _api.login(email, password);
    state = true;
  }

  Future<void> register(String email, String password, {String? displayName}) async {
    await _api.register(email, password, displayName: displayName);
    state = true;
  }

  Future<void> logout() async {
    await _api.logout();
    state = false;
    _ref.invalidate(summaryProvider);
    _ref.invalidate(reviewProvider);
  }
}

final summaryProvider = FutureProvider<BudgetSummary>((ref) async {
  ref.watch(authStateProvider);
  return ref.watch(apiProvider).summary();
});

final reviewProvider = FutureProvider<List<ReviewItem>>((ref) async {
  ref.watch(authStateProvider);
  return ref.watch(apiProvider).review();
});

final categoriesProvider = FutureProvider<List<Category>>((ref) async {
  ref.watch(authStateProvider);
  return ref.watch(apiProvider).categories();
});


/// Deposits that look like income and have not been recorded yet.
///
/// Drives the prompt on the budget screen. Deliberately a proposal, not
/// an automatic split: an unrecognised large deposit — a tax refund,
/// say — must never be divided up as though it were a paycheck.
final unreviewedPaychecksProvider =
    FutureProvider<List<Map<String, dynamic>>>((ref) async {
  final all = await ref.watch(apiProvider).paycheckCandidates();
  return [for (final c in all) if (c['already_recorded'] != true) c];
});

final plaidItemsProvider = FutureProvider<List<PlaidItem>>((ref) async {
  ref.watch(authStateProvider);
  return ref.watch(apiProvider).plaidItems();
});

final plaidLinkProvider = Provider<PlaidLinkService>((ref) {
  final service = PlaidLinkService(ref.watch(apiProvider));
  ref.onDispose(service.dispose);
  return service;
});

// --------------------------------------------------------------------- //
// Analytics
//
// The figures and the prose are deliberately separate providers. The
// numbers come from SQL and are always available; the narrative needs
// the inference host, which sleeps and takes ten seconds to wake. Tying
// them together would make the whole screen wait on the slowest, least
// essential part of it (ADR 0003).
// --------------------------------------------------------------------- //

/// Which stretch of time the insights screen is showing.
final insightsPeriodProvider = StateProvider<String>((_) => 'this_month');

final insightsProvider = FutureProvider<Insights>((ref) async {
  ref.watch(authStateProvider);
  final period = ref.watch(insightsPeriodProvider);
  return ref.watch(apiProvider).insights(period: period);
});

final expenseTypesProvider = FutureProvider<List<ExpenseType>>((ref) async {
  ref.watch(authStateProvider);
  final period = ref.watch(insightsPeriodProvider);
  return ref.watch(apiProvider).expenseTypes(period: period);
});

final recurringProvider = FutureProvider<RecurringReport>((ref) async {
  ref.watch(authStateProvider);
  return ref.watch(apiProvider).recurring();
});

/// Spending that is filed into an envelope but has no kind yet.
///
/// Watched by the insights screen so the labelling queue is visible
/// rather than something the user has to go looking for.
final untypedSpendingProvider = FutureProvider<List<SpendRow>>((ref) async {
  ref.watch(authStateProvider);
  return ref.watch(apiProvider).untypedSpending();
});

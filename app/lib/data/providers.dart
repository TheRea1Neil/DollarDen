import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../domain/models.dart';
import 'api_client.dart';
import 'plaid_link_service.dart';

/// Server URL is a preference, not a secret, so it lives in SharedPreferences.
/// Configurable so a user can point the app at their own deployment.
final baseUrlProvider = StateNotifierProvider<BaseUrlNotifier, String>(
  (ref) => BaseUrlNotifier(),
);

class BaseUrlNotifier extends StateNotifier<String> {
  static const _key = 'nnf.base_url';
  BaseUrlNotifier() : super(ApiClient.defaultBaseUrl) {
    _load();
  }

  Future<void> _load() async {
    final prefs = await SharedPreferences.getInstance();
    final stored = prefs.getString(_key);
    if (stored != null && stored.isNotEmpty) state = stored;
  }

  Future<void> set(String url) async {
    final cleaned = url.trim().replaceAll(RegExp(r'/+$'), '');
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_key, cleaned);
    state = cleaned;
  }
}

final apiProvider = Provider<ApiClient>((ref) {
  final url = ref.watch(baseUrlProvider);
  return ApiClient(baseUrl: url);
});

/// Whether a session exists. Null while we are still finding out, so the
/// UI can show a splash instead of flashing the login screen.
final authStateProvider = StateNotifierProvider<AuthNotifier, bool?>(
  (ref) => AuthNotifier(ref)..bootstrap(),
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

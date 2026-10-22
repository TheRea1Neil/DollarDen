import 'dart:async';

import 'package:dio/dio.dart';

import '../domain/models.dart';
import 'token_store.dart';

class ApiException implements Exception {
  final int? statusCode;
  final String message;
  ApiException(this.message, [this.statusCode]);

  bool get isAuth => statusCode == 401 || statusCode == 403;
  bool get isConflict => statusCode == 409;
  bool get isRateLimited => statusCode == 429;

  @override
  String toString() => message;
}

/// Talks to the backend at budget.example.org.
///
/// The base URL is configurable so a user can point the app at their own
/// deployment, and so development can target a local server.
class ApiClient {
  static const defaultBaseUrl = 'https://budget.example.org';

  final Dio _dio;
  final TokenStore _tokens;

  /// Guards against a thundering herd of refreshes: several requests can
  /// fail with 401 at once, and they should share a single refresh rather
  /// than each rotating the token out from under the others.
  Future<bool>? _refreshInFlight;

  ApiClient({String? baseUrl, TokenStore? tokens, Dio? dio})
      : _tokens = tokens ?? TokenStore(),
        _dio = dio ??
            Dio(BaseOptions(
              baseUrl: '${baseUrl ?? defaultBaseUrl}/api',
              connectTimeout: const Duration(seconds: 15),
              receiveTimeout: const Duration(seconds: 60),
              headers: {'Content-Type': 'application/json'},
              // We interpret status codes ourselves rather than letting Dio
              // throw, so error bodies stay readable.
              validateStatus: (_) => true,
            ));

  set baseUrl(String value) => _dio.options.baseUrl = '$value/api';
  String get baseUrl => _dio.options.baseUrl.replaceAll(RegExp(r'/api$'), '');

  // ------------------------------------------------------------------ //
  // plumbing
  // ------------------------------------------------------------------ //
  Future<Options> _authed() async {
    final token = await _tokens.accessToken;
    return Options(headers: {
      if (token != null) 'Authorization': 'Bearer $token',
    });
  }

  Never _fail(Response r) {
    final data = r.data;
    var message = 'Request failed (${r.statusCode})';
    if (data is Map && data['detail'] != null) {
      final d = data['detail'];
      message = d is String ? d : d.toString();
    }
    throw ApiException(message, r.statusCode);
  }

  /// Runs [send], and on a 401 refreshes once and retries.
  Future<Response> _withRetry(Future<Response> Function(Options) send) async {
    var response = await send(await _authed());
    if (response.statusCode != 401) return response;

    final refreshed = await _refreshTokens();
    if (!refreshed) return response;

    response = await send(await _authed());
    return response;
  }

  Future<bool> _refreshTokens() {
    return _refreshInFlight ??= _doRefresh().whenComplete(() {
      _refreshInFlight = null;
    });
  }

  Future<bool> _doRefresh() async {
    final refresh = await _tokens.refreshToken;
    if (refresh == null) return false;

    final r = await _dio.post('/auth/refresh', data: {'refresh_token': refresh});
    if (r.statusCode != 200 || r.data is! Map) return false;

    final data = (r.data as Map).cast<String, dynamic>();
    // Refresh tokens are single-use and rotate, so the new one must be
    // stored or the next refresh fails.
    await _tokens.saveTokens(
      data['access_token'] as String,
      data['refresh_token'] as String,
    );
    return true;
  }

  // ------------------------------------------------------------------ //
  // auth
  // ------------------------------------------------------------------ //
  Future<void> register(String email, String password, {String? displayName}) async {
    final r = await _dio.post('/auth/register', data: {
      'email': email,
      'password': password,
      if (displayName != null && displayName.isNotEmpty) 'display_name': displayName,
    });
    if (r.statusCode != 201) _fail(r);
    final d = (r.data as Map).cast<String, dynamic>();
    await _tokens.saveTokens(d['access_token'] as String, d['refresh_token'] as String);
  }

  Future<void> login(String email, String password) async {
    final r = await _dio.post('/auth/login', data: {'email': email, 'password': password});
    if (r.statusCode != 200) _fail(r);
    final d = (r.data as Map).cast<String, dynamic>();
    await _tokens.saveTokens(d['access_token'] as String, d['refresh_token'] as String);
  }

  Future<void> logout() async {
    final refresh = await _tokens.refreshToken;
    if (refresh != null) {
      // Best effort: the local tokens are cleared regardless, so a failure
      // here must not leave the user stuck signed in.
      try {
        await _dio.post('/auth/logout', data: {'refresh_token': refresh});
      } catch (_) {}
    }
    await _tokens.clear();
  }

  Future<bool> hasSession() async => (await _tokens.accessToken) != null;

  Future<Map<String, dynamic>> me() async {
    final r = await _withRetry((o) => _dio.get('/auth/me', options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as Map).cast<String, dynamic>();
  }

  // ------------------------------------------------------------------ //
  // budget
  // ------------------------------------------------------------------ //
  Future<BudgetSummary> summary() async {
    final r = await _withRetry((o) => _dio.get('/budget/summary', options: o));
    if (r.statusCode != 200) _fail(r);
    return BudgetSummary.fromJson((r.data as Map).cast<String, dynamic>());
  }

  Future<List<Category>> categories({bool includeArchived = false}) async {
    final r = await _withRetry((o) => _dio.get(
          '/budget/categories',
          queryParameters: {'include_archived': includeArchived},
          options: o,
        ));
    if (r.statusCode != 200) _fail(r);
    return (r.data as List)
        .map((e) => Category.fromJson((e as Map).cast<String, dynamic>()))
        .toList();
  }

  Future<Category> createCategory({
    required String name,
    required String kind,
    int? targetCents,
    int? alertAtCents,
    String? description,
  }) async {
    final r = await _withRetry((o) => _dio.post('/budget/categories', data: {
          'name': name,
          'kind': kind,
          if (targetCents != null) 'target_cents': targetCents,
          if (alertAtCents != null) 'alert_at_cents': alertAtCents,
          if (description != null && description.isNotEmpty) 'description': description,
        }, options: o));
    if (r.statusCode != 201) _fail(r);
    return Category.fromJson((r.data as Map).cast<String, dynamic>());
  }

  Future<Category> updateCategory(String id, Map<String, dynamic> changes) async {
    final r = await _withRetry(
        (o) => _dio.patch('/budget/categories/$id', data: changes, options: o));
    if (r.statusCode != 200) _fail(r);
    return Category.fromJson((r.data as Map).cast<String, dynamic>());
  }

  Future<void> archiveCategory(String id) async {
    final r = await _withRetry((o) => _dio.delete('/budget/categories/$id', options: o));
    if (r.statusCode != 200) _fail(r);
  }

  Future<List<ReviewItem>> review() async {
    final r = await _withRetry((o) => _dio.get('/budget/review', options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as List)
        .map((e) => ReviewItem.fromJson((e as Map).cast<String, dynamic>()))
        .toList();
  }

  /// Splits must account for every cent of the transaction; the backend
  /// rejects a partial split rather than leaving the ledger disagreeing
  /// with the bank.
  Future<void> resolveReview(
    String reviewId,
    List<({String categoryId, int amountCents})> splits, {
    bool remember = false,
  }) async {
    final r = await _withRetry((o) => _dio.post(
          '/budget/review/$reviewId/resolve',
          data: {
            'splits': [
              for (final s in splits)
                {'category_id': s.categoryId, 'amount_cents': s.amountCents}
            ],
            'remember': remember,
          },
          options: o,
        ));
    if (r.statusCode != 204) _fail(r);
  }

  Future<NextPayday> nextPayday() async {
    final r = await _withRetry((o) => _dio.get('/budget/next-payday', options: o));
    if (r.statusCode != 200) _fail(r);
    return NextPayday.fromJson((r.data as Map).cast<String, dynamic>());
  }

  // ------------------------------------------------------------------ //
  // plaid
  // ------------------------------------------------------------------ //

  /// Mint a link_token for the Plaid Link SDK.
  ///
  /// Passing [itemId] requests UPDATE MODE, which re-authenticates that
  /// existing connection in place. That path does not consume a connection
  /// credit and is the correct response to `login_required` (docs/04).
  Future<String> linkToken({String? itemId}) async {
    final r = await _withRetry((o) => _dio.post(
          '/plaid/link-token',
          queryParameters: {if (itemId != null) 'item_id': itemId},
          options: o,
        ));
    if (r.statusCode != 200) _fail(r);
    return (r.data as Map)['link_token'] as String;
  }

  /// Hand the short-lived public_token to the backend, which exchanges it
  /// for the long-lived access_token. That token never reaches this app.
  Future<PlaidItem> exchangePublicToken(String publicToken) async {
    final r = await _withRetry((o) => _dio.post(
          '/plaid/exchange',
          data: {'public_token': publicToken},
          options: o,
        ));
    if (r.statusCode != 201) _fail(r);
    return PlaidItem.fromJson((r.data as Map).cast<String, dynamic>());
  }

  Future<List<PlaidItem>> plaidItems() async {
    final r = await _withRetry((o) => _dio.get('/plaid/items', options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as List)
        .map((e) => PlaidItem.fromJson((e as Map).cast<String, dynamic>()))
        .toList();
  }

  Future<SyncResult> syncItem(String itemId) async {
    final r = await _withRetry(
        (o) => _dio.post('/plaid/items/$itemId/sync', options: o));
    if (r.statusCode != 200) _fail(r);
    return SyncResult.fromJson((r.data as Map).cast<String, dynamic>());
  }

  /// Fetch the encrypted token blobs for on-device redundancy.
  ///
  /// These are ciphertext the app cannot decrypt -- the server holds the
  /// key. Storing them means a dead Pi can be recovered without re-linking
  /// and burning a credit (docs/04).
  Future<Map<String, dynamic>> exportTokenBlobs() async {
    final r = await _withRetry((o) => _dio.get('/backup/tokens', options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as Map).cast<String, dynamic>();
  }

  Future<bool> health() async {
    try {
      final r = await _dio.get('/health');
      return r.statusCode == 200;
    } catch (_) {
      return false;
    }
  }
}

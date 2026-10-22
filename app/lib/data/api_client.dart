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

  /// Bring an archived category back. Categories are archived, never
  /// deleted, so this always has something to restore.
  Future<Category> restoreCategory(String id) async {
    final r = await _withRetry(
        (o) => _dio.post('/budget/categories/$id/restore', options: o));
    if (r.statusCode != 200) _fail(r);
    return Category.fromJson((r.data as Map).cast<String, dynamic>());
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

  /// Resolve a review by creating a brand-new category for it.
  ///
  /// Finding a charge that fits nowhere is exactly when you notice a
  /// category is missing, so this avoids leaving the queue to go and make
  /// one.
  Future<void> resolveReviewWithNewCategory(
    String reviewId, {
    required String name,
    String kind = 'spending',
    int? targetCents,
    int? alertAtCents,
    bool remember = false,
  }) async {
    final r = await _withRetry((o) => _dio.post(
          '/budget/review/$reviewId/resolve',
          data: {
            'new_category': {
              'name': name,
              'kind': kind,
              if (targetCents != null) 'target_cents': targetCents,
              if (alertAtCents != null) 'alert_at_cents': alertAtCents,
            },
            'remember': remember,
          },
          options: o,
        ));
    if (r.statusCode != 204) _fail(r);
  }

  /// Move a transaction in or out of the budget entirely.
  Future<void> setTreatment(String transactionId, String treatment) async {
    final r = await _withRetry((o) => _dio.post(
          '/budget/transactions/$transactionId/treatment',
          data: {'treatment': treatment},
          options: o,
        ));
    if (r.statusCode != 200) _fail(r);
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

  // ------------------------------------------------------------------ //
  // ai
  // ------------------------------------------------------------------ //
  Future<String> getGoals() async {
    final r = await _withRetry((o) => _dio.get('/ai/goals', options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as Map)['body'] as String? ?? '';
  }

  Future<void> setGoals(String body) async {
    final r = await _withRetry(
        (o) => _dio.put('/ai/goals', data: {'body': body}, options: o));
    if (r.statusCode != 200) _fail(r);
  }

  Future<AiJob> queueJob(String kind, [Map<String, dynamic>? request]) async {
    final r = await _withRetry((o) => _dio.post(
          '/ai/jobs',
          data: {'kind': kind, 'request': request ?? {}},
          options: o,
        ));
    if (r.statusCode != 202) _fail(r);
    return AiJob.fromJson((r.data as Map).cast<String, dynamic>());
  }

  Future<AiJob> getJob(String id) async {
    final r = await _withRetry((o) => _dio.get('/ai/jobs/$id', options: o));
    if (r.statusCode != 200) _fail(r);
    return AiJob.fromJson((r.data as Map).cast<String, dynamic>());
  }

  /// Poll a job to completion.
  ///
  /// The host may be asleep, so the first response can take ~10s longer
  /// than the inference itself. [onProgress] lets the UI say which stage
  /// it is at rather than showing an opaque spinner.
  Future<AiJob> awaitJob(
    String id, {
    Duration interval = const Duration(seconds: 3),
    Duration timeout = const Duration(minutes: 3),
    void Function(AiJob)? onProgress,
  }) async {
    final deadline = DateTime.now().add(timeout);
    while (DateTime.now().isBefore(deadline)) {
      final job = await getJob(id);
      onProgress?.call(job);
      if (job.finished) return job;
      await Future<void>.delayed(interval);
    }
    throw ApiException('The AI server did not respond in time.');
  }

  Future<Map<String, dynamic>> aiStatus() async {
    final r = await _withRetry((o) => _dio.get('/ai/status', options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as Map).cast<String, dynamic>();
  }

  // ------------------------------------------------------------------ //
  // proposals, opening balances
  // ------------------------------------------------------------------ //

  /// Apply a proposal the user has reviewed. Sends the EDITED payload, not
  /// a job id: what gets applied is what they approved on screen.
  Future<Map<String, dynamic>> applyProposal({
    required List<ProposedCategory> categories,
    required List<ProposedRule> rules,
    String planName = 'Standard',
    bool categoriesOnly = false,
  }) async {
    final r = await _withRetry((o) => _dio.post('/budget/apply-proposal', data: {
          'categories': [for (final c in categories) c.toJson()],
          'split_rules': [for (final x in rules) x.toJson()],
          'plan_name': planName,
          'categories_only': categoriesOnly,
        }, options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as Map).cast<String, dynamic>();
  }

  /// Set DISPLAY order from a dragged list. Changes no money.
  ///
  /// Funding priority is a different thing entirely -- see [reorderPlan].
  Future<List<Category>> reorderCategories(List<String> ids) async {
    final r = await _withRetry((o) => _dio.post('/budget/categories/reorder',
        data: {'category_ids': ids}, options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as List)
        .map((e) => Category.fromJson((e as Map).cast<String, dynamic>()))
        .toList();
  }

  /// Set FUNDING order: who gets paid first out of a paycheck. Saves a new
  /// plan version, and is rejected if the drag would strand the remainder.
  Future<Map<String, dynamic>> reorderPlan(
      String planId, List<String> categoryIds) async {
    final r = await _withRetry((o) => _dio.post('/budget/plans/$planId/reorder',
        data: {'category_ids': categoryIds}, options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as Map).cast<String, dynamic>();
  }

  Future<List<Map<String, dynamic>>> plans() async {
    final r = await _withRetry((o) => _dio.get('/budget/plans', options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as List)
        .map((e) => (e as Map).cast<String, dynamic>())
        .toList();
  }

  /// Create one reviewed category, optionally inserting a funding rule into
  /// the active plan. Unlike [applyProposal] this never rewrites the plan.
  Future<Map<String, dynamic>> applyCategory({
    required ProposedCategory category,
    String? ruleType,
    Map<String, dynamic> params = const {},
    int? orderIndex,
    Map<String, dynamic>? matcher,
  }) async {
    final r = await _withRetry((o) => _dio.post('/budget/apply-category', data: {
          'category': category.toJson(),
          if (ruleType != null) 'rule_type': ruleType,
          'params': params,
          if (orderIndex != null) 'order_index': orderIndex,
          if (matcher != null && matcher.isNotEmpty) 'matcher': matcher,
        }, options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as Map).cast<String, dynamic>();
  }

  Future<Map<String, dynamic>> reconciliation() async {
    final r = await _withRetry((o) => _dio.get('/budget/reconciliation', options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as Map).cast<String, dynamic>();
  }

  Future<Map<String, dynamic>> setOpeningBalance(
    List<({String categoryId, int amountCents})> allocations,
  ) async {
    final r = await _withRetry((o) => _dio.post('/budget/opening-balance', data: {
          'allocations': [
            for (final a in allocations)
              {'category_id': a.categoryId, 'amount_cents': a.amountCents}
          ],
        }, options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as Map).cast<String, dynamic>();
  }

  Future<List<Map<String, dynamic>>> accounts() async {
    final r = await _withRetry((o) => _dio.get('/budget/accounts', options: o));
    if (r.statusCode != 200) _fail(r);
    return [for (final e in r.data as List) (e as Map).cast<String, dynamic>()];
  }

  Future<void> setAccountTracked(String id, bool tracked) async {
    final r = await _withRetry((o) => _dio.post(
          '/budget/accounts/$id/tracked',
          data: {'is_tracked': tracked},
          options: o,
        ));
    if (r.statusCode != 200) _fail(r);
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

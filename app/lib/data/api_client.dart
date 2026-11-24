import 'dart:io';
import 'dart:async';

import 'package:dio/dio.dart';

import '../domain/insights.dart';
import '../domain/models.dart';
import 'token_store.dart';

/// The job is parked waiting for the GPU, or for morning.
///
/// Deliberately its own type rather than an ApiException: nothing has
/// gone wrong, the work is queued, and the screen should say so and
/// offer to run it anyway rather than showing an error.
class JobDeferred implements Exception {
  final AiJob job;
  JobDeferred(this.job);

  String get reason =>
      job.deferReason ?? 'Waiting for the computer to be free.';

  @override
  String toString() => reason;
}


/// Insist a finished job actually produced something, and say plainly
/// why if it did not.
///
/// Every AI call used to make this judgement for itself, and four of
/// them collapsed `deferred` into "the assistant could not answer
/// that" -- which is wrong twice over: nothing failed, and the work is
/// still queued. The user was told the feature was broken while it was
/// quietly waiting for a game to finish.
///
/// `deferred` therefore throws JobDeferred, which is NOT an error. The
/// screen should say what it is waiting for and offer to run it anyway.
AiJob requireFinished(AiJob done, String couldNot) {
  if (done.status == 'deferred') throw JobDeferred(done);
  if (done.status != 'done' || done.result == null) {
    throw ApiException(done.error ?? couldNot);
  }
  return done;
}

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

/// Talks to whichever backend this install has been pointed at.
///
/// There is no built-in server. Every install picks one first and then
/// signs in to it, the way a Jellyfin client does — which is the only
/// shape that works when everyone self-hosts their own.
///
/// A build may supply a default so that someone building the app for
/// their own household does not have to type it on every device:
///
///     flutter build apk --dart-define=DOLLARDEN_SERVER_URL=https://budget.example.org
///
/// Left unset it is empty, the app asks on first run, and no one
/// person's server is baked into the source.
class ApiClient {
  static const defaultBaseUrl =
      String.fromEnvironment('DOLLARDEN_SERVER_URL', defaultValue: '');

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

  /// Is there a DollarDen backend at this URL?
  ///
  /// Checked before a URL is saved, because the alternative is someone
  /// mistyping their address and then being told their password is
  /// wrong. Unauthenticated and tiny.
  static Future<bool> probe(String url) async {
    final cleaned = url.trim().replaceAll(RegExp(r'/+$'), '');
    try {
      final r = await Dio(BaseOptions(
        connectTimeout: const Duration(seconds: 10),
        receiveTimeout: const Duration(seconds: 10),
        validateStatus: (_) => true,
      )).get('$cleaned/api/health');
      return r.statusCode == 200;
    } catch (_) {
      return false;
    }
  }
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
    String? spendCriteria,
  }) async {
    final r = await _withRetry((o) => _dio.post('/budget/categories', data: {
          'name': name,
          'kind': kind,
          if (targetCents != null) 'target_cents': targetCents,
          if (alertAtCents != null) 'alert_at_cents': alertAtCents,
          if (description != null && description.isNotEmpty)
            'description': description,
          if (spendCriteria != null && spendCriteria.isNotEmpty)
            'spend_criteria': spendCriteria,
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

  /// Everything about one category: its settings, how it is funded and
  /// where in the order, what deducts from it, and recent activity.
  Future<CategoryDetail> categoryDetail(String id) async {
    final r = await _withRetry(
        (o) => _dio.get('/budget/categories/$id/detail', options: o));
    if (r.statusCode != 200) _fail(r);
    return CategoryDetail.fromJson((r.data as Map).cast<String, dynamic>());
  }

  /// Set, change, or remove how one category is funded.
  ///
  /// `ruleType: null` takes it out of the split plan — a category you only
  /// ever allocate to by hand. Saves a new plan version either way.
  Future<CategoryDetail> setFunding(
    String id, {
    required String? ruleType,
    Map<String, dynamic> params = const {},
    int? position,
  }) async {
    final r = await _withRetry((o) => _dio.put(
          '/budget/categories/$id/funding',
          data: {
            'rule_type': ruleType,
            'params': params,
            if (position != null) 'position': position,
          },
          options: o,
        ));
    if (r.statusCode != 200) _fail(r);
    return CategoryDetail.fromJson((r.data as Map).cast<String, dynamic>());
  }

  Future<Matcher> addMatcher(String categoryId, Map<String, dynamic> matcher,
      {int priority = 100}) async {
    final r = await _withRetry((o) => _dio.post('/budget/rules', data: {
          'category_id': categoryId,
          'matcher': matcher,
          'priority': priority,
        }, options: o));
    if (r.statusCode != 201) _fail(r);
    final j = (r.data as Map).cast<String, dynamic>();
    // The create endpoint does not render a summary; the detail call does.
    return Matcher.fromJson({...j, 'summary': ''});
  }

  Future<void> deleteMatcher(String id) async {
    final r = await _withRetry((o) => _dio.delete('/budget/rules/$id', options: o));
    if (r.statusCode != 204) _fail(r);
  }

  /// Bring an archived category back. Categories are archived, never
  /// deleted, so this always has something to restore.
  Future<Category> restoreCategory(String id) async {
    final r = await _withRetry(
        (o) => _dio.post('/budget/categories/$id/restore', options: o));
    if (r.statusCode != 200) _fail(r);
    return Category.fromJson((r.data as Map).cast<String, dynamic>());
  }

  /// Clear imported history nobody intended to budget.
  ///
  /// Connecting a bank imports whatever the institution offers -- months
  /// of charges land in the queue at once. [dryRun] asks what WOULD be
  /// dismissed without touching anything, so the count can be shown
  /// before the user commits to it.
  Future<DismissPreview> dismissBefore(DateTime before,
      {bool dryRun = true}) async {
    final r = await _withRetry((o) => _dio.post(
          '/budget/review/dismiss-before',
          data: {
            'before': before.toIso8601String().split('T').first,
            'dry_run': dryRun,
          },
          options: o,
        ));
    if (r.statusCode != 200) _fail(r);
    final m = (r.data as Map).cast<String, dynamic>();
    return DismissPreview(
      dismissed: m['dismissed'] as int? ?? 0,
      outflowCents: m['outflow_cents'] as int? ?? 0,
      oldest: m['oldest'] as String?,
      newest: m['newest'] as String?,
    );
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
    String? description,
    int? targetCents,
    int? alertAtCents,
    bool remember = false,
  }) async {
    final r = await _withRetry((o) => _dio.post(
          '/budget/review/$reviewId/resolve',
          data: {
            'new_category': {
              'name': name,
              if (description != null && description.isNotEmpty)
                'description': description,
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
  /// Android OAuth needs `android_package_name` on the link token and iOS
  /// needs `redirect_uri`; they are mutually exclusive, and sending the
  /// wrong one dead-ends at the bank's login instead of erroring. Only the
  /// app knows which it is, so it has to say.
  /// Pull transactions and balances from every connected bank, now.
  ///
  /// Pull-to-refresh used to only re-read the server's database, so it
  /// showed whatever the last webhook delivered and nothing newer. A
  /// missed webhook left the app silently stale with no way out.
  /// Throttled server-side, so pulling repeatedly is harmless.
  Future<void> refreshBanks() async {
    final r = await _withRetry((o) => _dio.post('/plaid/refresh', options: o));
    if (r.statusCode != 200) _fail(r);
  }

  Future<String> linkToken({String? itemId}) async {
    final platform = Platform.isAndroid
        ? 'android'
        : Platform.isIOS
            ? 'ios'
            : null;
    final r = await _withRetry((o) => _dio.post(
          '/plaid/link-token',
          queryParameters: {
            if (itemId != null) 'item_id': itemId,
            if (platform != null) 'platform': platform,
          },
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
      // Deferred stops the poll. The job is waiting for the GPU or for
      // morning and could be an hour away -- spinning until the
      // timeout would show the user nothing and tell them less.
      //
      // It THROWS rather than returning, because returning left every
      // caller to notice for itself and most did not: four of them
      // reported "the assistant could not answer that" for a job that
      // had not failed and was still queued. Throwing means a screen
      // either handles it deliberately or shows the reason verbatim --
      // both of which beat claiming the feature is broken.
      if (job.status == 'deferred') throw JobDeferred(job);
      if (job.settled) return job;
      await Future<void>.delayed(interval);
    }
    throw ApiException('The AI server did not respond in time.');
  }

  /// Run a deferred job now, whatever the GPU is doing.
  ///
  /// The gate protects a game's frame rate; it does not get to overrule
  /// the person who owns the machine.
  Future<AiJob> retryJob(String id) async {
    final r = await _withRetry((o) => _dio.post('/ai/jobs/$id/retry', options: o));
    if (r.statusCode != 200) _fail(r);
    return AiJob.fromJson((r.data as Map).cast<String, dynamic>());
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

  /// Save a reviewed set of rules as a new plan version.
  Future<Map<String, dynamic>> revisePlan(
      String planId, String name, List<Map<String, dynamic>> rules) async {
    final r = await _withRetry((o) => _dio.put('/budget/plans/$planId',
        data: {'name': name, 'rules': rules}, options: o));
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

  /// What a paycheck would do. Writes nothing.
  ///
  /// Pass [rules] to try a different funding order or a changed rule
  /// without saving anything — the order of the list IS the funding
  /// order.
  Future<PaycheckPreview> previewPaycheck(int grossCents,
      {String? planId,
      List<Map<String, dynamic>>? rules,
      List<Map<String, dynamic>>? manual}) async {
    final r = await _withRetry((o) => _dio.post('/budget/paychecks/preview',
        data: {
          'gross_cents': grossCents,
          if (planId != null) 'plan_id': planId,
          if (rules != null) 'rules': rules,
          if (manual != null && manual.isNotEmpty) 'manual': manual,
        },
        options: o));
    if (r.statusCode != 200) _fail(r);
    return PaycheckPreview.fromJson((r.data as Map).cast<String, dynamic>());
  }

  /// Record a paycheck and split it. This one does write.
  ///
  /// [rules] applies to THIS paycheck only and never rewrites the saved
  /// plan; [manual] amounts are taken off the top before the rules run.
  /// [transactionId] links the paycheck to the deposit it came from.
  /// Without it the deposit is never marked as recorded and keeps being
  /// offered for review forever.
  Future<Map<String, dynamic>> recordPaycheck(int grossCents,
      {bool apply = true,
      String? transactionId,
      List<Map<String, dynamic>>? rules,
      List<Map<String, dynamic>>? manual}) async {
    final r = await _withRetry((o) => _dio.post('/budget/paychecks',
        data: {
          'gross_cents': grossCents,
          'apply': apply,
          if (transactionId != null) 'transaction_id': transactionId,
          if (rules != null) 'rules': rules,
          if (manual != null && manual.isNotEmpty) 'manual': manual,
        },
        options: o));
    if (r.statusCode != 201) _fail(r);
    return (r.data as Map).cast<String, dynamic>();
  }

  /// Undo a paycheck, reversing every allocation it made.
  /// What undoing this paycheck would do. Writes nothing.
  Future<UndoPreview> undoPreview(String id) async {
    final r = await _withRetry(
        (o) => _dio.get('/budget/paychecks/$id/undo-preview', options: o));
    if (r.statusCode != 200) _fail(r);
    return UndoPreview.fromJson((r.data as Map).cast<String, dynamic>());
  }

  /// Reverse a paycheck. Refused with 409 when it would leave categories
  /// overspent, unless [force] — reversing does not un-spend the money.
  Future<UndoPreview> unapplyPaycheck(String id, {bool force = false}) async {
    final r = await _withRetry((o) => _dio.post(
        '/budget/paychecks/$id/unapply',
        data: {'force': force},
        options: o));
    if (r.statusCode != 200) _fail(r);
    return UndoPreview.fromJson((r.data as Map).cast<String, dynamic>());
  }

  /// Every transaction and where its money went.
  Future<HistoryPage> history({
    int limit = 50,
    int offset = 0,
    String? categoryId,
    String? treatment,
    String? search,
  }) async {
    final r = await _withRetry((o) => _dio.get('/budget/transactions',
        queryParameters: {
          'limit': limit,
          'offset': offset,
          if (categoryId != null) 'category_id': categoryId,
          if (treatment != null) 'treatment': treatment,
          if (search != null && search.isNotEmpty) 'search': search,
        },
        options: o));
    if (r.statusCode != 200) _fail(r);
    return HistoryPage.fromJson((r.data as Map).cast<String, dynamic>());
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

  /// Read a receipt into line items, from a photo or pasted text.
  ///
  /// [expectedCents] is the real charge when this is being matched to a
  /// transaction, so the result can say when the two disagree.
  Future<ParsedReceipt> parseReceipt({
    String? imageBase64,
    String? text,
    int? expectedCents,
    void Function(AiJob)? onProgress,
  }) async {
    final job = await queueJob('receipt_parse', {
      if (imageBase64 != null) 'image_b64': imageBase64,
      if (text != null && text.isNotEmpty) 'text': text,
      if (expectedCents != null) 'expected_cents': expectedCents,
    });
    // Vision on a sleeping host is the slowest thing the app does.
    final done = await awaitJob(job.id,
        onProgress: onProgress, timeout: const Duration(minutes: 8));
    if (!done.ok) {
      throw ApiException(done.error ?? 'The assistant could not read it.');
    }
    return ParsedReceipt.fromJson(done.result ?? {});
  }

  /// Categorise every line of a receipt in one call.
  ///
  /// Returns a map of 1-based line number to (categoryId, name); a line
  /// the assistant would not commit on is absent rather than guessed.
  Future<Map<int, (String, String)>> categoriseItems({
    required String merchant,
    required List<ReceiptItem> items,
  }) async {
    final job = await queueJob('categorise_items', {
      'merchant': merchant,
      'items': [
        for (final i in items)
          {'description': i.description, 'amount_cents': i.amountCents}
      ],
    });
    final done = await awaitJob(job.id, timeout: const Duration(minutes: 4));
    if (!done.ok) {
      throw ApiException(done.error ?? 'The assistant could not finish.');
    }
    final out = <int, (String, String)>{};
    for (final row in (done.result?['lines'] as List?) ?? const []) {
      final m = (row as Map).cast<String, dynamic>();
      final id = m['category_id'] as String?;
      final name = m['category_name'] as String?;
      if (id != null && name != null) {
        out[m['line'] as int] = (id, name);
      }
    }
    return out;
  }

  /// Ask where one charge belongs. Suggests; never files.
  Future<CategorySuggestion> suggestCategory({
    required String merchant,
    required int amountCents,
  }) async {
    final job = await queueJob('suggest_category',
        {'merchant': merchant, 'amount_cents': amountCents});
    final done = await awaitJob(job.id);
    if (!done.ok) {
      throw ApiException(done.error ?? 'The assistant could not finish.');
    }
    return CategorySuggestion.fromJson(done.result ?? {});
  }

  /// Deposits that look like income and have not been recorded yet.
  Future<List<Map<String, dynamic>>> paycheckCandidates() async {
    final r = await _withRetry(
        (o) => _dio.get('/budget/paychecks/candidates', options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as List)
        .map((e) => (e as Map).cast<String, dynamic>())
        .toList();
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

  // ----------------------------------------------------------------- //
  // Analytics
  //
  // Every figure here is computed server-side in SQL. The AI endpoints
  // below add language on top; they never produce a number, so the
  // screen is fully usable with the inference host asleep.
  // ----------------------------------------------------------------- //

  /// Today, as the PHONE sees it.
  ///
  /// The Pi runs on UTC. Asked at 22:00 on the 31st, a server-side
  /// `today` would answer for next month, and "this month" would come
  /// back empty for no visible reason.
  static String _localToday() {
    final n = DateTime.now();
    return '${n.year.toString().padLeft(4, '0')}-'
        '${n.month.toString().padLeft(2, '0')}-'
        '${n.day.toString().padLeft(2, '0')}';
  }

  Future<Insights> insights({String period = 'this_month'}) async {
    final r = await _withRetry((o) => _dio.get('/analytics/insights',
        queryParameters: {'period': period, 'today': _localToday()},
        options: o));
    if (r.statusCode != 200) _fail(r);
    return Insights.fromJson((r.data as Map).cast<String, dynamic>());
  }

  Future<SpendResult> spend({
    String period = 'this_month',
    String? groupBy,
    String metric = 'spend',
    List<String> expenseTypeIds = const [],
    List<String> categoryIds = const [],
    String? merchant,
    int limit = 20,
  }) async {
    final r = await _withRetry((o) => _dio.get('/analytics/spend',
        queryParameters: {
          'period': period,
          'today': _localToday(),
          'metric': metric,
          if (groupBy != null) 'group_by': groupBy,
          if (expenseTypeIds.isNotEmpty) 'expense_type_id': expenseTypeIds,
          if (categoryIds.isNotEmpty) 'category_id': categoryIds,
          if (merchant != null && merchant.isNotEmpty) 'merchant': merchant,
          'limit': limit,
        },
        options: o));
    if (r.statusCode != 200) _fail(r);
    return SpendResult.fromJson((r.data as Map).cast<String, dynamic>());
  }

  /// The rows behind a figure, so every total on screen opens up.
  Future<List<SpendRow>> spendTransactions({
    String period = 'this_month',
    List<String> expenseTypeIds = const [],
    List<String> categoryIds = const [],
    String? merchant,
    int limit = 100,
  }) async {
    final r = await _withRetry((o) => _dio.get('/analytics/spend/transactions',
        queryParameters: {
          'period': period,
          'today': _localToday(),
          if (expenseTypeIds.isNotEmpty) 'expense_type_id': expenseTypeIds,
          if (categoryIds.isNotEmpty) 'category_id': categoryIds,
          if (merchant != null && merchant.isNotEmpty) 'merchant': merchant,
          'limit': limit,
        },
        options: o));
    if (r.statusCode != 200) _fail(r);
    return ((r.data as Map)['transactions'] as List? ?? const [])
        .map((e) => SpendRow.fromJson((e as Map).cast<String, dynamic>()))
        .toList();
  }

  Future<RecurringReport> recurring() async {
    final r = await _withRetry((o) => _dio.get('/analytics/recurring',
        queryParameters: {'today': _localToday()}, options: o));
    if (r.statusCode != 200) _fail(r);
    return RecurringReport.fromJson((r.data as Map).cast<String, dynamic>());
  }

  Future<List<ExpenseType>> expenseTypes({String period = 'last_90_days'}) async {
    final r = await _withRetry((o) => _dio.get('/analytics/expense-types',
        queryParameters: {'period': period, 'today': _localToday()}, options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as List)
        .map((e) => ExpenseType.fromJson((e as Map).cast<String, dynamic>()))
        .toList();
  }

  Future<List<TypeSuggestion>> expenseTypeSuggestions() async {
    final r = await _withRetry(
        (o) => _dio.get('/analytics/expense-types/suggestions', options: o));
    if (r.statusCode != 200) _fail(r);
    return ((r.data as Map)['suggestions'] as List? ?? const [])
        .map((e) => TypeSuggestion.fromJson((e as Map).cast<String, dynamic>()))
        .toList();
  }

  /// Types the assistant coined that nobody has confirmed yet.
  Future<List<ExpenseType>> unreviewedExpenseTypes() async {
    final r = await _withRetry(
        (o) => _dio.get('/analytics/expense-types/unreviewed', options: o));
    if (r.statusCode != 200) _fail(r);
    return (r.data as List)
        .map((e) => ExpenseType.fromJson((e as Map).cast<String, dynamic>()))
        .toList();
  }

  Future<ExpenseType> createExpenseType(String name, {String? description}) async {
    final r = await _withRetry((o) => _dio.post('/analytics/expense-types',
        data: {'name': name, if (description != null) 'description': description},
        options: o));
    if (r.statusCode != 201) _fail(r);
    return ExpenseType.fromJson((r.data as Map).cast<String, dynamic>());
  }

  Future<ExpenseType> updateExpenseType(
      String id, Map<String, dynamic> changes) async {
    final r = await _withRetry((o) =>
        _dio.patch('/analytics/expense-types/$id', data: changes, options: o));
    if (r.statusCode != 200) _fail(r);
    return ExpenseType.fromJson((r.data as Map).cast<String, dynamic>());
  }

  /// Fold one kind into another, moving its history with it.
  Future<ExpenseType> mergeExpenseType(String id, String intoId) async {
    final r = await _withRetry((o) => _dio.post(
        '/analytics/expense-types/$id/merge',
        data: {'into_id': intoId},
        options: o));
    if (r.statusCode != 200) _fail(r);
    return ExpenseType.fromJson((r.data as Map).cast<String, dynamic>());
  }

  Future<void> archiveExpenseType(String id) async {
    final r = await _withRetry(
        (o) => _dio.delete('/analytics/expense-types/$id', options: o));
    if (r.statusCode != 204) _fail(r);
  }

  /// Label one charge. [name] is resolved against existing kinds first,
  /// so passing "Fast Foods" reuses "Fast food" instead of forking it.
  Future<void> setTransactionExpenseType(
    String transactionId, {
    String? expenseTypeId,
    String? name,
    String? description,
    bool fromAi = false,
  }) async {
    final r = await _withRetry((o) => _dio.post(
        '/analytics/transactions/$transactionId/expense-type',
        data: {
          if (expenseTypeId != null) 'expense_type_id': expenseTypeId,
          if (name != null) 'name': name,
          if (description != null) 'description': description,
          'created_by': fromAi ? 'ai' : 'user',
        },
        options: o));
    if (r.statusCode != 200) _fail(r);
  }

  /// Label one line of an itemised receipt.
  Future<void> setAllocationExpenseType(
    String allocationId, {
    String? expenseTypeId,
    String? name,
    String? description,
    bool fromAi = false,
  }) async {
    final r = await _withRetry((o) => _dio.post(
        '/analytics/allocations/$allocationId/expense-type',
        data: {
          if (expenseTypeId != null) 'expense_type_id': expenseTypeId,
          if (name != null) 'name': name,
          if (description != null) 'description': description,
          'created_by': fromAi ? 'ai' : 'user',
        },
        options: o));
    if (r.statusCode != 200) _fail(r);
  }

  /// Filed spending with no kind yet: the labelling queue.
  Future<List<SpendRow>> untypedSpending({
    String period = 'last_90_days',
    int limit = 50,
  }) async {
    final r = await _withRetry((o) => _dio.get('/analytics/untyped',
        queryParameters: {'period': period, 'today': _localToday(), 'limit': limit},
        options: o));
    if (r.statusCode != 200) _fail(r);
    return ((r.data as Map)['transactions'] as List? ?? const [])
        .map((e) => SpendRow.fromJson({
              ...(e as Map).cast<String, dynamic>(),
              'expense_type': '',
            }))
        .toList();
  }

  // --- AI on top of the figures ------------------------------------- //

  /// Ask a question in plain English.
  ///
  /// The model compiles the question into a query; the server runs it
  /// and hands back both the number and the query it ran, so the UI can
  /// show what was actually asked.
  Future<SpendingAnswer> askAboutSpending(
    String question, {
    void Function(AiJob)? onProgress,
    List<Map<String, String>> history = const [],
  }) async {
    final job = await queueJob('ask', {
      'question': question,
      'today': _localToday(),
      // Prior turns, oldest first. Without them a follow-up like
      // "what about last month?" has nothing to refer to.
      if (history.isNotEmpty) 'history': history,
    });
    final done = await awaitJob(job.id, onProgress: onProgress);
    requireFinished(done, 'The assistant could not answer that.');
    return SpendingAnswer.fromJson(done.result!);
  }

  /// A plain-words read of the month, with the cards it was based on.
  Future<({String narrative, Insights insights})> narrateInsights({
    String period = 'this_month',
    void Function(AiJob)? onProgress,
  }) async {
    final job = await queueJob('narrate_insights', {
      'period': period,
      'today': _localToday(),
    });
    final done = await awaitJob(job.id, onProgress: onProgress);
    requireFinished(done, 'The assistant could not summarise that.');
    final r = done.result!;
    return (
      narrative: r['narrative'] as String? ?? '',
      insights: Insights.fromJson({
        'period': r['period'],
        'cards': r['cards'],
      }),
    );
  }

  /// What kind of spending a charge is. Suggests; does not file.
  Future<TypeSuggestionResult> suggestExpenseType({
    required String merchant,
    required int amountCents,
    String? itemDescription,
    void Function(AiJob)? onProgress,
  }) async {
    final job = await queueJob('classify_expense_type', {
      'merchant': merchant,
      'amount_cents': amountCents,
      if (itemDescription != null) 'item_description': itemDescription,
    });
    final done = await awaitJob(job.id, onProgress: onProgress);
    requireFinished(done, 'The assistant could not classify that.');
    return TypeSuggestionResult.fromJson(done.result!);
  }

  /// Read a receipt photo into balanced, categorised line items.
  ///
  /// [amountCents] is the real charge and is not optional: the whole
  /// design rests on the model being unable to argue with the total
  /// (see backend app/ai/receipt_loop.py). The result ALWAYS sums to
  /// it — anything the model could not account for comes back as one
  /// line flagged for review.
  /// Give it [imageBase64] or [text] — one of the two.
  ///
  /// Pasted text is a first-class input, not a fallback: sometimes the
  /// receipt arrived by email, and text is strictly easier to read than
  /// a photograph of curled thermal paper.
  Future<ReceiptScan> scanReceipt({
    String? imageBase64,
    String? text,
    required int amountCents,
    void Function(AiJob)? onProgress,
  }) async {
    assert(imageBase64 != null || text != null,
        'scanReceipt needs a photo or the receipt text');
    final job = await queueJob('receipt_scan', {
      if (imageBase64 != null) 'image_b64': imageBase64,
      if (text != null) 'text': text,
      'expected_cents': amountCents,
    });
    final done = await awaitJob(job.id, onProgress: onProgress,
        timeout: const Duration(minutes: 8));
    if (done.waitingForGpu) {
      // Not a failure. The caller shows why and offers to override.
      throw JobDeferred(done);
    }
    requireFinished(done, 'The assistant could not read that receipt.');
    return ReceiptScan.fromJson(done.result!);
  }

  /// Re-split a transaction that is already filed.
  ///
  /// The splits must sum to the charge; the server refuses otherwise,
  /// because a split that drifts would quietly change what the budget
  /// thinks was spent.
  /// Everything known about one transaction, including the AI's own
  /// reasoning for where it put it.
  Future<TxnDetail> transactionDetail(String transactionId) async {
    final r = await _withRetry((o) =>
        _dio.get('/budget/transactions/$transactionId', options: o));
    if (r.statusCode != 200) _fail(r);
    return TxnDetail.fromJson((r.data as Map).cast<String, dynamic>());
  }

  Future<void> refileTransaction(
    String transactionId,
    List<({String categoryId, int amountCents, String? description})> splits,
  ) async {
    final r = await _withRetry((o) => _dio.post(
          '/budget/transactions/$transactionId/refile',
          data: {
            'splits': [
              for (final s in splits)
                {
                  'category_id': s.categoryId,
                  'amount_cents': s.amountCents,
                  if (s.description != null) 'description': s.description,
                }
            ]
          },
          options: o,
        ));
    if (r.statusCode != 200) _fail(r);
  }

  /// Move money from one envelope to another.
  ///
  /// Two opposing ledger entries, so the total allocated is unchanged
  /// and the history can still say where the money went. Not a balance
  /// edit — that would leave the other envelope silently wrong.
  Future<void> transferBetweenCategories({
    required String fromCategoryId,
    required String toCategoryId,
    required int amountCents,
    String? note,
  }) async {
    final r = await _withRetry((o) => _dio.post('/budget/transfer', data: {
          'from_category_id': fromCategoryId,
          'to_category_id': toCategoryId,
          'amount_cents': amountCents,
          if (note != null) 'note': note,
        }, options: o));
    if (r.statusCode != 200) _fail(r);
  }

  /// Set what each envelope should hold, in one go.
  ///
  /// TARGETS, not deltas — "Groceries should hold $400". Envelopes left
  /// out keep what they have. The server refuses a total above what is
  /// actually in the bank (minus card debt) unless [allowOver].
  Future<void> reallocate(
    List<({String categoryId, int amountCents})> targets, {
    bool allowOver = false,
  }) async {
    final r = await _withRetry((o) => _dio.post('/budget/reallocate', data: {
          'allocations': [
            for (final t in targets)
              {'category_id': t.categoryId, 'amount_cents': t.amountCents}
          ],
          'allow_over_allocation': allowOver,
        }, options: o));
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

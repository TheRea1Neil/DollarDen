import 'dart:async';
import 'dart:convert';

import 'package:plaid_flutter/plaid_flutter.dart';

import 'api_client.dart';
import 'token_store.dart';

/// Outcome of a Link session, so the UI can distinguish "user changed their
/// mind" from "something broke".
sealed class LinkOutcome {
  const LinkOutcome();
}

class LinkSucceeded extends LinkOutcome {
  final String institutionName;
  final int accounts;
  const LinkSucceeded(this.institutionName, this.accounts);
}

class LinkCancelled extends LinkOutcome {
  const LinkCancelled();
}

class LinkFailed extends LinkOutcome {
  final String message;
  const LinkFailed(this.message);
}

/// Drives the native Plaid Link SDK.
///
/// The app never handles an access_token. Link returns a short-lived
/// public_token, which goes straight to the backend to be exchanged; the
/// long-lived token is created and stored server-side (docs/04).
class PlaidLinkService {
  final ApiClient _api;
  final TokenStore _tokens;

  StreamSubscription<LinkSuccess>? _onSuccess;
  StreamSubscription<LinkExit>? _onExit;
  Completer<LinkOutcome>? _pending;

  PlaidLinkService(this._api, [TokenStore? tokens])
      : _tokens = tokens ?? TokenStore();

  /// Open Link. With [itemId] this is UPDATE MODE: it repairs an existing
  /// connection rather than creating a new one, so no credit is consumed.
  Future<LinkOutcome> open({String? itemId}) async {
    if (_pending != null && !_pending!.isCompleted) {
      return const LinkFailed('A connection is already in progress.');
    }
    final completer = Completer<LinkOutcome>();
    _pending = completer;

    String token;
    try {
      token = await _api.linkToken(itemId: itemId);
    } on ApiException catch (e) {
      _pending = null;
      return LinkFailed(e.message);
    } catch (_) {
      _pending = null;
      return const LinkFailed('Could not reach the server.');
    }

    await _cancelSubscriptions();

    _onSuccess = PlaidLink.onSuccess.listen((success) async {
      try {
        final item = await _api.exchangePublicToken(success.publicToken);
        // Back the encrypted blob up to this device immediately. Doing it
        // here means the redundancy exists from the moment the connection
        // does, rather than depending on the user visiting a settings
        // screen later.
        await backupTokens();
        _complete(LinkSucceeded(item.displayName, item.accounts));
      } on ApiException catch (e) {
        _complete(LinkFailed(e.message));
      } catch (_) {
        _complete(const LinkFailed('Linked, but the server could not be reached.'));
      }
    });

    _onExit = PlaidLink.onExit.listen((exit) {
      final err = exit.error;
      if (err == null) {
        _complete(const LinkCancelled());
      } else {
        // displayMessage is the user-facing string Plaid supplies;
        // message is the developer-facing one and is always present.
        _complete(LinkFailed(err.displayMessage ?? err.message));
      }
    });

    try {
      await PlaidLink.create(configuration: LinkTokenConfiguration(token: token));
      await PlaidLink.open();
    } catch (e) {
      _complete(LinkFailed('Could not open Plaid Link: $e'));
    }

    return completer.future;
  }

  /// Pull the encrypted token blobs and keep them in the keychain.
  ///
  /// The blobs are useless without the server's TOKEN_ENCRYPTION_KEY, so
  /// this adds redundancy without putting bank access on the device.
  Future<bool> backupTokens() async {
    try {
      final payload = await _api.exportTokenBlobs();
      await _tokens.savePlaidBlobs(jsonEncode(payload));
      return true;
    } catch (_) {
      // Never let a backup failure break the link flow: the connection
      // itself is already safely stored server-side.
      return false;
    }
  }

  void _complete(LinkOutcome outcome) {
    final c = _pending;
    _pending = null;
    _cancelSubscriptions();
    if (c != null && !c.isCompleted) c.complete(outcome);
  }

  Future<void> _cancelSubscriptions() async {
    await _onSuccess?.cancel();
    await _onExit?.cancel();
    _onSuccess = null;
    _onExit = null;
  }

  Future<void> dispose() => _cancelSubscriptions();
}

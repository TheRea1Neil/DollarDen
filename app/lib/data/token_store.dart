import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Credentials live in the OS keychain / keystore, never SharedPreferences
/// (docs/07).
///
/// This also holds the encrypted Plaid token blobs exported by the backend
/// for redundancy. Those are CIPHERTEXT the app cannot decrypt -- the server
/// holds the key -- so the app never possesses a usable Plaid token, while
/// still giving a second copy if the Pi is lost (docs/04).
class TokenStore {
  static const _access = 'dollarden.access_token';
  static const _refresh = 'dollarden.refresh_token';
  static const _blobs = 'dollarden.plaid_token_blobs';

  final FlutterSecureStorage _storage;

  TokenStore([FlutterSecureStorage? storage])
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
              iOptions: IOSOptions(
                // Never leaves this device, and is not restored to a new one
                // from an iCloud backup.
                accessibility: KeychainAccessibility.first_unlock_this_device,
              ),
            );

  Future<String?> get accessToken => _storage.read(key: _access);
  Future<String?> get refreshToken => _storage.read(key: _refresh);

  Future<void> saveTokens(String access, String refresh) async {
    await _storage.write(key: _access, value: access);
    await _storage.write(key: _refresh, value: refresh);
  }

  Future<void> saveAccessToken(String access) =>
      _storage.write(key: _access, value: access);

  Future<void> clear() async {
    await _storage.delete(key: _access);
    await _storage.delete(key: _refresh);
    // Deliberately NOT clearing the Plaid blobs: they are the off-box
    // backup. Wiping them on logout would discard the redundancy the user
    // signed up for, and they are useless without the server's key anyway.
  }

  Future<String?> get plaidBlobs => _storage.read(key: _blobs);
  Future<void> savePlaidBlobs(String json) =>
      _storage.write(key: _blobs, value: json);
}

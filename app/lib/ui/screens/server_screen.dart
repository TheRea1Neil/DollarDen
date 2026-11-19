import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/api_client.dart';
import '../../data/providers.dart';

/// Choose which backend this install talks to.
///
/// This comes before signing in, because there is nothing to sign in to
/// until it is answered. Every household runs its own server, so there
/// is no sensible default to assume — the same reason a Jellyfin client
/// asks before it asks for a password.
///
/// The URL is checked before it is saved. The alternative is someone
/// mistyping their address and then being told, repeatedly and
/// inaccurately, that their password is wrong.
class ServerScreen extends ConsumerStatefulWidget {
  /// Shown when changing servers rather than choosing a first one: adds
  /// a way back out and starts from the current address.
  final String? current;

  const ServerScreen({super.key, this.current});

  @override
  ConsumerState<ServerScreen> createState() => _ServerScreenState();
}

class _ServerScreenState extends ConsumerState<ServerScreen> {
  late final TextEditingController _url =
      TextEditingController(text: widget.current ?? '');
  bool _checking = false;
  String? _error;

  @override
  void dispose() {
    _url.dispose();
    super.dispose();
  }

  String _normalise(String raw) {
    var v = raw.trim().replaceAll(RegExp(r'/+$'), '');
    if (v.isEmpty) return v;
    // Typing "budget.example.org" is the common case and is not a
    // mistake worth an error message, so a missing scheme becomes
    // https. Only the ASSUMED scheme though — an explicit http:// is
    // left alone, because plenty of self-hosters run this on a LAN
    // address with no certificate, and refusing that would break a
    // legitimate setup. It is warned about below instead.
    if (!v.startsWith('http://') && !v.startsWith('https://')) {
      v = 'https://$v';
    }
    return v;
  }

  Future<void> _connect() async {
    final url = _normalise(_url.text);
    if (url.isEmpty) {
      setState(() => _error = 'Enter the address of your server.');
      return;
    }
    setState(() {
      _checking = true;
      _error = null;
    });

    final ok = await ApiClient.probe(url);
    if (!mounted) return;

    if (!ok) {
      setState(() {
        _checking = false;
        _error = 'Could not reach a budget server at $url.\n\n'
            'Check the address, and that the server is running and '
            'reachable from this device.';
      });
      return;
    }

    // Changing servers ends the current session: tokens are issued by
    // one backend and mean nothing to another.
    if (widget.current != null && widget.current != url) {
      await ref.read(baseUrlProvider.notifier).forget();
    }
    await ref.read(baseUrlProvider.notifier).set(url);
    if (!mounted) return;
    setState(() => _checking = false);
    if (widget.current != null) Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final changing = widget.current != null;

    return Scaffold(
      appBar: changing ? AppBar(title: const Text('Change server')) : null,
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  if (!changing) ...[
                    Icon(Icons.dns_outlined,
                        size: 48, color: theme.colorScheme.primary),
                    const SizedBox(height: 20),
                    Text('Connect to your server',
                        textAlign: TextAlign.center,
                        style: theme.textTheme.headlineSmall
                            ?.copyWith(fontWeight: FontWeight.w600)),
                    const SizedBox(height: 10),
                    Text(
                      'This app does not store your budget — your own '
                      'server does. Enter its address to get started.',
                      textAlign: TextAlign.center,
                      style: theme.textTheme.bodyMedium?.copyWith(
                          color: theme.colorScheme.onSurfaceVariant),
                    ),
                    const SizedBox(height: 28),
                  ],
                  TextField(
                    controller: _url,
                    autocorrect: false,
                    enableSuggestions: false,
                    keyboardType: TextInputType.url,
                    textInputAction: TextInputAction.go,
                    onChanged: (_) => setState(() {}),
                    onSubmitted: (_) => _checking ? null : _connect(),
                    decoration: const InputDecoration(
                      border: OutlineInputBorder(),
                      labelText: 'Server address',
                      hintText: 'budget.example.org',
                      prefixIcon: Icon(Icons.link),
                    ),
                  ),
                  const SizedBox(height: 8),
                  Text(
                    'Assumes https if you leave the scheme off. Ask '
                    'whoever set up your server if you are not sure of '
                    'the address.',
                    style: theme.textTheme.bodySmall?.copyWith(
                        color: theme.colorScheme.onSurfaceVariant),
                  ),
                  // Allowed, because a LAN install with no certificate
                  // is a real and reasonable setup. Said out loud,
                  // because the thing about to travel over it is a
                  // password to an account holding bank data.
                  if (_url.text.trim().startsWith('http://')) ...[
                    const SizedBox(height: 10),
                    Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Icon(Icons.info_outline,
                            size: 16, color: theme.colorScheme.tertiary),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            'This address is not encrypted. Fine on your '
                            'own network; not over the internet.',
                            style: theme.textTheme.bodySmall?.copyWith(
                                color: theme.colorScheme.tertiary),
                          ),
                        ),
                      ],
                    ),
                  ],
                  if (_error != null) ...[
                    const SizedBox(height: 16),
                    Container(
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: theme.colorScheme.errorContainer,
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: Text(_error!,
                          style: TextStyle(
                              color: theme.colorScheme.onErrorContainer)),
                    ),
                  ],
                  const SizedBox(height: 20),
                  FilledButton(
                    onPressed: _checking ? null : _connect,
                    style: FilledButton.styleFrom(
                        minimumSize: const Size.fromHeight(50)),
                    child: _checking
                        ? const SizedBox(
                            height: 20,
                            width: 20,
                            child: CircularProgressIndicator(strokeWidth: 2))
                        : const Text('Connect'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

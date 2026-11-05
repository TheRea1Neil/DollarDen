import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme.dart';
import '../../data/api_client.dart';
import '../../data/plaid_link_service.dart';
import '../../data/providers.dart';
import '../../domain/models.dart';

/// Linked bank connections.
///
/// There is deliberately no "disconnect" action. Removing a Plaid Item is
/// irreversible in the sense that matters: re-linking consumes one of a
/// finite number of connection credits (docs/04).
class ConnectionsScreen extends ConsumerStatefulWidget {
  const ConnectionsScreen({super.key});

  @override
  ConsumerState<ConnectionsScreen> createState() => _ConnectionsScreenState();
}

class _ConnectionsScreenState extends ConsumerState<ConnectionsScreen> {
  bool _busy = false;

  Future<void> _link({String? itemId}) async {
    setState(() => _busy = true);
    try {
      final outcome = await ref.read(plaidLinkProvider).open(itemId: itemId);
      if (!mounted) return;

      switch (outcome) {
        case LinkSucceeded(:final institutionName, :final accounts):
          ref.invalidate(plaidItemsProvider);
          ref.invalidate(summaryProvider);
          _toast(itemId == null
              ? 'Connected $institutionName — $accounts accounts'
              : 'Reconnected $institutionName');
        case LinkCancelled():
          break; // the user changed their mind; say nothing
        case LinkFailed(:final message):
          _toast(message, error: true);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _sync(PlaidItem item) async {
    setState(() => _busy = true);
    try {
      final r = await ref.read(apiProvider).syncItem(item.id);
      ref.invalidate(summaryProvider);
      ref.invalidate(reviewProvider);
      if (!mounted) return;
      _toast(r.added == 0
          ? 'Up to date'
          : '${r.added} new · ${r.autoCategorised} auto-categorised · '
              '${r.needsReview} to review');
    } on ApiException catch (e) {
      if (!mounted) return;
      if (e.isConflict) {
        // The backend returns 409 when an Item needs re-authentication,
        // pointing at update mode rather than failing opaquely.
        ref.invalidate(plaidItemsProvider);
        _toast('This connection needs reconnecting', error: true);
      } else {
        _toast(e.message, error: true);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _toast(String message, {bool error = false}) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(message),
      backgroundColor: error ? Theme.of(context).colorScheme.error : null,
    ));
  }

  @override
  Widget build(BuildContext context) {
    final items = ref.watch(plaidItemsProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Accounts'),
        actions: [
          IconButton(
            tooltip: 'Back up connection keys to this device',
            icon: const Icon(Icons.shield_outlined),
            onPressed: _busy ? null : _backup,
          ),
        ],
      ),
      body: items.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => Center(child: Text('$e')),
        data: (list) => RefreshIndicator(
          onRefresh: () async {
            // This screen is where balances live, so a pull here has to
            // mean "ask the bank", not "re-read what we were last told".
            try {
              await ref.read(apiProvider).refreshBanks();
            } catch (_) {}
            ref.invalidate(plaidItemsProvider);
            ref.invalidate(summaryProvider);
            await ref.read(plaidItemsProvider.future);
          },
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              if (list.isEmpty)
                const _NoConnections()
              else
                for (final item in list) ...[
                  _ConnectionCard(
                    item: item,
                    busy: _busy,
                    onSync: () => _sync(item),
                    onReconnect: () => _link(itemId: item.id),
                  ),
                  const SizedBox(height: 10),
                ],
              const SizedBox(height: 8),
              OutlinedButton.icon(
                onPressed: _busy ? null : () => _link(),
                icon: const Icon(Icons.add),
                label: Text(list.isEmpty ? 'Connect a bank' : 'Connect another bank'),
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size.fromHeight(50),
                  shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12)),
                ),
              ),
              if (list.isNotEmpty) ...[
                const SizedBox(height: 12),
                Text(
                  'Each bank you connect uses one of your connection credits. '
                  'Reconnecting an existing bank does not.',
                  textAlign: TextAlign.center,
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                      color: Theme.of(context).colorScheme.onSurfaceVariant),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _backup() async {
    setState(() => _busy = true);
    final ok = await ref.read(plaidLinkProvider).backupTokens();
    if (!mounted) return;
    setState(() => _busy = false);
    _toast(ok
        ? 'Connection keys backed up to this device'
        : 'Could not back up right now', error: !ok);
  }
}

class _ConnectionCard extends StatelessWidget {
  final PlaidItem item;
  final bool busy;
  final VoidCallback onSync;
  final VoidCallback onReconnect;

  const _ConnectionCard({
    required this.item,
    required this.busy,
    required this.onSync,
    required this.onReconnect,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final needsFixing = item.needsReauth;

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(
                  needsFixing ? Icons.link_off : Icons.account_balance,
                  color: needsFixing ? AppTheme.warning : theme.colorScheme.primary,
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(item.displayName,
                          style: theme.textTheme.titleMedium
                              ?.copyWith(fontWeight: FontWeight.w600)),
                      Text(
                        '${item.accounts} account${item.accounts == 1 ? '' : 's'}',
                        style: theme.textTheme.bodySmall?.copyWith(
                            color: theme.colorScheme.onSurfaceVariant),
                      ),
                    ],
                  ),
                ),
              ],
            ),
            if (needsFixing) ...[
              const SizedBox(height: 12),
              Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: AppTheme.warning.withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Text(
                  'Your bank needs you to sign in again. This repairs the '
                  'existing connection and does not use another credit.',
                  style: theme.textTheme.bodySmall,
                ),
              ),
              const SizedBox(height: 10),
              FilledButton.tonal(
                onPressed: busy ? null : onReconnect,
                child: const Text('Reconnect'),
              ),
            ] else ...[
              const SizedBox(height: 10),
              Row(
                children: [
                  TextButton.icon(
                    onPressed: busy ? null : onSync,
                    icon: const Icon(Icons.sync, size: 18),
                    label: const Text('Sync now'),
                  ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _NoConnections extends StatelessWidget {
  const _NoConnections();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 40),
      child: Column(
        children: [
          Icon(Icons.account_balance_outlined,
              size: 48, color: theme.colorScheme.onSurfaceVariant),
          const SizedBox(height: 16),
          Text('No banks connected', style: theme.textTheme.titleMedium),
          const SizedBox(height: 6),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24),
            child: Text(
              'Connect a bank to pull in transactions automatically.',
              textAlign: TextAlign.center,
              style: theme.textTheme.bodyMedium
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
          ),
        ],
      ),
    );
  }
}

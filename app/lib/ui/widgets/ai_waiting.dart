import 'package:flutter/material.dart';

import '../../data/api_client.dart';

/// Why the AI is not answering, in the user's terms.
///
/// Every screen used to invent its own wording, and four of them did not
/// distinguish "deferred" from "failed" at all -- so asking a question
/// while a game was running reported that the assistant could not answer,
/// when nothing had failed and the work was queued and waiting.
///
/// One widget so the explanation is the same everywhere: what is
/// happening, when it will sort itself out, and the choice to override.
class AiWaiting extends StatelessWidget {
  final JobDeferred deferred;
  final VoidCallback? onRunAnyway;

  /// Compact form for a screen where this is one element among many,
  /// rather than the whole page.
  final bool inline;

  const AiWaiting({
    super.key,
    required this.deferred,
    this.onRunAnyway,
    this.inline = false,
  });

  /// "15:36" from the server's ISO timestamp, in local time.
  ///
  /// The retry is an hour out, so "try again later" without a time
  /// leaves the user guessing whether to wait or give up.
  String? get _when {
    final raw = deferred.job.retryAt;
    if (raw == null) return null;
    final t = DateTime.tryParse(raw)?.toLocal();
    if (t == null) return null;
    return '${t.hour.toString().padLeft(2, '0')}:'
        '${t.minute.toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final when = _when;

    final body = <Widget>[
      Text(
        deferred.reason,
        textAlign: inline ? TextAlign.start : TextAlign.center,
        style: theme.textTheme.bodyMedium,
      ),
      if (when != null) ...[
        const SizedBox(height: 6),
        Text(
          'It will start on its own around $when — you do not have to '
          'stay here.',
          textAlign: inline ? TextAlign.start : TextAlign.center,
          style: theme.textTheme.bodySmall
              ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
        ),
      ],
      if (onRunAnyway != null) ...[
        const SizedBox(height: 16),
        FilledButton.tonalIcon(
          onPressed: onRunAnyway,
          icon: const Icon(Icons.play_arrow, size: 18),
          label: const Text('Run it now anyway'),
        ),
        const SizedBox(height: 6),
        Text(
          'Uses the graphics card straight away, which may stutter a '
          'game that is running.',
          textAlign: inline ? TextAlign.start : TextAlign.center,
          style: theme.textTheme.bodySmall
              ?.copyWith(color: theme.colorScheme.outline),
        ),
      ],
    ];

    if (inline) {
      return Card(
        color: theme.colorScheme.surfaceContainerHighest,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(children: [
                Icon(Icons.hourglass_empty,
                    size: 18, color: theme.colorScheme.onSurfaceVariant),
                const SizedBox(width: 8),
                Text('Waiting for the AI computer',
                    style: theme.textTheme.titleSmall),
              ]),
              const SizedBox(height: 10),
              ...body,
            ],
          ),
        ),
      );
    }

    return Center(
      child: Padding(
        padding: const EdgeInsets.all(28),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.hourglass_empty,
                size: 40, color: theme.colorScheme.outline),
            const SizedBox(height: 16),
            Text('Waiting for the AI computer',
                style: theme.textTheme.titleMedium),
            const SizedBox(height: 10),
            ...body,
          ],
        ),
      ),
    );
  }
}

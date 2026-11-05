import 'package:flutter/material.dart';

import '../../core/money.dart';
import '../../core/theme.dart';
import '../../domain/models.dart';

/// A category as a balance, not a report line.
///
/// The number that matters is what is LEFT, so it is the largest thing on
/// the row. The progress bar is secondary and only appears when there is a
/// target to measure against.
class CategoryTile extends StatelessWidget {
  final Category category;
  final VoidCallback? onTap;

  const CategoryTile({super.key, required this.category, this.onTap});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final status = AppTheme.statusColor(
      context,
      overspent: category.overspent,
      belowAlert: category.belowAlert,
    );
    final ratio = category.fillRatio;

    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(16),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        category.name,
                        style: theme.textTheme.titleMedium
                            ?.copyWith(fontWeight: FontWeight.w600),
                        overflow: TextOverflow.ellipsis,
                      ),
                      if (category.targetCents != null)
                        Text(
                          'of ${Money.formatCompact(category.targetCents!)}',
                          style: theme.textTheme.bodySmall?.copyWith(
                            color: theme.colorScheme.onSurfaceVariant,
                          ),
                        ),
                    ],
                  ),
                ),
                const SizedBox(width: 12),
                Column(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Text(
                      Money.format(category.balanceCents),
                      style: theme.textTheme.titleLarge?.copyWith(
                        fontWeight: FontWeight.w700,
                        color: status,
                        fontFeatures: const [FontFeature.tabularFigures()],
                      ),
                    ),
                    if (category.overspent)
                      Text('overspent',
                          style: theme.textTheme.labelSmall
                              ?.copyWith(color: AppTheme.danger))
                    else if (category.belowAlert)
                      Text('running low',
                          style: theme.textTheme.labelSmall
                              ?.copyWith(color: AppTheme.warning)),
                  ],
                ),
              ],
            ),
            if (ratio != null) ...[
              const SizedBox(height: 10),
              ClipRRect(
                borderRadius: BorderRadius.circular(4),
                child: LinearProgressIndicator(
                  value: ratio,
                  minHeight: 6,
                  backgroundColor:
                      theme.colorScheme.surfaceContainerHighest.withValues(alpha: 0.6),
                  // Continuous, not the three-step status colour: the
                  // bar is the one place a gradual change reads better
                  // than a threshold, because it is already showing a
                  // continuous quantity.
                  valueColor:
                      AlwaysStoppedAnimation(AppTheme.fullnessColor(ratio)),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

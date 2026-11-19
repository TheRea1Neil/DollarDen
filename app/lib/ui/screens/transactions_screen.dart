import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/providers.dart';
import 'history_screen.dart';
import 'review_screen.dart';

/// Transactions: the record, and the ones still needing a decision.
///
/// These were two separate places showing the same thing at two stages
/// of its life — Review until it is filed, History forever after — and
/// splitting them meant the obvious follow-up to filing a charge ("so
/// where did that end up?") was in a different tab reached from a
/// different screen.
///
/// Review is first: the first question on opening this tab is "is there
/// anything I need to deal with?", and History is what you go to once
/// that is answered. It still carries the count, both here and on the
/// bottom bar, so a glance at the nav is enough to know.
///
/// An [IndexedStack] rather than a [TabBarView]: both sections keep
/// their state — a half-typed search, a scrolled-back month, a loaded
/// page of results — regardless of how often you switch. It also
/// matches how the bottom bar holds the four top-level screens.
class TransactionsScreen extends ConsumerStatefulWidget {
  const TransactionsScreen({super.key});

  @override
  ConsumerState<TransactionsScreen> createState() => _TransactionsScreenState();
}

class _TransactionsScreenState extends ConsumerState<TransactionsScreen>
    with SingleTickerProviderStateMixin {
  late final TabController _tabs;
  int _section = 0;

  @override
  void initState() {
    super.initState();
    _tabs = TabController(length: 2, vsync: this);
    // "Dismiss old history" acts on the review queue, so it is only
    // offered while Review is the section on screen. That means the app
    // bar has to rebuild when the section changes, which a TabBar alone
    // would not cause.
    //
    // Guarded on the index actually changing: a TabController is an
    // Animation and notifies on every frame of the indicator slide, so
    // an unguarded setState here rebuilds both sections a dozen times
    // per tap for one boolean that changed once.
    _tabs.addListener(() {
      if (mounted && _tabs.index != _section) {
        setState(() => _section = _tabs.index);
      }
    });
  }

  @override
  void dispose() {
    _tabs.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final pending = ref
        .watch(summaryProvider)
        .maybeWhen(data: (s) => s.pendingReview, orElse: () => 0);
    final onReview = _section == 0;
    final theme = Theme.of(context);

    return Scaffold(
      // No title bar. "Transactions" is already the word under the icon
      // on the bottom bar, and repeating it cost a full toolbar's height
      // on the one screen that is nothing but two long lists. The tab
      // row carries the labels, and the dismiss action sits in it.
      //
      // `toolbarHeight: 0` rather than dropping the AppBar entirely: it
      // still handles the status bar inset and the surface tint the rest
      // of the app's bars have, so this reads as the same app.
      appBar: AppBar(
        toolbarHeight: 0,
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(49),
          // The TabBar draws its own underline, but only as wide as the
          // TabBar — which stops short of the action slot and leaves a
          // stub of a line with a bare gap beside it. Its divider is
          // switched off and drawn here instead, across the full width.
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              SizedBox(
                height: 48,
                child: Row(
                  children: [
                    Expanded(
                      child: TabBar(
                        controller: _tabs,
                        dividerHeight: 0,
                        tabs: [
                          Tab(
                            child: Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                const Text('Review'),
                                if (pending > 0) ...[
                                  const SizedBox(width: 8),
                                  Badge.count(count: pending),
                                ],
                              ],
                            ),
                          ),
                          const Tab(text: 'History'),
                        ],
                      ),
                    ),
                    // The slot is always the same width, occupied or not.
                    // Letting the button appear and vanish changes how much
                    // room is left for the tabs, so both labels slide sideways
                    // every time you switch section — for a control that has
                    // nothing to do with where the labels sit.
                    SizedBox(
                      width: 48,
                      child: onReview
                          ? IconButton(
                              tooltip: 'Dismiss old history',
                              icon: const Icon(Icons.history_toggle_off),
                              // Set explicitly: an IconButton in an AppBar's
                              // `bottom` does not inherit the bar's icon theme
                              // the way one in `actions` does, and defaulted
                              // to near-invisible.
                              color: theme.colorScheme.onSurfaceVariant,
                              onPressed: () => dismissOldHistory(context, ref),
                            )
                          : null,
                    ),
                    const SizedBox(width: 4),
                  ],
                ),
              ),
              const Divider(height: 1, thickness: 1),
            ],
          ),
        ),
      ),
      body: IndexedStack(
        index: _section,
        children: const [
          ReviewScreen(embedded: true),
          HistoryScreen(embedded: true),
        ],
      ),
    );
  }
}

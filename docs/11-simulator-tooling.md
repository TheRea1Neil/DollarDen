# Driving the iOS simulator

`tools/sim` lets me see the app and interact with it, so UI work is a
look-change-look loop rather than guesswork from source.

## What it is

Two capabilities, from two sources:

- **Seeing** — `xcrun simctl io screenshot`. Built into Xcode, no setup.
- **Interacting** — [idb](https://fbidb.io), Meta's iOS bridge. Gives
  tap, swipe, typing, and the thing that actually matters: the live
  **accessibility tree**.

The accessibility tree is the point. Without it, tapping means reading
pixel coordinates off a screenshot and hoping the layout does not move.
With it, `sim tap "Review"` finds the element whose label says Review
and taps its centre — which keeps working when the layout changes.

## Setup

```bash
brew install facebook/fb/idb-companion
cd tools && python3 -m venv .idbvenv && .idbvenv/bin/pip install fb-idb
```

The venv is gitignored. No OS permissions are needed — an earlier
AppleScript approach was abandoned because it required granting
Accessibility access to the terminal, which is a manual step and a
standing permission.

## Using it

```bash
cd tools

./sim boot                  # boot a simulator (default: iPhone 17)
./sim shot budget           # -> tools/shots/budget.png
./sim tree                  # every element: type, frame, label
./sim find "Paycheck"       # matching elements, best candidate first
./sim tap "Review"          # tap by label
./sim tapxy 200 400         # tap a point, when there is no label
./sim type "2000"
./sim key return
./sim scroll down
./sim swipe 200 600 200 200
./sim back
./sim relaunch
```

`tap` ranks candidates: tappable types (Button, Cell, TextField) before
static text, exact label matches before partial, shorter labels before
longer. Searching "Review" on the budget screen otherwise finds the
summary text "37 to review" before the Review tab.

## Getting the current build onto the simulator

The simulator does not share a build with the phone, and a stale one is
easy to mistake for a bug — the first screenshot taken here showed an
old layout and sent me looking for a regression that was not there.

```bash
cd app
flutter build ios --simulator --debug
xcrun simctl install booted build/ios/iphonesimulator/Runner.app
cd ../tools && ./sim relaunch
```

Debug builds are fine on the simulator; only a physical device needs
release (JIT needs a debugger attached, which is why a debug build dies
instantly when launched standalone on the phone).

## Limits

- Flutter renders to a canvas, so the tree is what Flutter's semantics
  layer exports. A widget with no semantics is invisible here — if
  something cannot be found, it may need a `Semantics` label, which is
  worth adding anyway.
- Text in a single `Text` widget arrives as one label with newlines, so
  a card reads as `"Tithing / $400.00 / Rent / $350.00"`. Fine for
  finding, poor for asserting.
- No gesture recording. Drag-to-reorder has to be driven with explicit
  `swipe` calls.

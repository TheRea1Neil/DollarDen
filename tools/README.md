# tools

`sim` — drive the iOS simulator: screenshot it, read its accessibility
tree, tap and type. Built for UI/UX work on the app.

See `docs/11-simulator-tooling.md` for setup and usage.

```bash
brew install facebook/fb/idb-companion
python3 -m venv .idbvenv && .idbvenv/bin/pip install fb-idb
./sim shot && ./sim tree
```

## Driving an Android device: `tools/droid`

The Android twin of `tools/sim`, same idea: find elements by LABEL from
the live view hierarchy rather than by pixel coordinates read off a
screenshot, so a script survives a layout change.

    ./tools/droid tree                 every labelled element, with a tap point
    ./tools/droid find <text>          matching elements
    ./tools/droid tap <text>           tap the first clickable match
    ./tools/droid type <text>          type into whatever has focus
    ./tools/droid shot <name>          screenshot to tools/shots/
    ./tools/droid install <apk>        adb install -r
    ./tools/droid logcat [pattern]     this app's logs only

Needs USB debugging on the device and the cable authorised. `adb` is
found automatically; the SDK here is the homebrew cask at
`/opt/homebrew/share/android-commandlinetools`, not the Android Studio
default, so it is not on `PATH` for a non-interactive shell.

### Things that cost time, learned driving the Plaid OAuth flow

- **Re-read the tree after every step.** The soft keyboard shifts the
  whole layout; a coordinate read before typing points somewhere else
  after. This is what `tap <label>` is for -- reaching for `tapxy`
  opened a settings dialog mid-login.
- **`tap` prefers a clickable match.** Flutter wraps text in
  non-clickable Views that match the same words as the button you want
  ("Choose which accounts count" shadowing the Accounts tab).
- **Never scroll up in a Chrome Custom Tab.** An upward swipe triggers
  pull-to-refresh, which reloads the page and invalidates the OAuth
  `state`, sending you back to the start of the bank login.
- **The view hierarchy reports elements that are not visible.** Plaid's
  sandbox pins a "Sandbox mode" banner over the bottom of the page;
  the terms checkbox and submit button sit behind it at coordinates
  that look fine in the tree and land on the banner. Scroll until they
  clear it.
- **Google Password Manager interrupts twice** -- once offering a saved
  credential for `cdn.plaid.com`, once warning that `pass_good` was
  found in a breach (it is a public test password). Both are bottom
  sheets that steal focus; dismiss with `key back` / `tap OK`.

## Personal builds: `tools/personal`

The committed icons and in-app logo are the public DollarDen logo
(`brand/dollarden-logo.jpeg`). For a personal build with your own logo,
put the art at `brand/personal/logo.jpeg` (gitignored, never pushed) and
prefix any flutter command:

    tools/personal build apk --release
    tools/personal run --release -d <device>

It regenerates every icon from the personal art, runs flutter, then puts
the public icons back, even if the build fails or you Ctrl-C it.
Needs Pillow: `python3 -m venv tools/.imgvenv && tools/.imgvenv/bin/pip install Pillow`.

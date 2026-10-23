# tools

`sim` — drive the iOS simulator: screenshot it, read its accessibility
tree, tap and type. Built for UI/UX work on the app.

See `docs/11-simulator-tooling.md` for setup and usage.

```bash
brew install facebook/fb/idb-companion
python3 -m venv .idbvenv && .idbvenv/bin/pip install fb-idb
./sim shot && ./sim tree
```

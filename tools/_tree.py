"""Render or search the simulator's accessibility tree.

The tree is what makes this useful: elements carry their labels, so a
button can be found by what it says rather than by guessing coordinates
off a screenshot. Labels survive layout changes; coordinates do not.
"""
import json
import sys


def rows(data):
    for el in data:
        f = el.get("frame") or {}
        if not f:
            continue
        label = (el.get("AXLabel") or el.get("title") or "")
        label = label.replace("\n", " / ")
        yield (
            int(f.get("x", 0)), int(f.get("y", 0)),
            int(f.get("width", 0)), int(f.get("height", 0)),
            el.get("type", "?"), label,
        )


# Things a tap can meaningfully land on, best first. Without this,
# searching for "Review" finds the summary text "37 to review" before
# the Review tab, and tapping does nothing.
_TAPPABLE = ("Button", "Link", "Cell", "SwitchButton", "TextField",
             "SearchField", "TabBar")


def rank(kind: str, label: str, needle: str) -> tuple:
    """Lower sorts first: tappable before text, exact before contained,
    short labels before long ones (a label listing half the screen is
    a container, not the thing you meant)."""
    tappable = 0 if kind in _TAPPABLE else 1
    exact = 0 if label.strip().lower() == needle else 1
    return (tappable, exact, len(label))


def main() -> None:
    mode = sys.argv[1]
    needle = sys.argv[2].lower() if len(sys.argv) > 2 else ""
    data = json.load(sys.stdin)

    if mode == "find":
        hits = [r for r in rows(data) if needle and needle in r[5].lower()]
        hits.sort(key=lambda r: rank(r[4], r[5], needle))
        for x, y, w, h, kind, label in hits:
            print("%d %d %d %d | %s | %s" % (x, y, w, h, kind, label[:70]))
        return

    for x, y, w, h, kind, label in rows(data):
        if True:
            # Whole-screen containers carry no information worth a line.
            if not label and kind in ("Application", "Window", "Other"):
                continue
            print("%-14s (%4d,%4d) %4dx%-4d %s"
                  % (kind, x, y, w, h, label[:88]))


if __name__ == "__main__":
    main()

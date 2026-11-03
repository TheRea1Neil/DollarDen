"""Parse a uiautomator XML dump into one line per interesting node.

Same idea as _tree.py for the iOS simulator: find elements by LABEL, not
by pixel coordinates read off a screenshot, so the script survives a
layout change.
"""
import re
import sys
import xml.etree.ElementTree as ET

BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def label(n):
    for k in ("text", "content-desc", "resource-id"):
        v = (n.get(k) or "").strip()
        if v:
            return v.rsplit("/", 1)[-1] if k == "resource-id" else v
    return ""


def main():
    data = sys.stdin.read()
    # The dump command prints a status line before the XML on some builds.
    start = data.find("<?xml")
    if start > 0:
        data = data[start:]
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        print(f"could not parse dump: {e}", file=sys.stderr)
        return 1

    want = (sys.argv[1].lower() if len(sys.argv) > 1 else None)
    for n in root.iter("node"):
        lab = label(n)
        cls = (n.get("class") or "").rsplit(".", 1)[-1]
        # An empty text field has no text, no content-desc and often no
        # resource-id -- but it is the thing you most need to tap. Give it
        # a synthetic label rather than dropping it from the tree.
        if not lab and cls == "EditText":
            lab = "<empty field>"
        if not lab:
            continue
        m = BOUNDS.match(n.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        if want and want not in lab.lower():
            continue
        tap = "tap" if n.get("clickable") == "true" else "   "
        print(f"{(x1+x2)//2:5d} {(y1+y2)//2:5d} {tap} | {cls:16s} | {lab}")
    return 0


sys.exit(main())

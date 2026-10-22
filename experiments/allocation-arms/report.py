"""Summarise the arms into one table."""
from __future__ import annotations

import pathlib
import glob, json, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from scenarios import ORDER, SCENARIOS

def load(pat):
    return {f: json.load(open(f)) for f in sorted(glob.glob(pat))}

print("=" * 96)
print("ARMS B (pure AI) and C (AI + deterministic repair)")
print("=" * 96)
for f, d in load("results-*.json").items():
    rows = d["rows"]
    nb = nc = ncons = ntot = 0
    secs, inv_b, inv_c, worst = [], 0, 0, 0
    detail = []
    for r in rows:
        for run in r["runs"]:
            if not run.get("ok"):
                ntot += 1
                continue
            ntot += 1
            secs.append(run["secs"])
            sb, sc = run["score_b"], run["score_c"]
            nb += sb["exact"]; nc += sc["exact"]; ncons += sb["conserved"]
            inv_b += bool(sb["inversions"]); inv_c += bool(sc["inversions"])
            worst = max(worst, abs(sb["conservation_error"]))
        s0 = r["runs"][0]
        if s0.get("ok"):
            detail.append((r["scenario"]["id"], s0["score_b"], s0["score_c"]))
    ss = sorted(secs)
    lat = (f"median {ss[len(ss)//2]:.1f}s, max {max(ss):.1f}s" if ss else "n/a")
    print(f"\n{d['model']}   ({ntot} runs, {lat})")
    print(f"  (recording a paycheck is interactive; the engine answers in "
          f"microseconds, and a sleeping AI host adds ~10s on top)")
    print(f"  B exact            {nb}/{ntot}")
    print(f"  B conserved money  {ncons}/{ntot}   worst miss ${worst/100:,.2f}")
    print(f"  B priority inversions in {inv_b}/{ntot}")
    print(f"  C exact            {nc}/{ntot}   (repair fixes {nc-nb})")
    print(f"  C priority inversions in {inv_c}/{ntot}")
    print(f"\n  {'scenario':22}{'B':>12}{'C':>12}   what B got wrong")
    print("  " + "-" * 88)
    for sid, sb, sc in detail:
        bm = "exact" if sb["exact"] else f"{sb['conservation_error']/100:+,.2f}"
        cm = "exact" if sc["exact"] else f"max {sc['max_err_cents']/100:,.2f}"
        print(f"  {sid:22}{bm:>12}{cm:>12}   {','.join(sb['wrong'])[:46]}")

print("\n" + "=" * 96)
print("ARM D (prose compiled to rules once, engine executes every payday)")
print("=" * 96)
for f, d in load("armd-*.json").items():
    print(f"\n{d['model']}")
    for i, at in enumerate(d["attempts"], 1):
        if not at.get("ok"):
            print(f"  attempt {i}: FAILED {at['err'][:80]}")
            continue
        res = at["results"]
        ok = [v for v in res.values() if "score" in v]
        exact = sum(1 for v in ok if v["score"]["exact"])
        errs = [k for k, v in res.items() if "error" in v]
        print(f"  attempt {i}: {at['secs']:.1f}s to compile   "
              f"exact {exact}/{len(res)}   "
              f"conserved {sum(1 for v in ok if v['score']['conserved'])}/{len(ok)}"
              + (f"   engine errors: {errs}" if errs else ""))
        bad = [(k, v["score"]) for k, v in res.items()
               if "score" in v and not v["score"]["exact"]]
        for k, sc in bad:
            print(f"      {k:20} wrong on {','.join(sc['wrong'])} "
                  f"(max ${sc['max_err_cents']/100:,.2f})")

"""Analyse an `ek-bridge --log` CSV: tracking error vs the live Unreal target and best-fit residual lag.

  python -m tools.analyze_bridge_log logs/run.csv [--lens lenses/test-lens.json]
"""
import argparse
import bisect
import csv
import statistics

from eklink.lens import LensProfile

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("csv")
ap.add_argument("--lens", default="lenses/test-lens.json", help="lens profile used for the distance-error column")
a = ap.parse_args()
rows=[r for r in csv.DictReader(open(a.csv, encoding="utf-8"))]
arm=next(float(r["t"]) for r in rows if r["state"]=="ARMED")
rows=[r for r in rows if r["state"]=="ARMED" and r["ue_motor"] and r["motor"]]
t=[float(r["t"]) for r in rows]; want=[float(r["ue_motor"]) for r in rows]
ms=[]; prev=None
for r in rows:
    if r["motor"]!=prev:
        prev=r["motor"]; ms.append((float(r["t"])-float(r["motor_age_ms"] or 0)/1000, float(r["motor"])))
def want_at(x):
    i=min(max(bisect.bisect_left(t,x),1),len(t)-1)
    return want[i-1]+(want[i]-want[i-1])*((x-t[i-1])/max(t[i]-t[i-1],1e-9))
ss=[m for m in ms if m[0]>arm+3]
live=[abs(m-want_at(x)) for x,m in ss]
best=min((statistics.mean([abs(m-want_at(x-l/1000)) for x,m in ss]),l) for l in range(-300,600,10))
p=LensProfile.load(a.lens)
cm_err=[abs(p.encoder_to_distance(m)-p.encoder_to_distance(want_at(x)))/p.encoder_to_distance(want_at(x))*100 for x,m in ss]
print(f"samples {len(ss)}  err mean {statistics.mean(live):.2f}%  p95 {sorted(live)[int(.95*(len(live)-1))]:.2f}%  max {max(live):.2f}%  residual lag {best[1]} ms  dist err mean {statistics.mean(cm_err):.1f}% p95 {sorted(cm_err)[int(.95*(len(cm_err)-1))]:.1f}%")

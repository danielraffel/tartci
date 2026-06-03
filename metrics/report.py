#!/usr/bin/env python3
import json,sys,os,statistics as st
from collections import defaultdict
rows=[json.loads(l) for l in open(sys.argv[1] if len(sys.argv)>1 else os.path.join(os.path.dirname(os.path.abspath(__file__)),"metrics.jsonl")) if l.strip()]
by=defaultdict(list)
for r in rows: by[(r["os"],r.get("mode","?"))].append(r)
print(f"{'os/mode':18} {'runs':>4} {'last build_s':>12} {'median build_s':>14} {'last cache%':>11}")
for k in sorted(by):
    rs=by[k]; bs=[r['build_s'] for r in rs if 'build_s' in r]; ch=[r['ccache_hit_pct'] for r in rs if 'ccache_hit_pct' in r]
    last=rs[-1]
    med=f"{st.median(bs):.1f}" if bs else "-"
    print(f"{k[0]+'/'+k[1]:18} {len(rs):>4} {str(last.get('build_s','-')):>12} {med:>14} {str(last.get('ccache_hit_pct','-')):>11}")

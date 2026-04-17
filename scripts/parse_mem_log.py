#!/usr/bin/env python3
import re
from collections import defaultdict

LOG = 'sims/verilator/output/chipyard.harness.TestHarness.DSPV512D128ShuttleConfig/mem_access_micro.out'

# regexes
r_time = re.compile(r'^time=\s*(\d+)\s+\[(\w+)->?(\w+)?\]\s*(.*)')
r_lrob_reserve = re.compile(r'tag=\s*(\d+)\s+lsiq=(\d+)')
r_lsa_req = re.compile(r'lsiq=(\d+)\s+debug=\s*(\d+)\s+.*addr=0x([0-9a-f]+)\s+tag=\s*(\d+)')
r_lss_resp = re.compile(r'debug=\s*(\d+)\s+eidx=\s*(\d+)')
r_lcu_push = re.compile(r'head=\s*(\d+)\s+tail=\s*(\d+)\s+push_count=\s*(\d+)')
r_lcu_pop = re.compile(r'head=\s*(\d+)\s+tail=\s*(\d+)\s+pop_count=\s*(\d+)')
r_lrob_deq = re.compile(r'tag=\s*(\d+)\s+lsiq=(\d+)')

# We'll map by "debug id" (the op debug field). Some prints include it; others use tag.
entries = defaultdict(lambda: {})

with open(LOG) as f:
    for line in f:
        line = line.rstrip('\n')
        m = r_time.match(line)
        if not m:
            continue
        t = int(m.group(1))
        module = m.group(2)
        sub = m.group(3) or ''
        rest = m.group(4)
        # AddrGen OUT/REQ include lsiq and debug
        if module == 'LSA' and sub == 'REQ':
            m2 = r_lsa_req.search(rest)
            if m2:
                lsiq = int(m2.group(1))
                dbg = int(m2.group(2))
                tag = int(m2.group(4))
                entries[dbg]['lsa_req'] = t
        elif module == 'LROB' and sub == 'RESERVE':
            m2 = r_lrob_reserve.search(rest)
            if m2:
                tag = int(m2.group(1))
                lsiq = int(m2.group(2))
                # tag may not equal debug id; but in this benchmark tag seems to map to tag=debug%?
                entries[tag].setdefault('lrob_reserve', t)
        elif module == 'LMU' and sub == 'PUSH':
            m2 = r_lcu_push.search(rest)
            if m2:
                # find nearest debug id by previous reserve or later resp; record push time by head/tail
                entries.setdefault('push_times', []).append(t)
                # can't map to debug id easily here
        elif module == 'LMU' and sub == 'POP':
            m2 = r_lcu_pop.search(rest)
            if m2:
                entries.setdefault('pop_times', []).append(t)
        elif module == 'LSS' and sub == 'COMP':
            m2 = re.search(r'op_dbg=\s*(\d+)', rest)
            if m2:
                dbg = int(m2.group(1))
                entries[dbg]['lss_comp'] = t
        elif module == 'LSS' and sub == 'RESP':
            m2 = r_lss_resp.search(rest)
            if m2:
                dbg = int(m2.group(1))
                entries[dbg]['lss_resp'] = t
        elif module == 'LROB' and sub == 'DEQ':
            m2 = r_lrob_deq.search(rest)
            if m2:
                tag = int(m2.group(1))
                entries[tag]['lrob_deq'] = t

# Build per-debug records where both lsa_req and lss_resp exist
records = []
for k in sorted([x for x in entries.keys() if not isinstance(x, str)]):
    v = entries[k]
    if 'lsa_req' in v and 'lss_resp' in v:
        records.append((k, v))

# compute stats
import statistics
end_to_end = []
reserve_lat = []
lcu_lat = []
comp_lat = []
for dbg,v in records:
    e2e = v['lss_resp'] - v['lsa_req']
    end_to_end.append(e2e)
    if 'lrob_reserve' in v:
        reserve_lat.append(v['lrob_reserve'] - v['lsa_req'])
    if 'lrob_deq' in v:
        comp_lat.append(v['lss_resp'] - v['lrob_deq'])

print('Count records:', len(records))
print('End-to-end (lsa_req -> lss_resp): avg=', int(statistics.mean(end_to_end)), 'min=', min(end_to_end), 'max=', max(end_to_end))
if reserve_lat:
    print('Reserve latency (lrob_reserve - lsa_req): avg=', int(statistics.mean(reserve_lat)))
if comp_lat:
    print('Commit latency (lss_resp - lrob_deq): avg=', int(statistics.mean(comp_lat)))

# output per-debug
print('\nPer-entry samples (debug, lsa_req, lrob_reserve, lss_comp, lss_resp, lrob_deq, e2e):')
for dbg,v in records:
    print(dbg, v.get('lsa_req'), v.get('lrob_reserve'), v.get('lss_comp'), v.get('lss_resp'), v.get('lrob_deq'), v.get('lss_resp')-v.get('lsa_req'))

#!/usr/bin/env python3
import re, csv, glob, os, sys

# Find files
dump_paths = glob.glob('sims/verilator/output/**/mem_access_micro.dump', recursive=True)
out_paths = glob.glob('sims/verilator/output/**/mem_access_micro.out', recursive=True)
csv_path = 'result/mem_access_micro_debug_latencies.csv'
out_csv = 'result/mem_access_micro_debug_latencies_with_instr.csv'
report = 'result/mem_access_micro_report.md'

if not dump_paths:
    print('mem_access_micro.dump not found', file=sys.stderr); sys.exit(1)
if not out_paths:
    print('mem_access_micro.out not found', file=sys.stderr); sys.exit(1)
if not os.path.exists(csv_path):
    print(f'{csv_path} not found', file=sys.stderr); sys.exit(1)

dump_path = dump_paths[0]
out_path = out_paths[0]
print('Using dump:', dump_path)
print('Using out :', out_path)

# Extract vector instruction lines from dump (disassembly lines with addresses)
instr_regex = re.compile(r'^\s*[0-9a-fA-F]+:\s+[0-9a-fA-F]+\s+(.*\b(vle32\.v|vse32\.v|vsse32\.v|vsuxei32\.v|vloxei32\.v|vsuxei32|vloxei32|vsuxei32\.v)\b.*)$')
instr_list = []
with open(dump_path, 'r', errors='ignore') as f:
    for line in f:
        m = instr_regex.match(line)
        if m:
            instr_text = m.group(1).strip()
            instr_list.append(instr_text)

print('Found', len(instr_list), 'vector instructions in dump')

# Extract DBG sequence from ISSUE prints in out
issue_dbg_seq = []
seen_dbg = set()
issue_re = re.compile(r"\[ISSUE\]\[(?:LD|ST)\].*?dbg=\s*(\d+)")
with open(out_path, 'r', errors='ignore') as f:
    for line in f:
        m = issue_re.search(line)
        if m:
            dbg = int(m.group(1))
            if dbg not in seen_dbg:
                seen_dbg.add(dbg)
                issue_dbg_seq.append(dbg)

print('Found', len(issue_dbg_seq), 'unique dbg ids in ISSUE sequence')

# Map every observed dbg to the static instruction sequence by repeating the static
# instruction list in order (best-effort mapping for per-element dbg ids).
mapping = {}
if instr_list:
    for i, dbg in enumerate(issue_dbg_seq):
        mapping[dbg] = instr_list[i % len(instr_list)]
else:
    mapping = {}

if not instr_list:
    print('Warning: no static vector instructions found in dump')
elif len(issue_dbg_seq) != len(instr_list):
    print('Note: dbg_count=', len(issue_dbg_seq), 'instr_sites=', len(instr_list), '- mapping will repeat static sites')

# Read existing CSV and write augmented CSV
with open(csv_path, 'r', newline='') as inf, open(out_csv, 'w', newline='') as outf:
    reader = csv.DictReader(inf)
    fieldnames = reader.fieldnames + ['instr'] if 'instr' not in reader.fieldnames else reader.fieldnames
    writer = csv.DictWriter(outf, fieldnames=fieldnames)
    writer.writeheader()
    rows = list(reader)
    for r in rows:
        try:
            dbg = int(r.get('debug') or r.get('dbg') or r.get('op_dbg'))
        except Exception:
            dbg = None
        instr = mapping.get(dbg, '')
        r['instr'] = instr
        writer.writerow(r)

print('Wrote augmented CSV to', out_csv)

# Compute min/max delta with instr and append short note to report
try:
    deltas = [(int(r['delta']), r) for r in rows if r.get('delta')]
    if deltas:
        min_row = min(deltas, key=lambda x: x[0])[1]
        max_row = max(deltas, key=lambda x: x[0])[1]
        note = '\n\n**Auto-added instr mapping**\n- Min latency row: debug={dbg} delta={delta} addr={addr} instr={instr}\n- Max latency row: debug={dbg2} delta={delta2} addr={addr2} instr={instr2}\n'.format(
            dbg=min_row.get('debug',''), delta=min_row.get('delta',''), addr=min_row.get('addr',''), instr=min_row.get('instr',''),
            dbg2=max_row.get('debug',''), delta2=max_row.get('delta',''), addr2=max_row.get('addr',''), instr2=max_row.get('instr',''))
        with open(report, 'a') as rf:
            rf.write(note)
        print('Appended summary to', report)
    else:
        print('No delta rows found to summarize')
except Exception as e:
    print('Error computing min/max:', e)

print('Done')

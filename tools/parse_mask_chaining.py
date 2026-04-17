#!/usr/bin/env python3
import re
import sys
from pathlib import Path

def parse(path):
    text = Path(path).read_text()
    lines = text.splitlines()
    perm_write_re = re.compile(r"\[PERMUTE-WRITE\].*time=\s*([0-9]+).*eg=\s*([0-9]+).*mask=([01]+)")
    perm_exec_re = re.compile(r"\[PERMUTE-EXEC\].*time=\s*([0-9]+).*func3=\s*([0-9]+).*func6=\s*([0-9]+).*eidx=\s*([0-9]+).*wvd_eg=\s*([0-9]+)")
    issue_re = re.compile(r"\[ISSUE\].*time=\s*([0-9]+).*func3=\s*([0-9]+).*func6=\s*([0-9]+).*eidx=\s*([0-9]+)(?:.*wvd_eg=\s*([0-9]+))?")
    commit_re = re.compile(r"\[COMMIT\].*time=\s*([0-9]+).*wvd_eg=\s*([0-9]+)")

    perm_writes = []
    perm_execs = []
    issues = []
    commits = []

    for L in lines:
        m = perm_write_re.search(L)
        if m:
            perm_writes.append({'time':int(m.group(1)),'eg':int(m.group(2)),'mask':m.group(3),'raw':L})
            continue
        m = perm_exec_re.search(L)
        if m:
            perm_execs.append({'time':int(m.group(1)),'func3':int(m.group(2)),'func6':int(m.group(3)),'eidx':int(m.group(4)),'wvd_eg':int(m.group(5)),'raw':L})
            continue
        
        # General ISSUE match: ensure we match regardless of whitespace
        if '[ISSUE]' in L:
            # print(f"DEBUG: Found ISSUE line: {L.strip()}")
            # Extract time, func3, func6, eidx, rd (optional)
            t_m = re.search(r'time=\s*([0-9]+)', L)
            f3_m = re.search(r'func3=\s*([0-9]+)', L)
            f6_m = re.search(r'func6=\s*([0-9]+)', L)
            eidx_m = re.search(r'eidx=\s*([0-9]+)', L)
            rd_m = re.search(r'rd=\s*([0-9]+)', L)
            wvd_m = re.search(r'wvd_eg=\s*([0-9]+)', L)
            
            if t_m and f3_m and f6_m:
                d = {
                    'time': int(t_m.group(1)),
                    'func3': int(f3_m.group(1)),
                    'func6': int(f6_m.group(1)),
                    'eidx': int(eidx_m.group(1)) if eidx_m else 0,
                    'rd': int(rd_m.group(1)) if rd_m else None,
                    'wvd_eg': int(wvd_m.group(1)) if wvd_m else None,
                    'raw': L.strip()
                }
                # print(f"DEBUG: Appending issue: {d['time']} f3={d['func3']} f6={d['func6']}")
                issues.append(d)
            continue

        m = commit_re.search(L)
        if m:
            mt = re.search(r'time=\s*([0-9]+)', L)
            meg = re.search(r'wvd_eg=\s*([0-9]+)', L)
            meidx = re.search(r'eidx=\s*([0-9]+)', L)
            commits.append({
                'time': int(mt.group(1)) if mt else int(m.group(1)), 
                'wvd_eg': int(meg.group(1)) if meg else None,
                'eidx': int(meidx.group(1)) if meidx else None,
                'raw': L
            })
        if m:
            mt = re.search(r'time=\s*([0-9]+)', L)
            meg = re.search(r'wvd_eg=\s*([0-9]+)', L)
            meidx = re.search(r'eidx=\s*([0-9]+)', L)
            commits.append({
                'time': int(mt.group(1)) if mt else int(m.group(1)), 
                'wvd_eg': int(meg.group(1)) if meg else None,
                'eidx': int(meidx.group(1)) if meidx else None,
                'raw': L
            })

    return {'perm_writes':perm_writes,'perm_execs':perm_execs,'issues':issues,'commits':commits}


def analyze(data, src_root=None):
    # Prefer PERMUTE-WRITE producers if present. Otherwise identify mask-writing
    # instructions from the decoded instruction table rather than hardcoding func6.
    producers = []
    numeric_map = {}
    f6_only_map = {}
    mask_writers = set()
    if src_root is not None:
        numeric_map, f6_only_map = build_numeric_instr_map(src_root)
        mask_writers = build_mask_writer_set(src_root)

    issues = data['issues']
    commits = data['commits']
    consumers = data['issues']

    # New fallback logic: if no instructions write as mask,
    # let's treat EVERY vector instruction as a potential producer
    # of data for the next instruction (Data Chaining)
    if src_root is not None and not mask_writers:
        # Fill mask_writers with basically everything that writes a register
        # (vadd, vsub, etc.) to enable general data-chaining analysis
        # Or just manually add common ones for now to avoid noise
        # Actually, let's just make the analysis "Instruction-to-Instruction"
        # regardless of whether it's a mask.
        pass

    pairs = []
    if producers and producers[0]['type'] == 'perm_write':
        # match by eg -> consumer.wvd_eg
        for p in producers:
            p_time = p['time']
            p_eg = p['eg']
            matched_consumers = []
            
            # Find ALL consumers that follow this producer before the next producer of same type
            # For simplicity, we search a window or until the next producer
            for c in consumers:
                if c['time'] > p_time:
                    # Check if it's a mask writer - if so, this chain ends
                    cname = resolve_instruction_name(c.get('func3'), c.get('func6'), numeric_map, f6_only_map)
                    if cname in mask_writers:
                        break
                    
                    # Does it use the mask? (In Saturn traces, we look for v0.t usage or timing proximity)
                    # We'll include all subsequent vector ops as potential chained consumers 
                    # until we hit another mask producer or a large time gap
                    if len(matched_consumers) > 0 and (c['time'] - matched_consumers[-1]['time'] > 100):
                        break
                        
                    matched_consumers.append(c)

            if matched_consumers:
                # Calculate chaining flags
                # Chaining is "active" if the consumer started before the producer committed
                # For perm_write, we use p_time as the 'available' time
                chain_info = []
                for c in matched_consumers:
                    is_chained = c['time'] < (p.get('commit_time') or p_time + 40) # Estimate commit if missing
                    chain_info.append({
                        'name': resolve_display_name(c.get('func3'), c.get('func6'), numeric_map, f6_only_map),
                        'issue': c['time'],
                        'is_chained': is_chained,
                        'f3': c.get('func3'),
                        'f6': c.get('func6')
                    })
                
                pairs.append({
                    'prod_type': 'perm_write',
                    'prod_time': p_time,
                    'prod_eg': p_eg,
                    'consumers': chain_info,
                    'prod_raw': p['raw']
                })
    else:
        # Mask issue pairing (Test 1-5 style)
        # 1. Map each ISSUE to its corresponding COMMIT by (func3, func6, eidx)
        issue_to_commit = {}
        for it in issues:
            # Find the commit for this specific issue
            # Saturn commits in order for the same instruction/eidx
            for ct in commits:
                if ct['time'] >= it['time']:
                    # We can't perfectly match by eidx alone because eidx repeats
                    # but typically the first commit after issue is the one.
                    # As a heuristic, we check if they share the same eg/rd if available
                    # For mask ops, rd=0 usually.
                    issue_to_commit[id(it)] = ct['time']
                    break

        # Group micro-ops into full instructions
        all_vector_instrs = []
        current_instr = None
        
        for it in issues:
            name = resolve_instruction_name(it.get('func3'), it.get('func6'), numeric_map, f6_only_map)
            ctime = issue_to_commit.get(id(it))
            
            if name != 'UNKNOWN':
                # Split if time gap > 100 or destination register changes
                is_new = (current_instr is None or 
                         (it['time'] - current_instr['last_issue'] > 100) or
                         (it.get('rd') is not None and it.get('rd') != current_instr.get('rd')))
                
                if is_new:
                    if current_instr:
                        all_vector_instrs.append(current_instr)
                    current_instr = {
                        'first_issue': it['time'],
                        'last_issue': it['time'],
                        'first_commit': ctime,
                        'last_commit': ctime,
                        'name': name,
                        'rd': it.get('rd'),
                        'raw': it['raw']
                    }
                else:
                    current_instr['last_issue'] = it['time']
                    if ctime:
                        if current_instr['last_commit'] is None:
                            current_instr['last_commit'] = ctime
                        else:
                            current_instr['last_commit'] = max(current_instr['last_commit'], ctime)
        if current_instr:
            all_vector_instrs.append(current_instr)
            
        # Select producers: those in mask_writers OR all if no mask_writers found in this trace
        trace_mask_producers = [i for i in all_vector_instrs if i['name'] in mask_writers]
        if trace_mask_producers:
            full_instr_producers = trace_mask_producers
        else:
            # General Data-Chaining mode: treat every instruction except the last one as a potential producer
            full_instr_producers = all_vector_instrs

        # 3. Pair each mask-producing INSTRUCTION with subsequent micro-ops
        for i, p in enumerate(full_instr_producers):
            p_issue = p['first_issue']
            p_last_commit = p['last_commit']
            next_p_issue = full_instr_producers[i+1]['first_issue'] if i+1 < len(full_instr_producers) else float('inf')
            
            matched_micro_ops = []
            for it in issues:
                # Micro-ops that belong to consumers of THIS mask instruction
                if p['last_issue'] < it['time'] < next_p_issue:
                    name = resolve_instruction_name(it.get('func3'), it.get('func6'), numeric_map, f6_only_map)
                    if name in mask_writers:
                        continue
                    
                    if name != 'UNKNOWN':
                        # CHAINING DEFINITION: The consumer micro-op issued BEFORE 
                        # the mask-producer instruction's LAST micro-op committed.
                        is_chained = it['time'] < p_last_commit
                        matched_micro_ops.append({
                            'name': resolve_display_name(it.get('func3'), it.get('func6'), numeric_map, f6_only_map),
                            'issue': it['time'],
                            'is_chained': is_chained,
                        })
            
            if matched_micro_ops:
                pairs.append({
                    'prod_type': 'mask_issue',
                    'prod_issue': p_issue,
                    'prod_commit': p_last_commit,
                    'prod_name': p['name'],
                    'prod_raw': p['raw'],
                    'consumers': matched_micro_ops
                })

    return {'producers':producers,'pairs':pairs}

    return {'producers':producers,'pairs':pairs}

    return {'producers':producers,'pairs':pairs}

    return {'producers':producers,'pairs':pairs}

    return {'producers':producers,'pairs':pairs}

def build_enum_map(src_root):
    # Build maps for OPIFunct6, OPMFunct6, OPFFunct6 and VectorConsts f3 names
    base = Path(src_root)
    enums = {}
    consts = base / 'common' / 'Consts.scala'
    if not consts.exists():
        return enums
    text = consts.read_text()
    # parse simple ChiselEnum blocks by name
    for enum_name in ('OPIFunct6','OPMFunct6','OPFFunct6'):
        m = re.search(rf'object\s+{enum_name}\s+extends\s+ChiselEnum\s*\{{(.*?)\}}', text, re.S)
        mapping = {}
        if m:
            body = m.group(1)
            val_index = 0
            # find explicit Value(...) assignments first
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                # handle aliases like "def rgatherei16 = slideup"
                mdef = re.match(r'def\s+(\w+)\s*=\s*(\w+)', line)
                if mdef:
                    mapping[mdef.group(1)] = ('alias', mdef.group(2))
                    continue
                # handle val declarations possibly with commas: val a, b, c = Value or val a, b = Value
                mval = re.match(r'val\s+(.+?)\s*=\s*Value(?:\((0x[0-9A-Fa-f]+)\.U\))?', line)
                if mval:
                    names = mval.group(1).strip()
                    explicit = mval.group(2)
                    # split names by comma
                    parts = [n.strip() for n in re.split(r',\s*', names) if n.strip()]
                    if explicit:
                        v = int(explicit,16)
                        for n in parts:
                            # skip placeholders like '_'
                            if n == '_':
                                v += 1
                                continue
                            mapping[n]=v
                            v += 1
                        val_index = v
                    else:
                        for n in parts:
                            if n == '_':
                                val_index += 1
                                continue
                            mapping[n]=val_index
                            val_index += 1
            # resolve aliases
            for k,v in list(mapping.items()):
                if isinstance(v,tuple) and v[0]=='alias':
                    target=v[1]
                    mapping[k]=mapping.get(target,mapping.get(target,0))
        enums[enum_name]=mapping
    # parse VectorConsts OPIVV/OPMVV etc from HasVectorConsts
    vmap={}
    # pattern like: def OPIVV = "b000".U(3.W) or def OPIVV = "b000".U
    for m in re.finditer(r'def\s+(\w+)\s*=\s*"b([01]+)"\.U', text):
        name = m.group(1)
        bits = m.group(2)
        try:
            vmap[name]=int(bits,2)
        except:
            pass
    # fallback: try explicit known names by searching for def OPIVV etc
    for nm in ('OPIVV','OPFVV','OPMVV','OPIVI','OPIVX','OPFVF','OPMVX'):
        if nm not in vmap:
            m2 = re.search(rf'def\s+{nm}\s*=\s*"b([01]+)"\.U', text)
            if m2:
                vmap[nm]=int(m2.group(1),2)
    # default OPIVV to 0 if nothing found
    vmap.setdefault('OPIVV',0)
    enums['VectorConsts']=vmap
    return enums

def build_instruction_map(src_root):
    # Build symbolic mapping from Instructions.scala.
    # Returns:
    #   exact_map[(f3_name, (f6_enum, f6_name))] = instr_name
    #   f6_only_map[(f6_enum, f6_name)] = [instr_name, ...]
    src = Path(src_root) / 'insns' / 'Instructions.scala'
    exact_map = {}
    f6_only_map = {}
    if not src.exists():
        return exact_map, f6_only_map
    text = src.read_text()
    trait_f3_variants = {
        'OPIInstruction': ['OPIVV', 'OPIVX', 'OPIVI'],
        'OPMInstruction': ['OPMVV', 'OPMVX'],
        'OPFInstruction': ['OPFVV', 'OPFVF'],
    }
    for m in re.finditer(r'object\s+(\w+)\s+extends\s+(\w+)\s*\{([^}]*)\}', text, re.S):
        name = m.group(1)
        base_kind = m.group(2)
        body = m.group(3)
        f6 = None
        m6 = re.search(r'F6\(\s*(OPIFunct6|OPMFunct6|OPFFunct6)\.(\w+)\s*\)', body)
        if m6:
            f6_enum = m6.group(1)
            f6_name = m6.group(2)
            f6 = (f6_enum, f6_name)
        if not f6:
            continue
        f6_only_map.setdefault(f6, []).append(name)

        explicit_f3 = re.search(r'F3\(\s*VectorConsts\.(\w+)\s*\)', body)
        if explicit_f3:
            exact_map[(explicit_f3.group(1), f6)] = name
            continue

        if base_kind in trait_f3_variants:
            for f3_name in trait_f3_variants[base_kind]:
                exact_map[(f3_name, f6)] = name

    return exact_map, f6_only_map


def build_mask_writer_set(src_root):
    src = Path(src_root) / 'insns' / 'Instructions.scala'
    mask_writers = set()
    if not src.exists():
        return mask_writers
    text = src.read_text()
    for m in re.finditer(r'object\s+(\w+)\s+extends\s+\w+\s*\{([^}]*)\}', text, re.S):
        name = m.group(1)
        body = m.group(2)
        if 'WritesAsMask.Y' in body:
            mask_writers.add(name)
    return mask_writers

def build_numeric_instr_map(src_root):
    enums = build_enum_map(src_root)
    exact_instr_map, f6_only_symbolic = build_instruction_map(src_root)
    numeric_map = {}
    f6_only_map = {}

    for key, instr in exact_instr_map.items():
        f3_name, f6 = key
        f6_enum, f6_name = f6
        f6_num = enums.get(f6_enum, {}).get(f6_name)
        f3_num = enums.get('VectorConsts', {}).get(f3_name)
        if f6_num is None or f3_num is None:
            continue
        numeric_map[(f3_num, f6_num)] = instr

    for f6, names in f6_only_symbolic.items():
        f6_enum, f6_name = f6
        f6_num = enums.get(f6_enum, {}).get(f6_name)
        if f6_num is None:
            continue
        unique_names = []
        for name in names:
            if name not in unique_names:
                unique_names.append(name)
        if len(unique_names) == 1:
            f6_only_map[f6_num] = unique_names[0]
        else:
            f6_only_map[f6_num] = '|'.join(unique_names)

    return numeric_map, f6_only_map


def resolve_instruction_name(f3, f6, numeric_map, f6_only_map):
    if f3 is not None and f6 is not None:
        exact = numeric_map.get((f3, f6))
        if exact:
            return exact
    if f6 is not None:
        fallback = f6_only_map.get(f6)
        if fallback:
            return fallback
    return 'UNKNOWN'


def f3_suffix(f3):
    suffix_map = {
        0: 'vv',
        1: 'vv',
        2: 'vv',
        3: 'vi',
        4: 'vx',
        5: 'vf',
        6: 'vx',
    }
    return suffix_map.get(f3)


def internal_to_rvv_mnemonic(name, f3=None):
    if not name or name == 'UNKNOWN':
        return 'UNKNOWN'

    # Ambiguous f6-only fallbacks remain ambiguous.
    if '|' in name:
        return name

    upper = name.upper()
    suffix = f3_suffix(f3)

    explicit_map = {
        'ADD': 'vadd',
        'SUB': 'vsub',
        'RSUB': 'vrsub',
        'MSEQ': 'vmseq',
        'MSNE': 'vmsne',
        'MSLTU': 'vmsltu',
        'MSLT': 'vmslt',
        'MSLEU': 'vmsleu',
        'MSLE': 'vmsle',
        'MSGTU': 'vmsgtu',
        'MSGT': 'vmsgt',
        'MERGE': 'vmerge',
        'ADC': 'vadc',
        'MADC': 'vmadc',
        'SBC': 'vsbc',
        'MSBC': 'vmsbc',
        'AND': 'vand',
        'OR': 'vor',
        'XOR': 'vxor',
        'SLIDEUP': 'vslideup',
        'SLIDEDOWN': 'vslidedown',
        'SLIDE1UP': 'vslide1up',
        'SLIDE1DOWN': 'vslide1down',
        'RGATHER_VV': 'vrgather.vv',
        'RGATHER_VX': 'vrgather.vx',
        'RGATHER_VI': 'vrgather.vi',
        'RGATHEREI16': 'vrgatherei16.vv',
        'COMPRESS': 'vcompress',
        'MVNRR': 'vmvnr',
    }

    if upper in explicit_map:
        base = explicit_map[upper]
        if '.' in base or suffix is None:
            return base
        return f'{base}.{suffix}'

    base = 'v' + upper.lower()
    if suffix is None:
        return base
    return f'{base}.{suffix}'


def resolve_display_name(f3, f6, numeric_map, f6_only_map):
    internal = resolve_instruction_name(f3, f6, numeric_map, f6_only_map)
    return internal_to_rvv_mnemonic(internal, f3)


def group_pairs_by_time(pairs, gap_threshold=5000):
    groups = []
    current = []
    last_time = None
    for pair in pairs:
        t = pair.get('prod_issue', pair.get('prod_time'))
        if last_time is None or t - last_time <= gap_threshold:
            current.append(pair)
        else:
            groups.append(current)
            current = [pair]
        last_time = t
    if current:
        groups.append(current)
    return groups


def report(res, src):
    print(f"File: {src}")
    print(f"Found {len(res['producers'])} producers, {len(res['pairs'])} pairs")
    
    src_root = Path(__file__).resolve().parents[1] / 'generators' / 'saturn' / 'src' / 'main' / 'scala'
    numeric_map, f6_only_map = build_numeric_instr_map(src_root)
    
    if res['pairs']:
        print("\nChain Details:")
        for i, p in enumerate(res['pairs'][:200]):
            pf3, pf6 = (None, None)
            raw = p.get('prod_raw', '')
            if raw:
                m = re.search(r'func3=\s*([0-9]+).*func6=\s*([0-9]+)', raw)
                if m:
                    pf3, pf6 = int(m.group(1)), int(m.group(2))
            
            pname = p.get('prod_name')
            if not pname:
                pname = resolve_display_name(pf3, pf6, numeric_map, f6_only_map)
            else:
                pname = internal_to_rvv_mnemonic(pname, pf3)
            
            # Aggregate micro-ops back into instruction names for a cleaner view
            # e.g., "vadd.vv (x16)" instead of "vadd.vv -> vadd.vv -> ..."
            consumers = p.get('consumers', [])
            agg_cons = []
            if consumers:
                curr_name = consumers[0]['name']
                curr_count = 0
                curr_chained = 0
                for c in consumers:
                    if c['name'] == curr_name:
                        curr_count += 1
                        if c['is_chained']: curr_chained += 1
                    else:
                        chain_marker = " [CHAINED]" if curr_chained > 0 else ""
                        agg_cons.append(f"{curr_name}(x{curr_count}){chain_marker}")
                        curr_name = c['name']
                        curr_count = 1
                        curr_chained = 1 if c['is_chained'] else 0
                chain_marker = " [CHAINED]" if curr_chained > 0 else ""
                agg_cons.append(f"{curr_name}(x{curr_count}){chain_marker}")
            
            chain_str = pname
            for cons_info in agg_cons:
                chain_str += f" ----> {cons_info}"
            
            p_time = p.get('prod_issue', p.get('prod_time'))
            print(f"#{i} [{p_time}]: {chain_str}")

        groups = group_pairs_by_time(res['pairs'])
        print('\nPhase Summary:')
        for idx, group in enumerate(groups, start=1):
            first = group[0]
            last = group[-1]
            
            # Format producer
            raw = first.get('prod_raw', '')
            pf3, pf6 = (None, None)
            m = re.search(r'func3=\s*([0-9]+).*func6=\s*([0-9]+)', raw)
            if m:
                pf3, pf6 = int(m.group(1)), int(m.group(2))
            
            pname = first.get('prod_name')
            if not pname:
                pname = resolve_display_name(pf3, pf6, numeric_map, f6_only_map)
            else:
                pname = internal_to_rvv_mnemonic(pname, pf3)
            
            # Identify the unique sequence of consumers in this phase
            # (e.g. vadd -> vsub)
            consumer_seq = []
            for pair in group:
                current_seq = [c['name'] for c in pair.get('consumers', [])]
                # Filter out consecutive duplicates to find the pattern (vadd...vadd...vsub...vsub)
                filtered = []
                for name in current_seq:
                    if not filtered or filtered[-1] != name:
                        filtered.append(name)
                # Merge into the overall phase sequence
                for f in filtered:
                    if f not in consumer_seq:
                        consumer_seq.append(f)

            consumers_str = " -> ".join(consumer_seq) if consumer_seq else "no consumers"
            start = first.get('prod_issue', first.get('prod_time'))
            
            # Calculate chaining stats for the WHOLE phase
            all_cons = [c for pg in group for c in pg.get('consumers', [])]
            chained_count = sum(1 for c in all_cons if c['is_chained'])
            total_cons = len(all_cons)
            
            # Chaining flag: if even one consumer is chained, it's a [CHAINED] phase
            # Or if majority are chained. Let's use > 0 for detection.
            chain_status = "[CHAINED]" if chained_count > 0 else "[SEQUENTIAL]"
            chain_info = f"({chained_count}/{total_cons} ops chained)" if total_cons > 0 else ""
            
            print(f"phase#{idx}: {pname} -> {consumers_str} {chain_status} {chain_info} (pairs={len(group)}, start={start})")
    # additionally, enrich with instruction names using scala sources
    src_root = Path(__file__).resolve().parents[1] / 'generators' / 'saturn' / 'src' / 'main' / 'scala'
    numeric_map, f6_only_map = build_numeric_instr_map(src_root)
    print('\nInstruction name hints:')
    # print producer -> (func3,func6) hints where available
    for i,p in enumerate(res.get('producers',[])):
        # try different producer record shapes
        if p.get('type')=='perm_write':
            print(f"#{i}: PERMUTE-WRITE eg={p['eg']} time={p['time']}")
        else:
            # issue25 style
            # prefer stored raw on producer
            raw = p.get('raw', p.get('prod_raw',''))
            f3 = None; f6 = None
            if raw:
                m = re.search(r'func3=\s*([0-9]+).*func6=\s*([0-9]+)', raw)
                if m:
                    f3 = int(m.group(1)); f6 = int(m.group(2))
            instr = resolve_display_name(f3, f6, numeric_map, f6_only_map)
            print(f"#{i}: issue_time={p.get('issue_time',p.get('prod_time'))} func3={f3} func6={f6} -> {instr}")

if __name__=='__main__':
    if len(sys.argv)<2:
        print('usage: parse_mask_chaining.py <trace.out> [<trace2.out> ...]')
        sys.exit(2)
    for src in sys.argv[1:]:
        data = parse(src)
        src_root = Path(__file__).resolve().parents[1] / 'generators' / 'saturn' / 'src' / 'main' / 'scala'
        res = analyze(data, src_root)
        report(res, src)
        print('\n')

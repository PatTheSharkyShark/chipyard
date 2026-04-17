#!/usr/bin/env python3
"""Extract per-dbg detailed event timeline from mem_access_micro.out"""

import re
import sys
from collections import defaultdict

def extract_dbg_timeline(out_path: str, target_dbg: int):
    """Extract all events for a specific dbg instruction"""
    
    # Patterns that include dbg/debug field
    patterns = [
        (r"time=\s*(\d+).*?\[LSA->REQ\].*?(?:debug|dbg)=\s*(\d+).*?tag=\s*(\d+).*?addr=(0x[0-9a-fA-F]+)", "LSA->REQ"),
        (r"time=\s*(\d+).*?\[TLIF->A\]\s+tag=\s*(\d+)", "TLIF->A"),
        (r"time=\s*(\d+).*?\[TLIF->D\]\s+tag=\s*(\d+)", "TLIF->D"),
        (r"time=\s*(\d+).*?\[LSS->RESP\].*?(?:debug|dbg|op_dbg)=\s*(\d+)", "LSS->RESP"),
        (r"time=\s*(\d+).*?\[VLIQ->ENQ\]\s+.*?dbg=\s*(\d+)", "VLIQ->ENQ"),
        (r"time=\s*(\d+).*?\[VLIQ->LAS\]\s+.*?dbg=\s*(\d+)", "VLIQ->LAS"),
        (r"time=\s*(\d+).*?\[LROB->RESERVE\].*?tag=\s*(\d+)", "LROB->RESERVE"),
        (r"time=\s*(\d+).*?\[LROB->PUSH\].*?tag=\s*(\d+)", "LROB->PUSH"),
        (r"time=\s*(\d+).*?\[LROB->DEQ\].*?tag=\s*(\d+)", "LROB->DEQ"),
        (r"time=\s*(\d+).*?\[LMU->PUSH\].*?tag=\s*(\d+)", "LMU->PUSH"),
        (r"time=\s*(\d+).*?\[LSS->COMP\].*?(?:debug|dbg|op_dbg)=\s*(\d+)", "LSS->COMP"),
    ]
    
    events = []
    tag_to_dbg = {}
    
    with open(out_path, 'r') as f:
        for line in f:
            # Track LSA->REQ to map tag -> dbg
            m = re.search(patterns[0][0], line)
            if m:
                time, dbg, tag, addr = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
                tag_to_dbg[tag] = dbg
                
            # Extract all pattern matches
            for pattern, event_name in patterns:
                m = re.search(pattern, line)
                if m:
                    groups = m.groups()
                    time = int(groups[0])
                    
                    # For LSA->REQ: (time, dbg, tag, addr)
                    if event_name == "LSA->REQ":
                        dbg = int(groups[1])
                        if dbg == target_dbg:
                            events.append((time, event_name, {"tag": int(groups[2]), "addr": groups[3]}))
                    # For events with dbg in different position
                    elif event_name in ["LSS->RESP", "VLIQ->ENQ", "VLIQ->LAS", "LSS->COMP"]:
                        dbg = int(groups[1])
                        if dbg == target_dbg:
                            events.append((time, event_name, {}))
                    # For tag-only events, use tag_to_dbg mapping
                    elif len(groups) >= 2:
                        tag = int(groups[1])
                        if tag in tag_to_dbg and tag_to_dbg[tag] == target_dbg:
                            events.append((time, event_name, {"tag": tag}))
    
    # Sort by time
    events.sort(key=lambda x: x[0])
    
    return events

if __name__ == "__main__":
    if len(sys.argv) < 2:
        target = 158  # Default to dbg 158
        out_path = "sims/verilator/output/chipyard.harness.TestHarness.DSPV512D128ShuttleConfig/mem_access_micro.out"
    else:
        target = int(sys.argv[1])
        out_path = sys.argv[2] if len(sys.argv) > 2 else "sims/verilator/output/chipyard.harness.TestHarness.DSPV512D128ShuttleConfig/mem_access_micro.out"
    
    print(f"Extracting timeline for dbg={target} from {out_path}\n")
    events = extract_dbg_timeline(out_path, target)
    
    print(f"{'Time':<8} {'Event':<20} {'Details':<40}")
    print("-" * 70)
    for time, event, details in events:
        detail_str = str(details) if details else ""
        print(f"{time:<8} {event:<20} {detail_str:<40}")

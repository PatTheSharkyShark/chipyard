#!/usr/bin/env python3
"""Minimal parser for mem_access_micro.out — concise, split load/store outputs.

This script extracts ISSUE/REQ/TLIF A/TLIF D/RESP events, computes per-dbg
core timing metrics and writes simplified CSV/MD for loads and stores.

═══════════════════════════════════════════════════════════════════════════════
PIPELINE EVENT VISIBILITY GUIDE
═══════════════════════════════════════════════════════════════════════════════

LROB (Load Reorder Buffer) Events - WHEN ARE THEY RECORDED?
  When present in CSV (not empty):
    ✓ Instruction is a LOAD (vl*, vlse, vluxei, vluxeix, etc.)
    ✓ Load entered the reorder buffer stage for response sequencing
    ✓ Both PUSH (entry) and DEQ (dequeue) events were observed
  
  When absent (empty cell):
    ✗ Instruction is a STORE (not a load)
    ✗ Load completed speculatively or bypassed reordering
    ✗ Load/response sequence incomplete in trace

SSB (Store Segment Buffer) Events - WHEN ARE THEY RECORDED?
  When present in CSV (not empty):
    ✓ Instruction is a STORE (vs*, vsse, vsuxei, vsuxeix, etc.)
    ✓ Store request routed through SSB (Store Segment Buffer)
    ✓ Both IN (entry) and OUT (exit) events were observed
  
  When absent (empty cell):
    ✗ Instruction is a LOAD (not a store)
    ✗ Store bypassed buffer (direct memory write)
    ✗ Store/buffer sequence incomplete in trace

TEMPORAL ORDERING ENFORCEMENT:
  - DEQ > PUSH: Load response dequeued AFTER request pushed (causality check)
  - OUT > IN: Store segment exits AFTER entering SSB (causality check)
  - Parser filters orphaned events from tag reuse across sequential instructions
  - Tags (0-11) are recycled; same tag number reused by multiple instructions over time

OUTPUT FILES:
  - mem_access_micro_loads.csv: All load instructions with timing metrics
  - mem_access_micro_stores.csv: All store instructions with timing metrics
  - mem_access_micro_per_dbg_loads.csv: Per-instruction detailed load timings
  - mem_access_micro_per_dbg_stores.csv: Per-instruction detailed store timings
  - mem_access_micro_detailed_timelines.md: Human-readable pipeline stage timings
"""
from __future__ import annotations

import glob
import os
import re
import csv
import argparse
from dataclasses import dataclass, field
from collections import defaultdict, deque
from typing import Dict, List, Optional, Deque, Tuple

# Regexes (minimal) - updated to accept new mnemonic-style prints and multiple dbg/debug/op_dbg forms
REQ_RE = re.compile(r"time=\s*(\d+)\s+\[LSA->REQ\].*?(?:debug|dbg)=\s*(\d+).*?addr=0x([0-9a-fA-F]+)\s+tag=\s*(\d+)")
A_RE = re.compile(r"time=\s*(\d+)\s+\[TLIF->A\]\s+tag=\s*(\d+)")
D_RE = re.compile(r"time=\s*(\d+)\s+\[TLIF->D\]\s+tag=\s*(\d+)")
RESP_RE = re.compile(r"time=\s*(\d+)\s+\[LSS->RESP\].*?(?:debug|dbg|op_dbg)=\s*(\d+)")
STORE_RESP_RE = re.compile(r"time=\s*(\d+)\s+\[SSS->RESP\].*?(?:debug|dbg|op_dbg)=\s*(\d+)")
# ISSUE/COMMIT now print human mnemonics with more detail: insn=vle 32.v sew=2 mop=0 ...
# Enhanced regex to capture insn (e.g., "vle 32.v"), sew, mop for detailed mnemonic
ISSUE_RE = re.compile(r"time=\s*(\d+).*?\[ISSUE\]\[(LD|ST)\].*?insn=([a-z]+\s+[\d.]+\.v)\s+sew=(\d+)\s+mop=(\d+).*?(?:dbg|debug|op_dbg)=\s*(\d+)")
COMMIT_RE = re.compile(r"time=\s*(\d+).*?\[COMMIT\]\[(LD|ST)\].*?insn=([a-z]+\s+[\d.]+\.v)\s+sew=(\d+).*?(?:dbg|debug|op_dbg)=\s*(\d+)")
VLIQ_ENQ_RE = re.compile(r"time=\s*(\d+)\s+\[VLIQ->ENQ\]\s+lsiq=\s*(\d+)\s+dbg=\s*(\d+)\s+vstart=\s*(\d+)\s+base_off=0x([0-9a-fA-F]+)")
VLIQ_LAS_RE = re.compile(r"time=\s*(\d+)\s+\[VLIQ->LAS\]\s+lsiq=\s*(\d+)\s+dbg=\s*(\d+)")
VSIQ_ENQ_RE = re.compile(r"time=\s*(\d+)\s+\[VSIQ->ENQ\]\s+siq=\s*(\d+)\s+dbg=\s*(\d+)\s+base_off=0x([0-9a-fA-F]+)\s+vl=\s*(\d+)")
VSIQ_SSS_RE = re.compile(r"time=\s*(\d+)\s+\[VSIQ->SSS\]\s+siq=\s*(\d+)\s+dbg=\s*(\d+)")
VSIQ_SAS_RE = re.compile(r"time=\s*(\d+)\s+\[VSIQ->SAS\]\s+siq=\s*(\d+)\s+dbg=\s*(\d+)")
VSIQ_DEQ_RE = re.compile(r"time=\s*(\d+)\s+\[VSIQ->DEQ\]\s+siq=\s*(\d+)\s+dbg=\s*(\d+)")
SSB_IN_RE = re.compile(r"time=\s*(\d+)\s+\[SSB->IN\].*?(?:dbg|debug|op_dbg)=\s*(\d+).*?sidx=\s*(\d+)\s+segstart=\s*(\d+)\s+rows=\s*(\d+)")
SSB_OUT_RE = re.compile(r"time=\s*(\d+)\s+\[SSB->OUT\].*?(?:dbg|debug|op_dbg)=\s*(\d+).*?out_row=\s*(\d+)\s+out_sidx=\s*(\d+)")
SMU_PUSH_RE = re.compile(r"time=\s*(\d+)\s+\[SMU->PUSH\].*?tag=\s*(\d+)")
SMU_POP_RE = re.compile(r"time=\s*(\d+)\s+\[SMU->POP\].*?tag=\s*(\d+)")
LSS_COMP_RE = re.compile(r"time=\s*(\d+)\s+\[LSS->COMP\].*?(?:debug|dbg|op_dbg)=\s*(\d+)")
LROB_DEQ_RE = re.compile(r"time=\s*(\d+)\s+\[LROB->DEQ\].*?tag=\s*(\d+)")
LROB_PUSH_RE = re.compile(r"time=\s*(\d+)\s+\[LROB->PUSH\].*?tag=\s*(\d+)")
LMU_PUSH_RE = re.compile(r"time=\s*(?P<time>\d+)\s+\[LMU->PUSH\].*?tag=\s*(?P<tag>\d+)")
LMU_POP_RE = re.compile(r"time=\s*(?P<time>\d+)\s+\[LMU->POP\].*?tag=\s*(?P<tag>\d+)")

MEM_PREFIX_LOAD = "vl"
MEM_PREFIX_STORE = "vs"

@dataclass
class RequestInstance:
    req_time: int
    tag: int
    req_addr: Optional[int] = None
    a_time: Optional[int] = None
    d_time: Optional[int] = None

@dataclass
class DebugTiming:
    """
    Timing information for a single memory instruction.
    
    Organized by official Saturn pipeline stages (from RTL documentation):
    
    ═══════════════════════════════════════════════════════════════════════════
    LOAD PATH (Load instructions: vl, vlse, vluxei, vluxeix, etc.):
    ═══════════════════════════════════════════════════════════════════════════
      Stage 1: Load Address Sequencer (LAS)
        - vliq_enq: Instruction enters load instruction queue
        - vliq_las: Load address sequence generation starts
        - first_req: First address request issued (LSA->REQ)
        - first_A: First address phase on bus (TLIF->A)
        - avg_A_to_D: Average latency from address to data (cycles)
        - last_D: Last data response received (TLIF->D)
        
      Stage 2: Load Reordering Buffer (LROB)
        - lrob_push: Response entry written to LROB (tag-tracked)
        - lrob_deq: Response dequeued from LROB in order
        
      Stage 3: Load Response Merger (LMU)
        - lmu_push: Data from LIFQ enters LMU (2nd-level reorder by byte range)
        - lmu_pop: Reordered data exits LMU to LSS
        
      Stage 4: Load Segment Buffer (LSB) & Load Segment Summarizer (LSS)
        - lss_comp: LSS->COMP event; data ready for vector datapath

    ═══════════════════════════════════════════════════════════════════════════
    STORE PATH (Store instructions: vs, vsse, vsuxei, vsuxeix, etc.):
    ═══════════════════════════════════════════════════════════════════════════
      Stage 1: Store Segment Buffer (SSB)
        - vsiq_enq: Instruction enters store instruction queue
        - ssb_in: Store segment enters SSB pipeline
        - ssb_out: Store segment exits SSB pipeline
        
      Stage 2: Store Data Merger (SMU)
        - smu_push: Data enters SMU reordering buffer
        - smu_pop: Reordered data exits SMU to memory
        
      Stage 3: Store Address Sequencer (SAS)
        - vsiq_sas: Address sequence generation starts
        - vsiq_sss: Store segment sequence generation
        - first_req: First address request issued (for stores)
        - first_A: First address phase (for stores)
        
      Stage 4: Store Acknowledgement Unit (SAU)
        - (Completion/acknowledgement handling)
    
    ═══════════════════════════════════════════════════════════════════════════
    COMMON FIELDS:
    ═══════════════════════════════════════════════════════════════════════════
      - last_resp: Latest LSS/SSS->RESP time (instruction complete from pipeline)
      - completion_delta: REQ to completion latency
      - completion_source: RESP (pipeline) or D_EST (data arrival based)
    """
    requests: List[RequestInstance] = field(default_factory=list)
    resp_times: List[int] = field(default_factory=list)
    mnemonic: Optional[str] = None
    # optional internal stage timestamps (filled when seen)
    vliq_enq: Optional[int] = None
    vliq_las: Optional[int] = None
    vsiq_enq: Optional[int] = None
    vsiq_sss: Optional[int] = None
    vsiq_sas: Optional[int] = None
    vsiq_deq: Optional[int] = None
    ssb_in: Optional[int] = None
    ssb_out: Optional[int] = None
    smu_push: Optional[int] = None
    smu_pop: Optional[int] = None
    lrob_deq: Optional[int] = None
    lrob_push: Optional[int] = None
    lmu_push: Optional[int] = None
    lmu_pop: Optional[int] = None
    lss_comp: Optional[int] = None
    # Per-tag LROB events to avoid mixing unrelated tags
    tag_lrob_push: Dict[int, int] = field(default_factory=dict)  # tag -> first push time
    tag_lrob_deq: Dict[int, int] = field(default_factory=dict)   # tag -> first deq time


def classify_mem_type(addr: Optional[int]) -> str:
    if addr is None:
        return ""
    if 0x70000000 <= addr < 0x80000000:
        return "TCM"
    return "DRAM"


def autodetect_out_path() -> str:
    matches = glob.glob("sims/verilator/output/**/mem_access_micro.out", recursive=True)
    if not matches:
        raise FileNotFoundError("No mem_access_micro.out found under sims/verilator/output")
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]


def autodetect_dump_path() -> str:
    matches = glob.glob("sims/verilator/output/**/mem_access_micro.dump", recursive=True)
    if not matches:
        raise FileNotFoundError("No mem_access_micro.dump found under sims/verilator/output")
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]


def get_dbg_for_tag_at_time(tag_timeline: Dict[int, List[Tuple[int, int]]], event_time: int, tag: int) -> Optional[int]:
    """Find the dbg associated with (tag, event_time) by looking up tag_timeline.
    Returns the dbg from the most recent REQ that occurred before or at event_time."""
    timeline = tag_timeline.get(tag, [])
    if not timeline:
        return None
    # Find the entry with largest time <= event_time
    best_dbg = None
    for req_time, dbg in timeline:
        if req_time <= event_time:
            best_dbg = dbg  # Keep updating to find the latest one <= event_time
        else:
            break  # timeline should be sorted by time, so stop here
    return best_dbg


def parse_issue_mnemonic(line: str) -> Optional[Tuple[str, int]]:
    # Try ISSUE first (has sew, mop fields)
    m = ISSUE_RE.search(line)
    if m:
        # groups: 1=time, 2=LD|ST, 3=insn, 4=sew, 5=mop, 6=dbg
        insn = m.group(3)
        sew = m.group(4)
        mop = m.group(5)
        try:
            dbg = int(m.group(6))
        except Exception:
            return None
        # Build detailed mnemonic: insn.sew(mop_type)
        # Map SEW values to element widths
        sew_map = {0: "8", 1: "16", 2: "32", 3: "64", 4: "128", 5: "256", 6: "512", 7: "1024"}
        sew_str = sew_map.get(int(sew), sew)
        
        # Clean up insn: normalize spaces
        insn_clean = " ".join(insn.split())  # Remove extra spaces
        
        # MOP values: 0=unit, 1=indexed-unordered, 2=strided, 3=indexed-ordered
        mop_int = int(mop)
        if mop_int == 0:
            # Unit stride (vle)
            mnem = f"{insn_clean}.sew{sew_str}"
        elif mop_int == 1:
            # Indexed unordered (vluxei)
            mnem = f"{insn_clean}.sew{sew_str}(idx-unord)"
        elif mop_int == 2:
            # Strided (vlse)
            mnem = f"{insn_clean}.sew{sew_str}(stride)"
        elif mop_int == 3:
            # Indexed ordered (vluxei ordered)
            mnem = f"{insn_clean}.sew{sew_str}(idx-ord)"
        else:
            mnem = f"{insn_clean}.sew{sew_str}"
        
        return mnem, dbg
    
    # Try COMMIT (may not have mop field)
    m = COMMIT_RE.search(line)
    if m:
        # groups: 1=time, 2=LD|ST, 3=insn, 4=sew, 5=dbg
        insn = m.group(3)
        sew = m.group(4)
        try:
            dbg = int(m.group(5))
        except Exception:
            return None
        # Map SEW
        sew_map = {0: "8", 1: "16", 2: "32", 3: "64", 4: "128", 5: "256", 6: "512", 7: "1024"}
        sew_str = sew_map.get(int(sew), sew)
        insn_clean = " ".join(insn.split())
        mnem = f"{insn_clean}.sew{sew_str}"
        return mnem, dbg
    
    return None


def parse_out_events(out_path: str) -> Dict[int, DebugTiming]:
    # First pass: 
    # 1. Collect tag->dbg mappings from LSA->REQ to handle tag reuse
    # 2. Record which tags belong to each dbg
    tag_timeline: Dict[int, List[Tuple[int, int]]] = defaultdict(list)  # tag -> [(req_time, dbg), ...]
    dbg_tags: Dict[int, set] = defaultdict(set)  # dbg -> set of tags this dbg used
    
    with open(out_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = REQ_RE.search(line)
            if m:
                t = int(m.group(1))
                dbg = int(m.group(2))
                tag = int(m.group(4))
                tag_timeline[tag].append((t, dbg))
                dbg_tags[dbg].add(tag)
    
    # Second pass: process all events with correct tag->dbg resolution
    # Only count LROB events for tags that actually belong to that dbg
    dbg_data: Dict[int, DebugTiming] = {}
    pending_a_by_tag: Dict[int, Deque[int]] = defaultdict(deque)
    pending_reqs_by_tag: Dict[int, Deque[RequestInstance]] = defaultdict(deque)
    tag_to_dbg: Dict[int, int] = {}  # Most recent tag->dbg mapping

    with open(out_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            mnem_dbg = parse_issue_mnemonic(line)
            if mnem_dbg:
                mnem, dbg = mnem_dbg
                d = dbg_data.setdefault(dbg, DebugTiming())
                d.mnemonic = mnem
                continue

            m = A_RE.search(line)
            if m:
                t, tag = int(m.group(1)), int(m.group(2))
                pending_a_by_tag[tag].append(t)
                continue

            m = VLIQ_ENQ_RE.search(line)
            if m:
                t, lsiq, dbg, vstart, base = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5),16)
                d = dbg_data.setdefault(dbg, DebugTiming())
                if d.vliq_enq is None or t < d.vliq_enq:
                    d.vliq_enq = t
                continue

            m = VLIQ_LAS_RE.search(line)
            if m:
                t, lsiq, dbg = int(m.group(1)), int(m.group(2)), int(m.group(3))
                d = dbg_data.setdefault(dbg, DebugTiming())
                if d.vliq_las is None or t < d.vliq_las:
                    d.vliq_las = t
                continue

            m = VSIQ_ENQ_RE.search(line)
            if m:
                t, siq, dbg, base, vl = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4),16), int(m.group(5))
                d = dbg_data.setdefault(dbg, DebugTiming())
                if d.vsiq_enq is None or t < d.vsiq_enq:
                    d.vsiq_enq = t
                continue

            m = VSIQ_SSS_RE.search(line)
            if m:
                t, siq, dbg = int(m.group(1)), int(m.group(2)), int(m.group(3))
                d = dbg_data.setdefault(dbg, DebugTiming())
                if d.vsiq_sss is None or t < d.vsiq_sss:
                    d.vsiq_sss = t
                continue

            m = VSIQ_SAS_RE.search(line)
            if m:
                t, siq, dbg = int(m.group(1)), int(m.group(2)), int(m.group(3))
                d = dbg_data.setdefault(dbg, DebugTiming())
                if d.vsiq_sas is None or t < d.vsiq_sas:
                    d.vsiq_sas = t
                continue

            m = VSIQ_DEQ_RE.search(line)
            if m:
                t, siq, dbg = int(m.group(1)), int(m.group(2)), int(m.group(3))
                d = dbg_data.setdefault(dbg, DebugTiming())
                if d.vsiq_deq is None or t < d.vsiq_deq:
                    d.vsiq_deq = t
                continue

            m = SSB_IN_RE.search(line)
            if m:
                t, dbg, sidx, segstart, rows_ = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
                d = dbg_data.setdefault(dbg, DebugTiming())
                if d.ssb_in is None or t < d.ssb_in:
                    d.ssb_in = t
                continue

            m = SSB_OUT_RE.search(line)
            if m:
                t, dbg, out_row, out_sidx = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                d = dbg_data.setdefault(dbg, DebugTiming())
                if d.ssb_out is None or t < d.ssb_out:
                    d.ssb_out = t
                continue

            m = SMU_PUSH_RE.search(line)
            if m:
                t, tag = int(m.group(1)), int(m.group(2))
                # Store path SMU events: ignore (no dbg mapping available for stores)
                continue

            m = SMU_POP_RE.search(line)
            if m:
                t, tag = int(m.group(1)), int(m.group(2))
                # Store path SMU events: ignore (no dbg mapping available for stores)
                continue

            m = LSS_COMP_RE.search(line)
            if m:
                t, dbg = int(m.group(1)), int(m.group(2))
                d = dbg_data.setdefault(dbg, DebugTiming())
                if d.lss_comp is None or t < d.lss_comp:
                    d.lss_comp = t
                continue

            m = LROB_DEQ_RE.search(line)
            if m:
                t, tag = int(m.group(1)), int(m.group(2))
                # Use tag_timeline to find the correct dbg for this tag at this time
                dbg = get_dbg_for_tag_at_time(tag_timeline, t, tag)
                # Only record if this tag actually belongs to the mapped dbg
                if dbg is not None and tag in dbg_tags[dbg]:
                    timeline = tag_timeline.get(tag, [])
                    req_times = [req_t for req_t, req_dbg in timeline if req_dbg == dbg]
                    # DEQ must be after REQ and within reasonable window (500 cycles)
                    if req_times and req_times[-1] <= t <= req_times[-1] + 500:
                        d = dbg_data.setdefault(dbg, DebugTiming())
                        # Record per-tag, take first deq for this tag
                        if tag not in d.tag_lrob_deq:
                            d.tag_lrob_deq[tag] = t
                continue

            m = LROB_PUSH_RE.search(line)
            if m:
                t, tag = int(m.group(1)), int(m.group(2))
                # Use tag_timeline to find the correct dbg for this tag at this time
                dbg = get_dbg_for_tag_at_time(tag_timeline, t, tag)
                # Only record if this tag actually belongs to the mapped dbg
                if dbg is not None and tag in dbg_tags[dbg]:
                    timeline = tag_timeline.get(tag, [])
                    req_times = [req_t for req_t, req_dbg in timeline if req_dbg == dbg]
                    # PUSH must be after REQ and within reasonable window (500 cycles)
                    if req_times and req_times[-1] <= t <= req_times[-1] + 500:
                        d = dbg_data.setdefault(dbg, DebugTiming())
                        # Record per-tag, take first push for this tag
                        if tag not in d.tag_lrob_push:
                            d.tag_lrob_push[tag] = t
                continue

            m = LMU_PUSH_RE.search(line)
            if m:
                t = int(m.group('time'))
                tag = m.groupdict().get('tag')
                mapped_dbg = None
                if tag is not None:
                    # Use tag_timeline to find the correct dbg for this tag at this time
                    mapped_dbg = get_dbg_for_tag_at_time(tag_timeline, t, int(tag))
                if mapped_dbg is not None:
                    d = dbg_data.setdefault(mapped_dbg, DebugTiming())
                    if d.lmu_push is None or t < d.lmu_push:
                        d.lmu_push = t
                continue

            m = LMU_POP_RE.search(line)
            if m:
                t = int(m.group('time'))
                tag = m.groupdict().get('tag')
                mapped_dbg = None
                if tag is not None:
                    # Use tag_timeline to find the correct dbg for this tag at this time
                    mapped_dbg = get_dbg_for_tag_at_time(tag_timeline, t, int(tag))
                if mapped_dbg is not None:
                    d = dbg_data.setdefault(mapped_dbg, DebugTiming())
                    if d.lmu_pop is None or t < d.lmu_pop:
                        d.lmu_pop = t
                continue

            m = REQ_RE.search(line)
            if m:
                t = int(m.group(1))
                dbg = int(m.group(2))
                addr = int(m.group(3), 16)
                tag = int(m.group(4))
                d = dbg_data.setdefault(dbg, DebugTiming())
                req = RequestInstance(req_time=t, tag=tag, req_addr=addr, a_time=t)
                # record tag->dbg mapping to correlate later prints that only include tag
                tag_to_dbg[tag] = dbg
                q = pending_a_by_tag[tag]
                while q and q[0] < t - 2:
                    q.popleft()
                if q and abs(q[0] - t) <= 2:
                    req.a_time = q.popleft()
                d.requests.append(req)
                pending_reqs_by_tag[tag].append(req)
                continue

            m = D_RE.search(line)
            if m:
                t, tag = int(m.group(1)), int(m.group(2))
                if pending_reqs_by_tag[tag]:
                    req = pending_reqs_by_tag[tag].popleft()
                    req.d_time = t
                continue

            m = RESP_RE.search(line)
            if m:
                t, dbg = int(m.group(1)), int(m.group(2))
                d = dbg_data.setdefault(dbg, DebugTiming())
                d.resp_times.append(t)
                continue

            m = STORE_RESP_RE.search(line)
            if m:
                t, dbg = int(m.group(1)), int(m.group(2))
                d = dbg_data.setdefault(dbg, DebugTiming())
                d.resp_times.append(t)
                continue

    # Post-process: aggregate per-tag LROB events, only from tags that have PUSH
    for dbg_id, dbg in dbg_data.items():
        # Only set global lrob_push/deq from tags that have both events or at least PUSH
        if dbg.tag_lrob_push:
            # Use minimum push time across all tags that actually pushed
            dbg.lrob_push = min(dbg.tag_lrob_push.values())
            # For DEQ, only include times from tags that also have PUSH (paired events)
            paired_deqs = [t for tag, t in dbg.tag_lrob_deq.items() if tag in dbg.tag_lrob_push]
            if paired_deqs:
                dbg.lrob_deq = min(paired_deqs)
            else:
                # No paired DEQ; DEQ orphaned or still in flight
                dbg.lrob_deq = None
        else:
            # No PUSH at all means all DEQ are orphaned from tag reuse
            dbg.lrob_deq = None
            dbg.lrob_push = None

    return dbg_data


def rebuild_dbg_mapping(out_path: str, dump_path: str, src_path: str) -> Dict[int, Tuple[Optional[str], Optional[str]]]:
    # simple mapping using ISSUE order vs dump sites
    seq = []
    with open(out_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = re.search(r"\[ISSUE\].*?(?:dbg|debug|op_dbg)=\s*(\d+)", line)
            if m:
                v = int(m.group(1))
                if v not in seq:
                    seq.append(v)
    sites = []
    if os.path.exists(dump_path):
        # parse dump for mnemonics (very small): look for lines like 'addr <func>:' then instruction lines
        func = None
        with open(dump_path, "r", encoding="utf-8", errors="ignore") as f:
            for L in f:
                fm = re.match(r"^\s*[0-9a-fA-F]+\s+<([^>]+)>:", L)
                if fm:
                    func = fm.group(1)
                    continue
                im = re.match(r"^\s*[0-9a-fA-F]+:\s+[0-9a-fA-F]+\s+([^\s]+)", L)
                if im and func:
                    sites.append((func, im.group(1)))
    # pair seq and sites
    mapping: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
    n = min(len(seq), len(sites))
    for i in range(n):
        mapping[seq[i]] = sites[i]
    return mapping


def compute_metrics_for_dbg(dbg: int, dbg_data: Dict[int, DebugTiming]) -> Dict[str, object]:
    d = dbg_data.get(dbg)
    if d is None:
        return {"dbg": str(dbg), "mnemonic": "", "mem_type": "", "first_req": "", "first_A": "", "avg_A_to_D": "", "last_D": "", "last_resp": "", "completion_delta": "", "completion_source": ""}

    req_times = [r.req_time for r in d.requests]
    a_times = [r.a_time for r in d.requests if r.a_time is not None]
    d_times = [r.d_time for r in d.requests if r.d_time is not None]

    first_req = min(req_times) if req_times else None
    first_a = min(a_times) if a_times else None
    last_d = max(d_times) if d_times else None
    last_resp = max(d.resp_times) if d.resp_times else None

    ad_list = [r.d_time - r.a_time for r in d.requests if r.a_time is not None and r.d_time is not None and r.d_time >= r.a_time]
    avg_ad = round(sum(ad_list)/len(ad_list),1) if ad_list else ""

    completion_delta = ""
    completion_source = ""
    if first_req is not None and last_resp is not None:
        completion_delta = last_resp - first_req
        completion_source = "RESP"
    elif first_req is not None and last_d is not None:
        completion_delta = last_d - first_req
        completion_source = "D_EST"

    first_addr = d.requests[0].req_addr if d.requests else None
    mem_type = classify_mem_type(first_addr)
    # internal LSU stage timestamps (take earliest observed per-dbg)
    vliq_enq = None
    vliq_las = None
    vsiq_enq = None
    vsiq_sss = None
    vsiq_sas = None
    ssb_in = None
    ssb_out = None
    smu_push = None
    smu_pop = None
    lrob_deq = None
    lrob_push = None
    lmu_push = None
    lmu_pop = None
    lss_comp = None
    # try to extract from DebugTiming fields if present
    if hasattr(d, 'vliq_enq'):
        vliq_enq = d.vliq_enq
    if hasattr(d, 'vliq_las'):
        vliq_las = d.vliq_las
    if hasattr(d, 'vsiq_enq'):
        vsiq_enq = d.vsiq_enq
    if hasattr(d, 'vsiq_sss'):
        vsiq_sss = d.vsiq_sss
    if hasattr(d, 'vsiq_sas'):
        vsiq_sas = d.vsiq_sas
    if hasattr(d, 'ssb_in'):
        ssb_in = d.ssb_in
    if hasattr(d, 'ssb_out'):
        ssb_out = d.ssb_out
    if hasattr(d, 'smu_push'):
        smu_push = d.smu_push
    if hasattr(d, 'smu_pop'):
        smu_pop = d.smu_pop
    if hasattr(d, 'lrob_deq'):
        lrob_deq = d.lrob_deq
    if hasattr(d, 'lrob_push'):
        lrob_push = d.lrob_push
    if hasattr(d, 'lmu_push'):
        lmu_push = d.lmu_push
    if hasattr(d, 'lmu_pop'):
        lmu_pop = d.lmu_pop
    if hasattr(d, 'lss_comp'):
        lss_comp = d.lss_comp

    return {
        "dbg": str(dbg),
        "mnemonic": d.mnemonic or "",
        "mem_type": mem_type,
        "first_req": first_req or "",
        "first_A": first_a or "",
        "avg_A_to_D": avg_ad,
        "last_D": last_d or "",
        "last_resp": last_resp or "",
        "completion_delta": completion_delta,
        "completion_source": completion_source,
        # LSU stage timestamps
        "vliq_enq": vliq_enq or "",
        "vliq_las": vliq_las or "",
        "vsiq_enq": vsiq_enq or "",
        "vsiq_sss": vsiq_sss or "",
        "vsiq_sas": vsiq_sas or "",
        "ssb_in": ssb_in or "",
        "ssb_out": ssb_out or "",
        "smu_push": smu_push or "",
        "smu_pop": smu_pop or "",
        "lrob_deq": lrob_deq or "",
        "lrob_push": lrob_push or "",
        "lmu_push": lmu_push or "",
        "lmu_pop": lmu_pop or "",
        "lss_comp": lss_comp or "",
    }


def fmt_cell(v):
    if v is None or v == "":
        return ""
    return str(v)


def write_simple_csv_md(rows: List[Dict[str, object]], csv_path: str, md_path: str):
    # LOAD PATH stages: LAS -> LROB -> LMU -> LSB
    # STORE PATH stages: SSB -> SMU -> SAS -> SAU
    cols = [
        "seq","dbg","func","mnemonic","mem_type",
        # Common timing
        "first_req","first_A","avg_A_to_D","last_D","last_resp",
        "completion_delta","completion_source",
        # LOAD PATH - Stage 1: Load Address Sequencer (LAS)
        "vliq_enq","vliq_las",
        # LOAD PATH - Stage 2: Load Reordering Buffer (LROB)
        "lrob_push","lrob_deq",
        # LOAD PATH - Stage 3: Load Response Merger (LMU)
        "lmu_push","lmu_pop",
        # LOAD PATH - Stage 4: Load Segment Summarizer (LSS)
        "lss_comp",
        # STORE PATH - Stage 1: Store Segment Buffer (SSB)
        "vsiq_enq","vsiq_sss","ssb_in","ssb_out",
        # STORE PATH - Stage 2: Store Data Merger (SMU)
        "smu_push","smu_pop",
        # STORE PATH - Stage 3: Store Address Sequencer (SAS)
        "vsiq_sas","vsiq_deq",
    ]
    # drop columns that are entirely empty across all rows (but keep seq, dbg)
    essential = {"seq", "dbg"}
    active_cols = []
    for c in cols:
        if c in essential:
            active_cols.append(c)
            continue
        any_nonempty = any((r.get(c) is not None and str(r.get(c)).strip() != "") for r in rows)
        if any_nonempty:
            active_cols.append(c)

    # write csv
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=active_cols)
        w.writeheader()
        for r in rows:
            out = {c: r.get(c, "") for c in active_cols}
            w.writerow(out)
    # write md (drop `func` column from markdown output)
    md_cols = [c for c in active_cols if c != "func"]
    # human-readable Chinese descriptions for columns
    # Organized by official Saturn pipeline stages: LOAD (LAS->LROB->LMU->LSS) and STORE (SSB->SMU->SAS->SAU)
    COL_DESCRIPTIONS = {
        "seq": "序号（输出行顺序）",
        "dbg": "调试 ID，用于将此时间线映射回生成器的 dbg 值",
        "func": "函数名（如果可用，CSV 中保留但 MD 不显示）",
        "mnemonic": "指令助记符，例如 vle32.v，vle=加载，vs=存储",
        "mem_type": "内存类型：TCM 或 DRAM",
        "first_req": "第一次地址请求（LSA->REQ）发出的周期",
        "first_A": "第一次地址相位握手（TLIF->A）的周期",
        "avg_A_to_D": "地址到数据平均延迟：(last_D - first_A) / 元素数（周期）",
        "last_D": "最后一次数据响应（TLIF->D）的周期",
        "last_resp": "管道完成响应（LSS->RESP 或 SSS->RESP）的周期",
        "completion_delta": "从首个 REQ 到完成（RESP 或 D_EST）的总延迟（周期）",
        "completion_source": "完成时刻的来源：RESP=管道响应，D_EST=数据到达估计",
        # LOAD PATH - Stage 1: Load Address Sequencer (LAS)
        "vliq_enq": "[LAS-1] 加载指令进入向量加载指令队列（VLIQ）",
        "vliq_las": "[LAS-2] 加载地址序列生成开始至完成（最后地址生成时刻）",
        # LOAD PATH - Stage 2: Load Reordering Buffer (LROB)
        "lrob_push": "[LROB-1] 加载响应进入 LROB（Load Reordering Buffer），tag 追踪",
        "lrob_deq": "[LROB-2] 加载响应从 LROB 按顺序弹出（tag 对应）",
        # LOAD PATH - Stage 3: Load Response Merger (LMU)
        "lmu_push": "[LMU-1] 数据从 LIFQ 进入 LMU（Load Response Merger），二级按字节范围重排序",
        "lmu_pop": "[LMU-2] 重排序完成的数据从 LMU 弹出交付给 LSS",
        # LOAD PATH - Stage 4: Load Segment Summarizer (LSS)
        "lss_comp": "[LSS] LSS->COMP 事件，数据已准备进入向量 datapath，对应 LSB（Load Segment Buffer）处理完成",
        # STORE PATH - Stage 1: Store Segment Buffer (SSB)
        "vsiq_enq": "[SSB-1] 存储指令进入向量存储指令队列（VSIQ）",
        "vsiq_sss": "[SSB-2] 存储段序列生成启动（Store Segment Sequencer）",
        "ssb_in": "[SSB-3] 存储段进入 SSB（Store Segment Buffer）管道",
        "ssb_out": "[SSB-4] 存储段从 SSB 管道弹出（处理完成）",
        # STORE PATH - Stage 2: Store Data Merger (SMU)
        "smu_push": "[SMU-1] 数据进入 SMU（Store Data Merger），重排序缓冲",
        "smu_pop": "[SMU-2] 重排序数据从 SMU 弹出交付给存储单元",
        # STORE PATH - Stage 3: Store Address Sequencer (SAS)
        "vsiq_sas": "[SAS-1] 存储地址序列生成启动（Store Address Sequencer）",
        "vsiq_deq": "[SAS-2] 存储指令从 VSIQ 出队，地址序列生成完成",
    }

    with open(md_path, "w", encoding="utf-8") as f:
        # Determine if this is loads or stores based on mnemonic prefix
        is_load = all(r.get("mnemonic", "").startswith(("vl", "vlse", "vluxei")) for r in rows if r.get("mnemonic"))
        
        if is_load:
            # LOAD PATH title and stages
            f.write("# 向量加载指令时序分析\n\n")
            f.write("## 加载路径（LOAD PATH）管道阶段概览\n\n")
            f.write("Saturn 的加载路径由以下 4 个主要阶段组成：\n\n")
            f.write("| 阶段 | 名称 | 功能 | 关键事件 |\n")
            f.write("|------|------|------|----------|\n")
            f.write("| 1 | **Load Address Sequencer (LAS)** | 地址生成和请求发出 | vliq_enq, vliq_las, first_req, first_A |\n")
            f.write("| 2 | **Load Reordering Buffer (LROB)** | 处理乱序到达的响应 | lrob_push, lrob_deq |\n")
            f.write("| 3 | **Load Response Merger (LMU)** | 二级数据重排序（按字节范围） | lmu_push, lmu_pop |\n")
            f.write("| 4 | **Load Segment Buffer (LSB)** | 分段数据缓冲和处理 | lss_comp |\n\n")
            f.write("## 数据表\n\n")
        else:
            # STORE PATH title and stages
            f.write("# 向量存储指令时序分析\n\n")
            f.write("## 存储路径（STORE PATH）管道阶段概览\n\n")
            f.write("Saturn 的存储路径由以下 4 个主要阶段组成：\n\n")
            f.write("| 阶段 | 名称 | 功能 | 关键事件 |\n")
            f.write("|------|------|------|----------|\n")
            f.write("| 1 | **Store Segment Buffer (SSB)** | 分段缓冲和处理 | vsiq_enq, vsiq_sss, ssb_in, ssb_out |\n")
            f.write("| 2 | **Store Data Merger (SMU)** | 数据重排序（按字节范围） | smu_push, smu_pop |\n")
            f.write("| 3 | **Store Address Sequencer (SAS)** | 地址生成和请求发出 | vsiq_sas, vsiq_deq, first_req, first_A |\n")
            f.write("| 4 | **Store Acknowledgement Unit (SAU)** | 确认和完成处理 | last_resp |\n\n")
            f.write("## 数据表\n\n")
        
        f.write("| " + " | ".join(md_cols) + " |\n")
        f.write("|" + "---|" * len(md_cols) + "\n")
        for r in rows:
            vals = [fmt_cell(r.get(c, "")) for c in md_cols]
            f.write("| " + " | ".join(vals) + " |\n")

        # append Chinese field glossary
        f.write("\n## 字段说明\n\n")
        for c in md_cols:
            desc = COL_DESCRIPTIONS.get(c, "")
            if desc:
                f.write(f"- **{c}**: {desc}\n")


def write_detailed_timelines(dbg_data: Dict[int, DebugTiming], out_md: str):
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write("# Detailed per-instruction timelines\n\n")
        for dbg in sorted(dbg_data.keys()):
            d = dbg_data[dbg]
            f.write(f"## dbg {dbg}  \n")
            f.write(f"- mnemonic: {d.mnemonic or ''}\n")
            # summary table of stages
            f.write("\n| Stage | time (cycle) |\n")
            f.write("|---:|---:|\n")
            stages = [
                ("vliq_enq", d.vliq_enq), ("vliq_las", d.vliq_las), ("vsiq_enq", d.vsiq_enq),
                ("vsiq_sss", d.vsiq_sss), ("vsiq_sas", d.vsiq_sas), ("vsiq_deq", d.vsiq_deq),
                ("ssb_in", d.ssb_in), ("ssb_out", d.ssb_out), ("smu_push", d.smu_push),
                ("smu_pop", d.smu_pop), ("lrob_deq", d.lrob_deq), ("lrob_push", d.lrob_push),
                ("lmu_push", d.lmu_push), ("lmu_pop", d.lmu_pop), ("lss_comp", d.lss_comp)
            ]
            for name, val in stages:
                f.write(f"| {name} | {val if val is not None else ''} |\n")

            # requests table
            f.write("\n**Requests (per-tag/rid)**\n\n")
            f.write("| tag | req_time | A_time | D_time | addr |\n")
            f.write("|---:|---:|---:|---:|---:|\n")
            for req in d.requests:
                f.write(f"| {req.tag} | {req.req_time} | {req.a_time or ''} | {req.d_time or ''} | {hex(req.req_addr) if req.req_addr else ''} |\n")

            f.write("\n---\n\n")


def write_dbg_timeline_csv(dbg_data: Dict[int, DebugTiming], csv_path: str):
    # Split into loads and stores with columns relevant to each
    load_cols = [
        "dbg","mnemonic","mem_type","first_req","first_A","avg_A_to_D",
        "last_D","last_resp","completion_delta","completion_source",
        "vliq_enq","vliq_las","lrob_deq","lrob_push","lmu_push","lmu_pop","lss_comp",
    ]
    store_cols = [
        "dbg","mnemonic","mem_type","first_req","first_A","avg_A_to_D",
        "last_D","last_resp","completion_delta","completion_source",
        "vsiq_enq","vsiq_sss","vsiq_sas","vsiq_deq","ssb_in","ssb_out","smu_push","smu_pop","lmu_pop",
    ]

    # helper to write a CSV given cols and a filter predicate
    def _write(path, cols, predicate):
        # gather rows first so we can prune empty columns
        built_rows = []
        for dbg in sorted(dbg_data.keys()):
            d = dbg_data[dbg]
            if not predicate(d):
                continue
            metrics = compute_metrics_for_dbg(dbg, dbg_data)
            row = {c: metrics.get(c, "") for c in cols}
            # fill stage columns where present
            row.update({
                "vliq_enq": getattr(d, 'vliq_enq', "") or "",
                "vliq_las": getattr(d, 'vliq_las', "") or "",
                "vsiq_enq": getattr(d, 'vsiq_enq', "") or "",
                "vsiq_sss": getattr(d, 'vsiq_sss', "") or "",
                "vsiq_sas": getattr(d, 'vsiq_sas', "") or "",
                "vsiq_deq": getattr(d, 'vsiq_deq', "") or "",
                "ssb_in": getattr(d, 'ssb_in', "") or "",
                "ssb_out": getattr(d, 'ssb_out', "") or "",
                "smu_push": getattr(d, 'smu_push', "") or "",
                "smu_pop": getattr(d, 'smu_pop', "") or "",
                "lrob_deq": getattr(d, 'lrob_deq', "") or "",
                "lrob_push": getattr(d, 'lrob_push', "") or "",
                "lmu_push": getattr(d, 'lmu_push', "") or "",
                "lmu_pop": getattr(d, 'lmu_pop', "") or "",
                "lss_comp": getattr(d, 'lss_comp', "") or "",
            })
            trimmed = {c: row.get(c, "") for c in cols}
            built_rows.append(trimmed)

        # decide active columns (keep dbg, mnemonic)
        essential = {"dbg", "mnemonic"}
        active_cols = []
        for c in cols:
            if c in essential:
                active_cols.append(c)
                continue
            any_nonempty = any((r.get(c) is not None and str(r.get(c)).strip() != "") for r in built_rows)
            if any_nonempty:
                active_cols.append(c)

        # write final CSV
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=active_cols)
            w.writeheader()
            for r in built_rows:
                out = {c: r.get(c, "") for c in active_cols}
                w.writerow(out)

    _write('result/mem_access_micro_per_dbg_loads.csv', load_cols, lambda d: (d.mnemonic or '').startswith('vl'))
    _write('result/mem_access_micro_per_dbg_stores.csv', store_cols, lambda d: (d.mnemonic or '').startswith('vs'))


def write_event_visibility_guide():
    """Generate a markdown guide explaining LROB and SSB event visibility in Chinese."""
    guide = """# 内存事件可见性指南

## 📌 LROB 和 SSB 事件说明

### LROB (Load Reorder Buffer) 事件 - 仅用于 LOAD 指令

#### 何时在 CSV 中出现（有值）？
- ✅ 指令是 **LOAD 类型**：`vl*`, `vlse`, `vluxei`, `vluxeix` 等
- ✅ Load 进入了 **LROB 重排序阶段**（因序性需求）
- ✅ 同时观察到 **PUSH** (请求入队) 和 **DEQ** (响应出队) 事件
- ✅ 同一 dbg 的多个 tag 被正确配对聚合

#### 何时在 CSV 中缺失（空单元格）？
- ❌ 指令是 **STORE 类型**（不是 LOAD）
- ❌ Load 被**推测完成**，绕过了 LROB
- ❌ Load 直接**绕过重排序缓冲**
- ❌ 所有 tags 的 DEQ 没有对应的 PUSH（孤立事件）

#### 字段含义
| 字段 | 含义 | 时间关系 |
|------|------|---------|
| `lrob_push` | Load 条目写入 LROB 的周期 | 请求被接受 |
| `lrob_deq` | Load 响应从 LROB 出队的周期 | 响应就绪 |
| **正确顺序** | `lrob_deq > lrob_push` | ✅ 响应在请求后 |
| **错误顺序** | `lrob_deq < lrob_push` | ❌ 违反因果关系 |

---

### SSB (Store Segment Buffer) 事件 - 仅用于 STORE 指令

#### 何时在 CSV 中出现（有值）？
- ✅ 指令是 **STORE 类型**：`vs*`, `vsse`, `vsuxei`, `vsuxeix` 等
- ✅ Store 请求通过 **SSB 管道路由**（分段存储处理）
- ✅ 同时观察到 **IN** (入队) 和 **OUT** (出队) 事件

#### 何时在 CSV 中缺失（空单元格）？
- ❌ 指令是 **LOAD 类型**（不是 STORE）
- ❌ Store **绕过 SSB**，直接写内存
- ❌ Store **未经过缓冲**处理

#### 字段含义
| 字段 | 含义 | 时间关系 |
|------|------|---------|
| `ssb_in` | Store 分段进入 SSB 管道的周期 | 请求被接受 |
| `ssb_out` | Store 分段离开 SSB 管道的周期 | 处理完成 |
| **正确顺序** | `ssb_out > ssb_in` | ✅ 出队在入队后 |
| **错误顺序** | `ssb_out < ssb_in` | ❌ 违反因果关系 |

---

## 🔄 Tag 复用问题说明

Saturn 内存访问使用 12-bit tags (0-11) 进行请求追踪。这些 tags 会**循环复用**于多条指令：

### 问题场景
```
tag=8 在指令1 (dbg=159) 时：REQ@71921, PUSH@71921, DEQ@71925 ✓ 配对
tag=8 在指令2 (dbg=162) 时：REQ@72077, PUSH@72085, DEQ@72087 ✓ 配对
tag=8 在指令3 (dbg=169) 时：REQ@82885, PUSH@82890, DEQ@82895 ✓ 配对
```

### 解决方案
Parser 使用**两步处理**来处理 tag 复用：

1️⃣ **第一步**：扫描整个 trace 文件
   - 建立 tag → (请求时间, 指令ID) 的完整时间表
   - 记录每个指令使用的所有 tags

2️⃣ **第二步**：处理 LROB/SSB 事件
   - 验证每个事件的 tag 确实属于该指令
   - 验证事件发生日期在请求时间附近（500周期内）
   - 只从**有配对 PUSH 的 tags** 中取最小值
   - 过滤掉**孤立的 DEQ**（没有对应 PUSH）

### 具体例子（dbg=162）
```
指令 dbg=162 使用 tags: {8, 9, 10, 11}

tag=8:  PUSH=无      DEQ=72084  ❌ 孤立（无 PUSH，来自前一指令）
tag=9:  PUSH=72085   DEQ=72087  ✅ 配对
tag=10: PUSH=72086   DEQ=72091  ✅ 配对
tag=11: PUSH=72087   DEQ=72095  ✅ 配对

聚合结果（只从有 PUSH 的 tags）：
  lrob_push = min(72085, 72086, 72087) = 72085
  lrob_deq = min(72087, 72091, 72095) = 72087
  
✅ 正确顺序：72087 > 72085
```

---

## 📊 输出文件说明

### 主要输出
| 文件 | 内容 | LROB/SSB 显示 |
|------|------|--------------|
| `mem_access_micro_loads.csv` | 所有 LOAD 指令统计 | LOAD 的 LROB 列 |
| `mem_access_micro_stores.csv` | 所有 STORE 指令统计 | STORE 的 SSB 列 |
| `mem_access_micro_loads.md` | LOAD 指令详细分析 | - |
| `mem_access_micro_stores.md` | STORE 指令详细分析 | - |

### 详细输出
| 文件 | 内容 | 用途 |
|------|------|------|
| `mem_access_micro_per_dbg_loads.csv` | 每条 LOAD 指令的完整时间 | 精细分析 |
| `mem_access_micro_per_dbg_stores.csv` | 每条 STORE 指令的完整时间 | 精细分析 |
| `mem_access_micro_detailed_timelines.md` | 管道阶段详细时间序列 | 调试参考 |
| `mem_access_micro_event_visibility_guide.md` | 本文档 | 概念理解 |

---

## ❓ 常见问题

### Q1: 为什么我的 LOAD 指令没有 lrob_push/lrob_deq？

**可能原因**：
1. 指令是 STORE（不是 LOAD）→ 检查 `mnemonic` 列
2. Load 被推测完成 → 硬件优化，不需要 LROB
3. Load 直接返回数据 → 内存层级优化
4. 所有 tag 的 DEQ 都是孤立的 → 被 parser 过滤

**验证方法**：
```python
# 检查 CSV 中的 mnemonic
if "vl" in mnemonic:  # vle, vlse, vluxei 等
    print("是 LOAD 指令，应该有 LROB 或理由缺失")
else:
    print("是 STORE 指令，不应该有 LROB")
```

### Q2: 什么时候 SSB 事件会为空？

**正常情况**（为空是对的）：
- 指令是 LOAD（不是 STORE）
- Store 直接发到内存（bypass SSB）
- Store 在推测阶段已完成

**异常情况**（可能表示问题）：
- 指令是 STORE 但 SSB 為空 → 可能被优化掉
- 只有 ssb_in 没有 ssb_out → Store 未完成

### Q3: Tag 8 为什么在多个指令中出现？

**这是正常的！** Tag 复用是硬件设计：
- Saturn 使用 12 个 tags (0-11) 循环
- 指令1 用 tag=8 完成后，tag=8 可被指令2 重用
- Parser 通过时间关联来区分不同指令的相同 tag
- 详见上面的"Tag 复用问题说明"部分

### Q4: 我看到 lrob_deq < lrob_push（倒序），这正常吗？

**不正常！** 这违反了因果关系：
- 响应（DEQ）不可能在请求（PUSH）之前到达
- 如果看到这种情况，说明 parser 中可能有 bug
- 目前 parser 已修复此问题，所以不应看到倒序

---

## 🔍 验证和调试

### 检查时间顺序
```bash
# 检查所有 LOAD 的 LROB 顺序
grep "vl" result/mem_access_micro_loads.csv | awk -F, '{
  if ($15 != "" && $16 != "") {  # lrob_deq, lrob_push
    if ($15 < $16) print "❌", $2, "DEQ(" $15 ") < PUSH(" $16 ")"
    else print "✅", $2, "DEQ(" $15 ") > PUSH(" $16 ")"
  }
}'

# 检查所有 STORE 的 SSB 顺序
grep "vs" result/mem_access_micro_stores.csv | awk -F, '{
  if ($14 != "" && $15 != "") {  # ssb_in, ssb_out
    if ($15 < $14) print "❌", $2, "OUT(" $15 ") < IN(" $14 ")"
    else print "✅", $2, "OUT(" $15 ") > IN(" $14 ")"
  }
}'
```

### 理解指令类型
```bash
# 列出所有 LOAD 指令
grep "^vl" result/mem_access_micro_loads.csv | head -10

# 列出所有 STORE 指令
grep "^vs" result/mem_access_micro_stores.csv | head -10

# 查看特定指令的所有时间
grep ",162," result/mem_access_micro_per_dbg_loads.csv | head -1
```

---

## 📚 扩展阅读

- **LROB 基础**：Load Reorder Buffer 用于维持指令顺序，确保相关 loads 正确排序
- **SSB 基础**：Store Segment Buffer 用于分段处理 store 操作，提高内存吞吐量
- **Tag 复用**：所有 RISC-V 内存系统的通用设计，通过时间关联消除歧义
- **因果关系**：硬件事件必须满足逻辑顺序（请求→处理→响应）

"""
    with open("result/mem_access_micro_event_visibility_guide.md", "w", encoding="utf-8") as f:
        f.write(guide)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="", help="path to mem_access_micro.out")
    p.add_argument("--dump", default="", help="path to mem_access_micro.dump")
    p.add_argument("--src", default="", help="(unused) src path")
    args = p.parse_args()

    out_path = args.out or autodetect_out_path()
    dump_path = args.dump or autodetect_dump_path()

    dbg_data = parse_out_events(out_path)
    mapping = rebuild_dbg_mapping(out_path, dump_path, args.src)

    rows = []
    seq = 0
    # prefer mapping order
    ordered_dbg = list(mapping.keys()) if mapping else sorted(dbg_data.keys())
    seen = set()
    for dbg in ordered_dbg:
        seen.add(dbg)
        m = compute_metrics_for_dbg(dbg, dbg_data)
        func, mnemonic = mapping.get(dbg, (None, None))
        if func:
            m["func"] = func
        if mnemonic and not m.get("mnemonic"):
            m["mnemonic"] = mnemonic
        m["seq"] = seq
        seq += 1
        rows.append(m)
    # add any remaining dbg
    for dbg in sorted(dbg_data.keys()):
        if dbg in seen:
            continue
        m = compute_metrics_for_dbg(dbg, dbg_data)
        m["seq"] = seq
        seq += 1
        rows.append(m)

    # split loads/stores
    load_rows = [r for r in rows if (r.get("mnemonic", "").startswith(MEM_PREFIX_LOAD))]
    store_rows = [r for r in rows if (r.get("mnemonic", "").startswith(MEM_PREFIX_STORE))]

    if load_rows:
        write_simple_csv_md(load_rows, "result/mem_access_micro_loads.csv", "result/mem_access_micro_loads.md")
    if store_rows:
        write_simple_csv_md(store_rows, "result/mem_access_micro_stores.csv", "result/mem_access_micro_stores.md")

    print(f"Processed out: {out_path}")
    print(f"Wrote loads: result/mem_access_micro_loads.csv (rows={len(load_rows)})")
    print(f"Wrote stores: result/mem_access_micro_stores.csv (rows={len(store_rows)})")

if __name__ == '__main__':
    main()

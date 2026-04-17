# Advanced Vector Test Results — Summary

This report summarizes the results of the `advanced_vec_test` benchmark, which evaluates the performance of stable and risky vector instructions in both DRAM and TCM environments.

## Chaining and Execution Findings

### 1. Register Chaining Observations
Based on the `advanced_vec_test.out` trace, we observed the following sequence for a permute instruction (e.g., `vrgather` or similar, `func6=14/15/12`):
- **ISSUE** and **COMMIT** for the instruction blocks occur in rapid succession.
- **PERMUTE-EXEC** starts shortly after the first indices are committed.
- **PERMUTE-WRITE** follows the execution, often with a full mask (`111...111`), indicating all lanes are being updated.

For example, at time `13458`, an instruction (`func6=14`) is issued.
```
[ISSUE] time=13458 func3=4 func6=14 ... eidx=0 rd=16
[COMMIT] time=13458 wvd_eg=64 ... rd=16 eidx=0
[PERMUTE-EXEC] time=13459 ... eidx=0 wvd_eg=64
[PERMUTE-WRITE] time=13459 eg=64 mask=000...000
```
*Note: Some initially executed segments show a zero mask, which might be due to initialization or specific instruction semantics (like `vslideup` with offset).*

### 2. Performance Comparison: DRAM vs. TCM

The benchmark compares instruction latency when data is placed in DRAM vs. TCM. TCM (Tightly Coupled Memory) consistently shows significantly lower latencies.

| Instruction Category | Instruction | DRAM Latency (cyc) | TCM Latency (cyc) | Speedup |
| :--- | :--- | :---: | :---: | :---: |
| **Stable** | `vslideup.vx` | 105 | 35 | 3.0x |
| | `vslidedown.vx` | 47 | 35 | 1.3x |
| | `vslide1up.vx` | 46 | 34 | 1.4x |
| | `vslide1down.vx` | 47 | 35 | 1.3x |
| | `vrgather.vx` | 54 | 33 | 1.6x |
| **Risky (Reductions)** | `vredsum.vs` | 73 | 63 | 1.2x |
| | `vredor.vs` | 67 | 61 | 1.1x |
| | `vredxor.vs` | 67 | 61 | 1.1x |
| | `vredmaxu.vs` | 67 | 61 | 1.1x |
| **Chaining** | Reduction Chain | 106 | 100 | 1.06x |

### 3. Reduction Chaining
The "Reduction Chain" (sum -> or -> xor -> maxu) takes **106 cycles** in DRAM and **100 cycles** in TCM. 
- Individual reductions take ~67-73 cycles in DRAM. If run serially without hardware optimization, we might expect ~270+ cycles.
- The significantly lower "Chain Total" (106 cycles) suggests that the hardware is successfully **chaining** these reduction operations or performing them in a way that overlaps their execution significantly.

## Detailed Trace Discoveries
1. **Instruction Overlap:** Multiple `ISSUE` and `COMMIT` events for the same `rd` (register destination) with different `eidx` (element index) blocks indicate that vector instructions are processed in chunks (likely corresponding to the hardware's vector datapath width).
2. **Execution Timing:** In TCM, the time between `ISSUE` and `PERMUTE-EXEC` is minimal, often occurring in the same or next cycle, confirming minimal memory stall overhead.
3. **Masking Behavior:** The `PERMUTE-WRITE` logs show masks being applied at the element group (`eg`) level. This is crucial for instructions like `vslideup` where some lanes are preserved and others updated.

## Conclusion
The Saturn vector unit demonstrates robust support for standard vector permutations and reductions. The TCM provides a substantial performance boost for memory-dependent vector operations. Most importantly, the reduction chain results provide strong evidence of **vector chaining** capability, allowing multiple vector instructions to execute with overlapping latencies.

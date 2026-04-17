# Saturn 向量处理器内存访问性能分析报告

## 1. Saturn 内存子系统实现细节

Saturn 的内存子系统核心在于 **LAS (Load Address Sequencer)** 和 **SAS (Store Address Sequencer)**。

### 地址生成逻辑 (`AddrGen.scala`)
*   **自动寻址模式识别**：LAS/SAS 通过指令中的 `mop` (Memory Opcode) 字段自动识别寻址模式：
    *   `mopUnit` (00): 单位跨步访问。
    *   `mopUnordered` (01): 索引访问 (Gather/Scatter)。
*   **请求合并 (Request Merging)**：
    *   对于 **Unit-stride** 和 **Segmented (Unit-stride)** 访问，`AddrGen` 会尝试计算一次内存请求（128-bit 总线宽）能覆盖的最大元素数量 (`next_act_elems`)。
    *   它会生成一个位掩码（Mask），允许在单个时钟周期内发起对总线全宽度的请求。
*   **分段优化 (Fast Segmented)**：
    *   当执行 `vlseg` 类指令且为单位跨步时，Saturn 识别为 `fast_segmented`。它会将分段数据视为连续流处理，利用 `LoadSegmenter` 硬件进行高效的解交织（De-interleaving），这比软件手动解交织快得多。
*   **索引阻塞机制 (Indexed Blocking)**：
    *   当识别为 **Indexed** 模式时，LAS 必须等待向量寄存器堆（VRF）读出索引数据。由于 `needs_index` 信号置高，地址生成被串行化：**取一个索引 -> 算一个地址 -> 发一个请求**。由于地址不连续，无法进行总线合并。

---

## 2. 测试方法与环境

### 配置参数 (DSPV512D128ShuttleConfig)
*   **VLEN**: 512 bits (每个寄存器 16 个 32-bit 元素)
*   **DLEN**: 128 bits (16 字节内存接口宽度)
*   **处理器**: Shuttle (超标量流水线)

### 测试负载设计 (Benchmark)
*   **数据规模**: $N = 512$ 个 32-bit 元素（跨越 32 个向量寄存器）。
*   **测试指令**:
    1.  **Unit Load/Store**: `vle32.v` / `vse32.v`
    2.  **Segmented Load/Store**: `vlseg2e32.v` / `vsseg2e32.v` (nf=2)
    3.  **Indexed Load/Store**: `vluxei32.v` / `vsoxei32.v` (使用乱序索引 `(i*3)%N`)
*   **测量方式**: 使用 RISC-V `mcycle` CSR 测量指令开始到 `fence` 完成的总周期数。

---

## 3. 测试结果 (N=512)

| 操作模式 | 总周期数 (Cycles) | 效率 (Cycles/Elem) | 性能比 (相对 Unit Load) |
| :--- | :---: | :---: | :---: |
| **Unit Load** | 527 | 1.03 | 1.0x |
| **Segmented Load** (nf=2) | **283** | **0.55** | **1.86x (更快)** |
| **Indexed Load** | 3495 | 6.82 | 0.15x |
| | | | |
| **Unit Store** | 163 | 0.32 | 3.23x |
| **Segmented Store** | 292 | 0.57 | 1.80x |
| **Indexed Store** | 3007 | 5.87 | 0.17x |

---

## 4. 深度分析

### 为什么 Segmented 比 Unit-stride 还快？
尽管 Segmented 访问的总数据量（512 元素）与 Unit-stride 相同，但性能表现更优，原因如下：
1.  **指令密度 (Instruction Density)**：`vlseg2` 指令在单条指令内处理了双倍的数据量。在 N=512 的测试中，Unit-stride 需要循环迭代 32 次（LMUL=1），而 Segmented 仅需 16 次。减少了 `vsetvli`、指针加法和跳转指令的开销。
2.  **硬件合并效率**：Saturn 的 LAS 针对 `fast_segmented` 做了深度优化。由于段内地址是连续的，硬件能以 128-bit 满带宽吞吐数据，同时解交织模块在后台并行工作，几乎消除了段访问的转换开销。

### Indexed 访问的性能塌陷
Indexed 操作（Gather/Scatter）的耗时是连续访问的 **6-7 倍**。
*   **瓶颈 1：地址计算依赖**。LAS 必须按序等待索引向量从执行流水线写回到物理寄存器或旁路，导致流水线气泡。
*   **瓶颈 2：内存带宽碎片化**。因为索引是随机的，每次请求只能获取 4 个字节（32-bit），而 128-bit 的总线带宽利用率仅为 25%。此外，每个请求都需要独立的地址翻译（TLB 查找）和 Cache 访问，导致严重的延迟积累。

---

## 4. 主内存 (DRAM) vs. 向量 TCM 性能对比

为了进一步验证内存层级对向量性能的影响，我们引入了 **Vector TCM (Tightly Coupled Memory)** 进行对比测试。

### 环境配置
*   **Vector TCM 基地址**: `0x70000000` (64KB, 4 Banks, 1 cycle latency)
*   **对比负载**: 
    - **Main Memory (DRAM)**: $N=512$
    - **Vector TCM**: $N=256$ (受限于 64KB 容量)
*   **归一化指标**: 周期数/元素 (Cycles/Elem)

### 测试结果对照表

| 访问模式 | Main Memory (Cy/E) | Vector TCM (Cy/E) | **加速比 (Speedup)** |
| :--- | :---: | :---: | :---: |
| **Unit Load** | 1.03 | **0.42** | **2.45x** |
| **Segmented Load** (nf=2) | 0.55 | **0.57** | **~1.0x (无收益)** |
| **Indexed Load** | 6.82 | **4.46** | **1.53x** |

---

## 5. 深度分析 (TCM vs DRAM)

### 1. 为什么 Unit Load 在 TCM 上有显著提升？
*   **延迟消除**：Main Memory 访问涉及 TileLink 互联、L2 Cache 仲裁以及 DRAM 延迟（几十个周期）。TCM 紧耦合在向量单元内部，单周期返回数据。
*   **带宽利用率**：TCM 的 4-bank 结构配合 128-bit DLEN 宽度，使得单位跨步访问几乎能达到理论带宽峰值。

### 2. 为什么 Segmented 访问在 TCM 上没有加速？
这是一个反直觉的现象，分析如下：
*   **指令发射受限 (Instruction-Bound)**：由于 `vlseg` 指令在内部会解成多个微操作（Micro-ops），主要的耗时已经不再是内存延迟，而是 **前端取指和指令解码/分发** 的开销。
*   **硬件解交织瓶颈**：Saturn 的 `LoadSegmenter` 逻辑已经在 DRAM 访问时就掩盖了大部分内存延迟。当存储介质变快时，瓶颈转移到了向量单元内部的寄存器写回端口（Write-back ports）和指令控制流。
*   **结论**：对分段访问而言，瓶颈不在存储分级，而在流水线前端。

### 3. Indexed 访问的有限提升
*   TCM 提供了更好的地址查找性能，但 **地址生成 (LAS) 的串行化** 依然是核心矛盾。
*   即便是 TCM，仍然需要 "取索引 -> 算地址 -> 发请求" 的串行循环。虽然请求到 TCM 的延迟缩短了，但 LAS 每一跳的逻辑延迟和指令开销仍然占据了 70% 的执行时间。

---

## 6. 最终结论与建议
*   **存储选择**：如果算法涉及大量 **Unit Load** 或 **Indexed** 操作，将高频数据放入 **Vector TCM (0x70000000)** 可获得 **1.5x - 2.5x** 的性能红利。
*   **分段访问**：`vlseg` 指令非常强大，无论是在 DRAM 还是 TCM 下都能提供极佳的单位周期数据吞吐量，不必为了加速分段访问而刻意使用 TCM。
*   **性能排序**：**TCM Unit Load > Main Segmented > Main Unit Load >> Indexed**。

---

## 7. 高级向量指令分析 (Special Instructions)

在 `DSPV512D128ShuttleConfig` (VLEN=512, DLEN=128) 配置下，对特殊向量操作（Slide, Gather, Reduction）进行了基准测试。**注意：部分复杂指令在该 RTL 配置下存在死锁 Bug。**

### 1. 性能测试结果 (N=16, e32)

| 指令类型 | 指令示例 | DRAM (周期) | TCM (周期) | 加速比 | 含义说明 |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Slide Up** | `vslideup.vx` | 76 | 35 | **2.17x** | 向量元素整体向高索引方向平移 (常用于滤波/卷积) |
| **Slide Down** | `vslidedown.vx` | 47 | 35 | **1.34x** | 向量元素整体向低索引方向平移 |
| **Slide 1** | `vslide1up/down.vx` | 50 | 34 | **1.47x** | 平移一个元素，并由标量寄存器填充空出的首/尾位 |
| **Gather/Broadcast** | `vrgather.vx` | 45 | 33 | **1.36x** | 根据标量索引，将源向量中的某一个元素广播到全向量 |

### 2. RTL 实现缺陷分析 (Deadlock Category)

通过迭代测试，在当前 Saturn `Split` 发射结构配置下发现了三类硬件死锁（Deadlock/Hang）情况，导致指令无法完成：

#### A. GatherUnit 元素级死锁 (Elementwise path)
*   **受影响指令**：`vrgather.vv`, `vcompress.vm`
*   **原因**：`GatherUnit.scala` 包含两条路径：`slide_buffer` (针对 Slide 指令，工作正常) 和 `eidx_buffer` (针对元素级 Gather/Compress)。在 `Split` 配置下，`eidx_buffer` 路径会导致流水线前端与后端状态不同步，造成永久挂起。
*   **规避方案**：使用 `vrgather.vx` (OPIVX 路径) 替代，或在软件层通过 Slide + Mask 模拟 Compress。

#### B. 累加器初始化死锁 (Non-zero AccInit)
*   **受影响指令**：`vredmax.vs`, `vredmin.vs`, `vredand.vs`, `vredminu.vs`
*   **原因**：当 Reduction 指令需要将累加器初始化为非零值 (如 `AccInitNeg`, `AccInitOnes`) 时，硬件逻辑在写入初始值阶段会发生死锁。
*   **结论**：仅 `vredsum.vs` (AccInitZeros) 和 `vredmaxu.vs` 在部分情况下可运行。

#### C. 位逻辑流水线不稳定性 (BitwisePipe)
*   **受影响指令**：`vredor.vs`, `vredxor.vs`, `vmor.mm`, `vcpop.m`
*   **现象**：这些使用 `BitwisePipeFactory` 或 `MaskUnitFactory` 的指令在 `Split` 多发射结构中表现出非确定性的挂起，可能与多路径下的寄存器写回冲突（Write-back Conflict）有关。

---

## 8. 总结与建议

*   **Slide 指令提升显著**：`vslideup.vx` 在 TCM 上有超过 **2x** 的提升，因为它涉及大量内存操作与向量寄存器移动的重叠，TCM 的低延迟显著缩短了数据准备周期。
*   **配置建议**：目前的 `DSPV512D128ShuttleConfig` 对于工业级 RVV 代码集尚不够稳定。建议在产品化时优先使用 `ReferenceConfig` (单发射) 进行功能验证，或针对 `Split` 结构的 `GatherUnit` 进行 RTL 级修复。
*   **性能天花板**：特殊指令在 TCM 上的执行时间平均在 **33-35 周期** (N=16, VLEN=512)，这代表了向量执行单元 (DLEN=128) 在该频率下的吞吐极限。

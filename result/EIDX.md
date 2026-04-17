# `eidx` 分配与解释（Saturn）

## 概要

- `eidx`：element index（元素索引），表示 sequencer 在当前向量指令处理流程中定位到的元素位置。
- 主要用途：生成 head/tail 掩码、选择 VRF/VM 的 EG（element-group）、计算写回的 EG id（wvd_eg）、判定何时触发写回或 chaining（链式传递）。

## 核心概念（快速上手）

- `element-group (EG)`：按内存对齐/访问粒度把连续元素分成的组。EG 通常对应内存中的对齐块（例如 64 字节），在跨组时需要触发写回或更新 mask。
- `eew`：element effective width（以对齐位表示，通常用移位量表示元素大小）。
- `vstart` / `vl`：向量起始索引与向量长度上限，`eidx` 在 dispatch 时初始化为 `vstart`，并受 `vl` 截断。

## 关键函数与精确代码（来自源码）

下面直接摘录实现，并附上精炼说明。代码位于 [generators/saturn/src/main/scala/backend/Sequencer.scala](generators/saturn/src/main/scala/backend/Sequencer.scala)。

```scala
def get_next_eidx(vl: UInt, eidx: UInt, eew: UInt, sub_len: UInt, reads_mask: Bool, elementwise: Bool, len: Int) = {
  val lenOffBits = log2Ceil(len / 8)
  val next = Wire(UInt((1+log2Ceil(maxVLMax)).W))
  next := Mux(elementwise, eidx +& 1.U, Mux(reads_mask,
    eidx +& len.U,
    (((eidx >> (lenOffBits.U - eew - sub_len)) +& 1.U) << (lenOffBits.U - eew - sub_len))
  ))
  min(vl, next)
}

def next_is_new_eg(eidx: UInt, next_eidx: UInt, eew: UInt, masked: Bool) = {
  val offset = Mux(masked, log2Ceil(dLen).U, dLenOffBits.U - eew)
  (next_eidx >> offset) =/= (eidx >> offset)
}
```

说明：

- `get_next_eidx` 的推进规则（优先级）：
  1. 若 `elementwise`：按元素推进：`next = eidx + 1`。
  2. 否则若 `reads_mask`：以字节/块为单位跳过：`next = eidx + len`（len 以字节计）。
  3. 否则：按对齐/组粒度推进：先右移到组粒度（位移 = `lenOffBits - eew - sub_len`），+1 后左移回去，保证对齐跳跃。
  4. 最后 `min(vl, next)`：确保不会超出 `vl`。

- `next_is_new_eg`：通过选择偏移 `offset`（若按掩码分组则用 `log2Ceil(dLen)`，否则使用 `dLenOffBits - eew`）把 `eidx`/`next_eidx` 右移，再比较高位；若不同则说明进入新的 EG。

## 各 Sequencer 中 `eidx` 的使用（关键摘录与逻辑）

- `ExecuteSequencer`（核心流水调度，见 [generators/saturn/src/main/scala/backend/ExecuteSequencer.scala](generators/saturn/src/main/scala/backend/ExecuteSequencer.scala)）：

```scala
val next_eidx = Reg(UInt((1+log2Ceil(maxVLMax)).W))
val eidx      = Reg(UInt(log2Ceil(maxVLMax).W))
...
when (io.dis.fire) {
  eidx      := 0.U
  next_eidx := get_next_eidx(dis_eff_vl, 0.U, dis_incr_eew, 0.U, dis_increments_as_mask, dis_ctrl.bool(Elementwise), dLen)
}
io.iss.bits.eidx := eidx
val eidx_tail = next_eidx === eff_vl
val tail = Mux(inst.reduction && usesAcc.B, acc_fold && acc_last, eidx_tail)
```

说明：dispatch 时初始化 `eidx`（通常 0 或 `vstart`），计算 `next_eidx`；issue 路径用 `eidx` 作为当前元素索引并以 `tail` 判断是否完成。`ExecuteSequencer` 同时会将 `eidx` 用于打印调试日志与选择 EG。 

- `LoadSequencer`（加载，见 [generators/saturn/src/main/scala/backend/LoadSequencer.scala](generators/saturn/src/main/scala/backend/LoadSequencer.scala)）：

```scala
val eidx = Reg(UInt(log2Ceil(maxVLMax).W))
val next_eidx = get_next_eidx(inst.vconfig.vl, eidx, inst.mem_elem_size, 0.U, false.B, false.B, mLen)
val tail = next_eidx === inst.vconfig.vl && sidx === inst.seg_nf

when (io.dis.fire) {
  eidx := iss_inst.vstart
  sidx := iss_inst.segstart
}

io.iss.bits.eidx := eidx

when (io.iss.fire && !tail) {
  when (sidx === inst.seg_nf) {
    sidx := 0.U
    eidx := next_eidx
  } .otherwise { sidx := sidx + 1.U }
}
```

说明：`LoadSequencer` 在 dispatch 时把 `eidx` 设为 `vstart`，每次 issue 根据 segment (`sidx`) 推进；段末会把 `eidx` 更新为 `next_eidx`。head/tail 掩码由 `get_head_mask`/`get_tail_mask` 基于 `eidx`/`next_eidx` 生成。

- `StoreSequencer`（存储，见 [generators/saturn/src/main/scala/backend/StoreSequencer.scala](generators/saturn/src/main/scala/backend/StoreSequencer.scala)）：

```scala
val eidx = Reg(UInt(log2Ceil(maxVLMax).W))
val sub_mlen = Reg(UInt(2.W))
val next_eidx = get_next_eidx(inst.vconfig.vl, eidx, inst.mem_elem_size, sub_mlen, false.B, false.B, mLen)

when (io.dis.fire) {
  eidx := iss_inst.vstart
  sub_mlen := Mux(...)
}

when (io.iss.fire && !tail) {
  when (sidx === inst.seg_nf) {
    sidx := 0.U
    eidx := next_eidx
  } .otherwise { sidx := sidx + 1.U }
}
```

说明：与 `LoadSequencer` 类似，但 `StoreSequencer` 使用 `sub_mlen` 调整对齐/推进粒度（用于分段/子长度对齐）。

## 工作流程（简要步骤）

1. Dispatch (`io.dis.fire`)：初始化 `eidx`（通常为 `vstart` 或 0），并计算 `next_eidx`。
2. Issue (`io.iss.fire`)：把当前 `eidx` 作为本次发射的元素索引输出，使用 head/tail mask 限制首尾访问。
3. 在 segment 边界（`sidx == seg_nf`）或根据推进规则把 `eidx := next_eidx`，并通过 `next_is_new_eg` 判断是否跨 EG，从而触发写回/清除 EG mask/链传递。

## 举例（帮助理解）

假设 EG 大小 `dLen = 64` 字节，元素宽 32-bit（4 字节 -> eew=2），则每个 EG 包含 64/4 = 16 个元素。当 `eidx` 从 15 进到 16（或 `next_eidx` 的高位与 `eidx` 的高位不同）时，`next_is_new_eg` 返回 true，表示进入新 EG，应触发对应的写回或 mask 更新。

## 注意事项

- 对于 gather/rgather 指令，`eidx` 可能来自外部源（`io.vgu.gather_eidx`），不总是由 sequencer 自增。
- `elementwise` 与 `reads_mask` 的差别会显著改变推进粒度（逐元素 vs 按块跳跃），影响 head/tail mask、VGPR/VM 访问顺序与 chaining 行为。

## 参考（源码位置）

- `Sequencer` 核心：[generators/saturn/src/main/scala/backend/Sequencer.scala](generators/saturn/src/main/scala/backend/Sequencer.scala)
- `ExecuteSequencer`：[generators/saturn/src/main/scala/backend/ExecuteSequencer.scala](generators/saturn/src/main/scala/backend/ExecuteSequencer.scala)
- `LoadSequencer`：[generators/saturn/src/main/scala/backend/LoadSequencer.scala](generators/saturn/src/main/scala/backend/LoadSequencer.scala)
- `StoreSequencer`：[generators/saturn/src/main/scala/backend/StoreSequencer.scala](generators/saturn/src/main/scala/backend/StoreSequencer.scala)

---
（已重写，若需加入逐行编号注释或更多示例，我可以继续补充。）

## 发射结构（issue structure）说明

下面为 `issStructure` 的几种常见配置的要点（译自仓库文档/注释），便于理解 sequencer/执行单元的映射与调度开销：

- **Unified（统一）**：
  - 单一 sequencer 负责所有算术指令（整数与浮点），由一个共享的 VIQ 提供指令。
  - 优点：硬件开销最小。
  - 缺点：无法同时把整数与浮点 FU 饱和（不能同时高效驱动两类单元）。

- **Shared（共享）**：
  - 有独立的整数与浮点 sequencer，但两者由同一个 VIQ 供给指令；VIQ 将指令发往整数或浮点 sequencer。
  - 优点：在指令合理交错的情况下可以同时饱和整数和浮点执行单元，吞吐更好。
  - 权衡：需要把指令在 VIQ 层正确分发以实现并行性。

- **Split（分离）**：
  - 整数与浮点各自有独立的 sequencer 且由各自的 issue queue 提供指令（两个独立队列）。
  - 优点：性能最高、最灵活，能在整数/浮点间做动态乱序调度，适合高性能内核。
  - 缺点：增加面积开销（独立的浮点 issue queue）。

- **Multi-ALU**：
  - 增加额外的整数 sequencer 与功能单元，使整数 ALU 指令可以双发射（dual-issue）。
  - 适用于对整数 ALU 吞吐有高需求的场景。

- **Multi-MAC / Multi-FMA**：
  - 分别为整数 MAC 或浮点 FMA 增加额外的 sequencer 与功能单元，支持双发射的 MAC/FMA。
  - 这些配置提高算术吞吐相对于内存带宽的比率，能在数据重用充分的内核上获得更高性能。

（以上配置可在 `generators/saturn/src/main/scala/common/Parameters.scala` 的 `VectorIssueStructure` 中查看具体生成逻辑与所包含的 FunctionalUnit 列表。）

### 精确代码摘录与逻辑（直接来自源码）

下面给出核心函数与各 Sequencer 中 `eidx` 的精确摘录，以及简要中文逻辑说明，便于追溯实现。

#### `Sequencer.scala` 中的核心函数
```scala
def get_next_eidx(vl: UInt, eidx: UInt, eew: UInt, sub_len: UInt, reads_mask: Bool, elementwise: Bool, len: Int) = {
  val lenOffBits = log2Ceil(len / 8)
  val next = Wire(UInt((1+log2Ceil(maxVLMax)).W))
  next := Mux(elementwise, eidx +& 1.U, Mux(reads_mask,
    eidx +& len.U,
    (((eidx >> (lenOffBits.U - eew - sub_len)) +& 1.U) << (lenOffBits.U - eew - sub_len))
  ))
  min(vl, next)
}
def next_is_new_eg(eidx: UInt, next_eidx: UInt, eew: UInt, masked: Bool) = {
  val offset = Mux(masked, log2Ceil(dLen).U, dLenOffBits.U - eew)
  (next_eidx >> offset) =/= (eidx >> offset)
}
```

简要逻辑：
- `get_next_eidx`：
  - 若 `elementwise`：逐元素推进 `eidx + 1`。
  - 否若 `reads_mask`：按字节/块推进 `eidx + len`（以 `len` 为字节单位跳过）。
  - 否：按对齐/组粒度推进：将 `eidx` 右移到组粒度（`lenOffBits - eew - sub_len`），+1 后左移回去。
  - 最后用 `min(vl, next)` 截断，保证不超过 `vl`。
- `next_is_new_eg`：根据是否按掩码分组选择偏移 `offset`，将 `eidx` 与 `next_eidx` 右移后比较高位；若不同则进入新 `element-group`（EG）。

#### `ExecuteSequencer.scala` 中关于 `eidx` 的关键摘录
```scala
val next_eidx    = Reg(UInt((1+log2Ceil(maxVLMax)).W))
val eidx         = Reg(UInt(log2Ceil(maxVLMax).W))
...
when (io.dis.fire) {
  ...
  eidx          := 0.U
  next_eidx     := get_next_eidx(dis_eff_vl, 0.U, dis_incr_eew, 0.U, dis_increments_as_mask, dis_ctrl.bool(Elementwise), dLen)
  ...
} .elsewhen (io.iss.fire) {
  valid := !tail
  head := false.B
}
...
io.iss.bits.eidx := eidx
val eidx_tail = next_eidx === eff_vl
val tail      = Mux(inst.reduction && usesAcc.B, acc_fold && acc_last, eidx_tail)
```

说明：在 dispatch 时初始化 `eidx`（通常 0 或 `vstart`），计算 `next_eidx`；在 issue 路径中通过 `io.iss.bits.eidx` 将当前 `eidx` 传给下游，并以 `tail` 判断完成条件。

#### `LoadSequencer.scala` 中关于 `eidx` 的关键摘录
```scala
val eidx  = Reg(UInt(log2Ceil(maxVLMax).W))
val next_eidx = get_next_eidx(inst.vconfig.vl, eidx, inst.mem_elem_size, 0.U, false.B, false.B, mLen)
val tail      = next_eidx === inst.vconfig.vl && sidx === inst.seg_nf
...
when (io.dis.fire) {
  eidx := iss_inst.vstart
  sidx := iss_inst.segstart
  ...
} .elsewhen (io.iss.fire) {
  valid := !tail
  head  := false.B
}
...
io.iss.bits.eidx := eidx
...
when (io.iss.fire && !tail) {
  when (sidx === inst.seg_nf) {
    sidx := 0.U
    eidx := next_eidx
  } .otherwise {
    sidx := sidx + 1.U
  }
}
```

说明：`LoadSequencer` 在 dispatch 时把 `eidx` 设为 `vstart`，在每次 issue 且未到 `tail` 时根据 segment（`sidx`）推进，段末更新 `eidx := next_eidx`。

#### `StoreSequencer.scala` 中关于 `eidx` 的关键摘录
```scala
val eidx     = Reg(UInt(log2Ceil(maxVLMax).W))
val sub_mlen = Reg(UInt(2.W))
val next_eidx = get_next_eidx(inst.vconfig.vl, eidx, inst.mem_elem_size, sub_mlen, false.B, false.B, mLen)
...
when (io.dis.fire) {
  eidx  := iss_inst.vstart
  sub_mlen := Mux(...)
  ...
} .elsewhen (io.iss.fire) {
  valid := !tail
  head := false.B
}
...
when (io.iss.fire && !tail) {
  when (sidx === inst.seg_nf) {
    sidx := 0.U
    eidx := next_eidx
  } .otherwise { sidx := sidx + 1.U }
}
```

说明：与 `LoadSequencer` 相似，但 `StoreSequencer` 会根据 `sub_mlen`（子长度偏移）调整对齐/推进粒度。

#### 精简总结
- `get_next_eidx` 负责按照指令属性（`elementwise` / `reads_mask` / `eew` / `sub_len`）计算下一元素索引 `next_eidx`；最终用 `min(vl, next)` 限制范围。
- `next_is_new_eg` 通过右移比较高位判断是否跨越 EG（按 `dLen` 或按 `dLenOffBits - eew` 来划分），用于触发写回、清除 EG mask、或链（chaining）管理。
- 各 Sequencer 在 `io.dis.fire` 初始化 `eidx`（通常为 `vstart`），在 `io.iss.fire` 路径发出当前 `eidx` 并在段末更新为 `next_eidx`，同时使用 `head/tail` mask 来限制首尾访问。





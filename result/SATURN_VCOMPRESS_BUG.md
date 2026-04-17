# Saturn RTL Bug: `vcompress.vm` / `vrgather.vv` 死锁

## 问题现象

在 `DSPV512D128ShuttleConfig`（以及所有包含 `PermuteUnitFactory` 的配置）上，执行以下指令会导致仿真挂死，最终触发看门狗超时，退出码为 `1337`：

- `vcompress.vm vd, vs2, vs1`
- `vrgather.vv  vd, vs2, vs1`（部分情况下）
- `vrgatherei16.vv vd, vs2, vs1`（部分情况下）

```
*** FAILED *** (tohost = 1337)
[654935000] %Error: TestHarness.sv:99: Assertion failed in TOP.TestDriver.testHarness
```

`tohost = 1337` 是 Saturn `syscalls.c` 里的 watchdog 超时代码（见 `common/syscalls.c:64`）。

---

## 软件代码（符合 RVV 1.0 规范，无误）

测试代码如下，完整符合 RVV 1.0 规范：

```c
/* vcompress.vm: 按 spec 例子写法
 *   vd=v2, vs2=v1(data), vs1=v0(mask)
 *   vd ≠ vs2 ≠ vs1, 无寄存器重叠
 *   vm=1 (无 mask), vstart=0 */
asm volatile(
    "vsetivli %0,16,e32,m1,tu,ma\n\t"   /* vl=16, tail-undisturbed */
    "vle32.v  v1,(%2)\n\t"              /* v1 = 源数据 */
    "vle32.v  v2,(%4)\n\t"              /* v2 = 目的预初始化 */
    "vlm.v    v0,(%3)\n\t"              /* v0 = mask */
    "vcompress.vm v2,v1,v0\n\t"         /* 压缩: vd=v2, vs2=v1, vs1=v0 */
    "vse32.v  v2,(%4)\n\t" "fence"
    :"=r"(vl):"r"(N),"r"(data),"r"(cmask),"r"(out)
    :"v0","v1","v2","memory");
```

RVV 1.0 Spec（`v-spec.adoc §16.8`）对 `vcompress.vm vd, vs2, vs1` 的约束：

| 约束 | 规范要求 | 代码实际 | 状态 |
|------|---------|---------|------|
| `vd ≠ vs2` | v2 ≠ v1 | ✓ | 合规 |
| `vd ≠ vs1` | v2 ≠ v0 | ✓ | 合规 |
| `vm=1`（无 mask）| `.vm` 后缀即 vm=1 | ✓ | 合规 |
| `vstart = 0` | 默认 0 | ✓ | 合规 |

**软件端无任何问题，死锁完全由 RTL 引起。**

---

## RTL 根因分析

### Bug 位置 1：`ExecuteSequencer.scala` — VRF bank 永久锁死

```scala
// ExecuteSequencer.scala, line ~247
when (compress) { // The destination is not known at this point
    io.pipe_write_req.bank_sel := ~(0.U(vParams.vrfBanking.W))
}
```

**问题**：`vcompress` 的写目标地址需要在运行时由 prefix-sum（popcount 累加）动态计算，
在 issue 时地址未知，RTL 用将 `bank_sel` 置全 1 的方式"保守地"锁住**所有 VRF bank**。

- `DSPV512D128ShuttleConfig` 有 `vrfBanking = 4`，即 4 个 bank 全部被一条 `vcompress` 独占。
- 后续任何指令（包括无关的 `vredsum`、`vredor` 等）都在等 bank 释放，永久阻塞。

### Bug 位置 2：`ExecuteSequencer.scala` — `wvd_mask` 永远不清零

```scala
// ExecuteSequencer.scala, line ~338
when (next_is_new_eg(eidx, next_eidx, vd_eew, inst.writes_mask)
      && !inst.reduction && !compress) {    // ← !compress guard
    val wvd_clr_mask = UIntToOH(io.iss.bits.wvd_eg)
    wvd_mask := wvd_mask & ~wvd_clr_mask   // compress 永远不走这里
}
```

**问题**：`!compress` 的 guard 导致 `vcompress` 执行期间 `wvd_mask`（写寄存器追踪位图）**永远不清零**，
后续所有指令的 `data_hazard` 检查永远为 true，形成死锁。

### 两个 Bug 的联合效果

```
vcompress.vm 开始执行
  ├─ bank_sel = 0b1111  → 所有 VRF bank 被独占
  └─ wvd_mask 不清零    → 后续指令 data_hazard = true (永久)
       ↓
后续任何向量指令（vredsum/vredor/vredxor...）
  ├─ 等 bank 释放       → 永久等待（bank_sel 不恢复）
  └─ data_hazard = true → 不发射
       ↓
系统死锁 → 看门狗超时 → tohost = 1337
```

---

## 影响范围

**不是 `DSPV512D128ShuttleConfig` 特有，而是整个 Saturn 的全局 bug。**

`PermuteUnitFactory`（包含 `vcompress`）通过 `integerFUs()` 被所有含整数运算的 issue structure 引用：

```scala
// Parameters.scala
def integerFUs(...) = integerALUs ++ Seq(
    IntegerDivideFactory(...),
    PermuteUnitFactory,    // ← 所有整数路径都携带 vcompress
)
```

| issStructure | 具有 vcompress | 受影响配置举例 |
|---|---|---|
| `Unified` | ✓ | `RocketVectorUnit` 类配置 |
| `Shared` | ✓ | `REFV512D128ShuttleConfig` |
| **`Split`** | ✓ | **`DSPV512D128ShuttleConfig`**（最易触发） |
| `MultiFMA` | ✓ | `GENV512D256MultiFMAShuttleConfig` |
| `MultiALU` | ✓ | 同类 |
| `dma` | ✗ | 仅内存拷贝，无运算，不受影响 |

**`Split` 配置最易触发**：Split 有两条独立 issue path（int + fp），两条 path 的 sequencer
同时竞争被全部锁住的 VRF bank，死锁概率更高、更稳定。

---

## 相关文件

| 文件 | 行号 | 内容 |
|------|------|------|
| `src/main/scala/backend/ExecuteSequencer.scala` | ~247 | `bank_sel := ~0`（锁全部 bank）|
| `src/main/scala/backend/ExecuteSequencer.scala` | ~338 | `!compress` guard（wvd_mask 不清零）|
| `src/main/scala/exu/PermuteUnit.scala` | 58–59 | `compress_eidx` prefix-sum 累加逻辑 |
| `src/main/scala/common/Parameters.scala` | ~146 | `PermuteUnitFactory` 注册位置 |

---

## 修复思路（供参考）

**方向 1**：在 `PermuteUnit` 执行完成后通知 sequencer 真实的写目标 eg，
延迟清零 `wvd_mask` 而不是彻底跳过。

**方向 2**：将 `vcompress` 实现为顺序依赖（sequentially dependent）指令，
禁止与后续指令重叠流水，执行完整条指令后统一释放 bank 和 wvd_mask。

**方向 3**：参考 `vrgather.vv` 的 `eidx_buffer` 路径，预计算目标地址后
再在正常路径流动，避免运行时才知道写地址的问题。

# LROB 和 SSB 事件说明文档

## 快速参考

### LROB (Load Reorder Buffer) 事件 - **仅用于 LOAD 指令**

#### 何时在 CSV 中出现（有值）？
- ✓ 指令是 LOAD 类型：`vl*`, `vlse`, `vluxei`, `vluxeix` 等
- ✓ Load 进入了 LROB 重排序阶段（因难序性需求）
- ✓ 同时观察到 PUSH（请求入队）和 DEQ（响应出队）事件

#### 何时在 CSV 中缺失（空单元格）？
- ✗ 指令是 STORE 类型（不是 LOAD）
- ✗ Load 被推测完成，绕过了 LROB
- ✗ Load 直接绕过重排序缓冲

#### 具体字段解释
| 字段 | 含义 | 何时记录 |
|------|------|---------|
| `lrob_push` | Load 条目写入 LROB 的周期 | Load 请求被接受进入 LROB |
| `lrob_deq` | Load 响应从 LROB 出队的周期 | Load 响应数据就绪并被播放 |

---

### SSB (Store Segment Buffer) 事件 - **仅用于 STORE 指令**

#### 何时在 CSV 中出现（有值）？
- ✓ 指令是 STORE 类型：`vs*`, `vsse`, `vsuxei`, `vsuxeix` 等
- ✓ Store 请求通过 SSB 管道路由（分段存储处理）
- ✓ 同时观察到 IN（入队）和 OUT（出队）事件

#### 何时在 CSV 中缺失（空单元格）？
- ✗ 指令是 LOAD 类型（不是 STORE）
- ✗ Store 绕过 SSB，直接写内存
- ✗ Store 未经过缓冲处理

#### 具体字段解释
| 字段 | 含义 | 何时记录 |
|------|------|---------|
| `ssb_in` | Store 分段进入 SSB 管道的周期 | Store 被 SSB 接受 |
| `ssb_out` | Store 分段离开 SSB 管道的周期 | Store 处理完成/转发出去 |

---

## 时间顺序保证

### Load 指令（LROB）
```
lrob_deq > lrob_push   ✓ 正确（响应在请求之后）
lrob_deq < lrob_push   ✗ 错误（违反因果关系）
```

### Store 指令（SSB）
```
ssb_out > ssb_in       ✓ 正确（出队在入队之后）
ssb_out < ssb_in       ✗ 错误（违反因果关系）
```

---

## Tag 复用问题说明

Saturn 内存访问使用 12-bit tags (0-11) 进行请求追踪。这些 tags 会循环复用：

- **Tag 8** 在 第1条指令 (dbg=159) 用于 PUSH@71921, DEQ@71925
- **Tag 8** 在 第2条指令 (dbg=162) 用于 PUSH@72085, DEQ@72087
- **Tag 8** 在 第3条指令 (dbg=169) 用于 PUSH@82890, DEQ@82895

**解决方案**：Parser 使用两步处理
1. **第一步**：建立 tag → (请求时间, 指令ID) 的时间表
2. **第二步**：处理事件时，验证 tag 确实属于该指令，且事件与请求时间接近
3. **过滤**：只从有配对 PUSH/DEQ 的 tags 中取最小值

---

## 实际示例

### 正确的 Load（dbg=162）
```
tag=9:  PUSH=72085, DEQ=72087  ✓ 配对且顺序正确
tag=10: PUSH=72086, DEQ=72091  ✓ 配对且顺序正确  
tag=11: PUSH=72087, DEQ=72095  ✓ 配对且顺序正确
tag=8:  PUSH=无,   DEQ=72084   ✗ 孤立（来自前一条指令）

最终结果：lrob_push = min(72085,72086,72087) = 72085
          lrob_deq = min(72087,72091,72095) = 72087 ✓ 正确
```

### Store 指令（dbg=159）
```
ssb_in = 71936   (Store 进入缓冲)
ssb_out = 71940  (Store 完成处理)

71940 > 71936  ✓ 正确顺序
```

---

## 输出文件说明

| 文件 | 内容 | 何时有 LROB/SSB |
|------|------|-----------------|
| `mem_access_micro_loads.csv` | 所有 LOAD 指令统计 | LOAD 指令的 LROB 字段 |
| `mem_access_micro_stores.csv` | 所有 STORE 指令统计 | STORE 指令的 SSB 字段 |
| `mem_access_micro_per_dbg_loads.csv` | Load 指令详细时间 | 同上 |
| `mem_access_micro_per_dbg_stores.csv` | Store 指令详细时间 | 同上 |
| `mem_access_micro_detailed_timelines.md` | 管道阶段时间 (Markdown) | 所有可用的管道事件 |



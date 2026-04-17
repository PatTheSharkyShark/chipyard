#include <stdio.h>
#include <stdint.h>

#define N         16
#define TCM_BASE  0x70000000U

static inline uint64_t read_cycles(void) {
    uint64_t c; 
    asm volatile("rdcycle %0" : "=r"(c)); 
    return c;
}

static uint32_t dram_buf[N * 2];
static const uint8_t cmask[4] = {0x55, 0x55, 0x55, 0x55};

void init_data(uint32_t *base) {
    for (int i = 0; i < N; i++) base[i] = i + 1;
}

/* 辅助：使用通用约束，编译器自动选择向量寄存器 */
#define VSET_E32_M1(vl, avl) \
    asm volatile("vsetvli %0, %1, e32, m1, ta, ma" : "=r"(vl) : "r"(avl))

/* ---- Phase 1: stable instructions ---- */
void bench_stable(const char *label, uint32_t *data) {
    uint32_t *out = data + N;
    size_t vl; 
    uint64_t s;
    int off4 = 4, off99 = 99, idx7 = 7;
    
    printf("\n=== %s STABLE ===\n", label);

    /* vslideup.vx - 使用临时变量让编译器分配寄存器 */
    s = read_cycles();
    VSET_E32_M1(vl, N);
    asm volatile(
        "vle32.v v8, (%0)\n\t"
        "vmv.v.i v16, 0\n\t"
        "vslideup.vx v16, v8, %1\n\t"
        "vse32.v v16, (%2)\n\t"
        "fence"
        :
        : "r"(data), "r"(off4), "r"(out)
        : "memory", "v8", "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23"
    );
    printf("vslideup.vx    offset=4:    %lu cyc\n", read_cycles()-s);

    /* vslidedown.vx */
    s = read_cycles();
    VSET_E32_M1(vl, N);
    asm volatile(
        "vle32.v v8, (%0)\n\t"
        "vslidedown.vx v16, v8, %1\n\t"
        "vse32.v v16, (%2)\n\t"
        "fence"
        :
        : "r"(data), "r"(off4), "r"(out)
        : "memory", "v8", "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23"
    );
    printf("vslidedown.vx  offset=4:    %lu cyc\n", read_cycles()-s);

    /* vslide1up.vx */
    s = read_cycles();
    VSET_E32_M1(vl, N);
    asm volatile(
        "vle32.v v8, (%0)\n\t"
        "vslide1up.vx v16, v8, %1\n\t"
        "vse32.v v16, (%2)\n\t"
        "fence"
        :
        : "r"(data), "r"(off99), "r"(out)
        : "memory", "v8", "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23"
    );
    printf("vslide1up.vx   xs1=99:      %lu cyc\n", read_cycles()-s);

    /* vslide1down.vx */
    s = read_cycles();
    VSET_E32_M1(vl, N);
    asm volatile(
        "vle32.v v8, (%0)\n\t"
        "vslide1down.vx v16, v8, %1\n\t"
        "vse32.v v16, (%2)\n\t"
        "fence"
        :
        : "r"(data), "r"(off99), "r"(out)
        : "memory", "v8", "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23"
    );
    printf("vslide1down.vx xs1=99:      %lu cyc\n", read_cycles()-s);

    /* vrgather.vx */
    s = read_cycles();
    VSET_E32_M1(vl, N);
    asm volatile(
        "vle32.v v8, (%0)\n\t"
        "vrgather.vx v16, v8, %1\n\t"
        "vse32.v v16, (%2)\n\t"
        "fence"
        :
        : "r"(data), "r"(idx7), "r"(out)
        : "memory", "v8", "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23"
    );
    printf("vrgather.vx    index=7:     %lu cyc\n", read_cycles()-s);
}

/* ---- Phase 2: risky instructions ---- */
void bench_risky(const char *label, uint32_t *data) {
    uint32_t *out = data + N;
    static uint32_t red_out[N];
    size_t vl; 
    uint64_t s;
    
    printf("\n=== %s RISKY ===\n", label);

    /* vcompress.vm */
    /* skipping because SATURN RTL deadlocks on this instruction */
    /*
    printf("[CHK] vcompress.vm ...\n");
    s = read_cycles();
    asm volatile("vsetivli %0, 16, e32, m1, tu, ma" : "=r"(vl));
    asm volatile(
        "vle32.v v8, (%0)\n\t"
        "vle32.v v16, (%2)\n\t"
        "vlm.v v0, (%1)\n\t"
        "vcompress.vm v16, v8, v0\n\t"
        "vse32.v v16, (%2)\n\t"
        "fence"
        :
        : "r"(data), "r"(cmask), "r"(out)
        : "memory", "v0", "v1", "v8", "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23"
    );
    printf("vcompress.vm   mask=0x55:   %lu cyc\n", read_cycles()-s);
    */

    /* vredsum.vs */
    printf("[CHK] vredsum.vs ...\n");
    s = read_cycles();
    VSET_E32_M1(vl, N);
    asm volatile(
        "vle32.v v24, (%0)\n\t"
        "vmv.v.i v25, 0\n\t"
        "vredsum.vs v25, v24, v25\n\t"
        "vse32.v v25, (%1)\n\t"
        "fence"
        :
        : "r"(data), "r"(red_out)
        : "memory", "v24", "v25", "v26", "v27", "v28", "v29", "v30", "v31"
    );
    printf("vredsum.vs     init=0:      %lu cyc\n", read_cycles()-s);

    /* vredor.vs */
    printf("[CHK] vredor.vs ...\n");
    s = read_cycles();
    VSET_E32_M1(vl, N);
    asm volatile(
        "vle32.v v24, (%0)\n\t"
        "vmv.v.i v25, 0\n\t"
        "vredor.vs v25, v24, v25\n\t"
        "vse32.v v25, (%1)\n\t"
        "fence"
        :
        : "r"(data), "r"(red_out)
        : "memory", "v24", "v25", "v26", "v27", "v28", "v29", "v30", "v31"
    );
    printf("vredor.vs      init=0:      %lu cyc\n", read_cycles()-s);

    /* vredxor.vs */
    printf("[CHK] vredxor.vs ...\n");
    s = read_cycles();
    VSET_E32_M1(vl, N);
    asm volatile(
        "vle32.v v24, (%0)\n\t"
        "vmv.v.i v25, 0\n\t"
        "vredxor.vs v25, v24, v25\n\t"
        "vse32.v v25, (%1)\n\t"
        "fence"
        :
        : "r"(data), "r"(red_out)
        : "memory", "v24", "v25", "v26", "v27", "v28", "v29", "v30", "v31"
    );
    printf("vredxor.vs     init=0:      %lu cyc\n", read_cycles()-s);

    /* vredmaxu.vs */
    printf("[CHK] vredmaxu.vs ...\n");
    s = read_cycles();
    VSET_E32_M1(vl, N);
    asm volatile(
        "vle32.v v24, (%0)\n\t"
        "vmv.v.i v25, 0\n\t"
        "vredmaxu.vs v25, v24, v25\n\t"
        "vse32.v v25, (%1)\n\t"
        "fence"
        :
        : "r"(data), "r"(red_out)
        : "memory", "v24", "v25", "v26", "v27", "v28", "v29", "v30", "v31"
    );
    printf("vredmaxu.vs    init=0:      %lu cyc\n", read_cycles()-s);

    /* chain 4 reductions together to see if hardware supports chaining */
    printf("[CHK] reduction chain: sum->or->xor->maxu ...\n");
    s = read_cycles();
    VSET_E32_M1(vl, N);
    asm volatile(
        "vle32.v v24, (%0)\n\t"
        "vmv.v.i v25, 0\n\t"
        "vredsum.vs v25, v24, v25\n\t"
        "vredor.vs v25, v24, v25\n\t"
        "vredxor.vs v25, v24, v25\n\t"
        "vredmaxu.vs v25, v24, v25\n\t"
        "vse32.v v25, (%1)\n\t"
        "fence"
        :
        : "r"(data), "r"(red_out)
        : "memory", "v24", "v25", "v26", "v27", "v28", "v29", "v30", "v31"
    );
    printf("chain total: %lu cyc\n", read_cycles()-s);
}


int main(void) {
    init_data(dram_buf);
    init_data((uint32_t *)TCM_BASE);

    bench_stable("DRAM", dram_buf);
    bench_stable("TCM",  (uint32_t *)TCM_BASE);
    bench_risky("DRAM", dram_buf);
    bench_risky("TCM",  (uint32_t *)TCM_BASE);
    return 0;
}
#include <stdio.h>
#include <stdint.h>

#define USE_LMUL4 0      /* set 1 to use m4 (64 elements per vector op) */
#define USE_LMUL_TINY 0  /* set 1 to use only 2 elements (single uop per instr) */

#if USE_LMUL4
#define N 64
#define VSET_E32_M(vl, avl) \
    asm volatile("vsetvli %0, %1, e32, m4, ta, ma" : "=r"(vl) : "r"(avl))
#define VA "v8"
#define VB "v12"
#define VT "v16"
#define VO "v20"
#define VR "v24"
#elif USE_LMUL_TINY
#define N 2  /* Single uop per instruction (DLEN=64bits, 2 e32 elements) */
#define VSET_E32_M(vl, avl) \
    asm volatile("vsetvli %0, %1, e32, m1, ta, ma" : "=r"(vl) : "r"(avl))
#define VA "v8"
#define VB "v9"
#define VT "v10"
#define VO "v16"
#define VR "v11"
#else
#define N 16
#define VSET_E32_M(vl, avl) \
    asm volatile("vsetvli %0, %1, e32, m1, ta, ma" : "=r"(vl) : "r"(avl))
#define VA "v8"
#define VB "v9"
#define VT "v10"
#define VO "v16"
#define VR "v11"
#endif
#define TCM_BASE  0x70000000U

static inline uint64_t read_cycles(void) {
    uint64_t c; 
    asm volatile("rdcycle %0" : "=r"(c)); 
    return c;
}

static uint32_t a[N];
static uint32_t b[N];
static uint32_t out[N];
static uint32_t tmp[N];

/* 关键修改：a 和 b 不同，产生混合 mask */
void init_data(void) {
    for (int i = 0; i < N; i++) {
        a[i] = i + 1;        // 1, 2, 3, ..., 16
        b[i] = (i + 1) * 2;  // 2, 4, 6, ..., 32 (故意不同)
    }
}

#define VSET_E32_M1 VSET_E32_M

int main(void) {
    size_t vl;
    uint64_t s;

    init_data();

    printf("\n=== MASK CHAINING TEST ===\n");
    printf("a:   "); for (int i = 0; i < N; i++) printf("%2u ", a[i]); printf("\n");
    printf("b:   "); for (int i = 0; i < N; i++) printf("%2u ", b[i]); printf("\n");

    /* Test 1: Masked add (chaining: vmsne -> vadd) */
    printf("\n--- Test 1: vmsne.vv -> vadd.vv (masked) ---\n");
    VSET_E32_M1(vl, N);
    /* preload operands into vector regs (setup, not timed) */
    asm volatile(
        "vle32.v " VA ", (%0)\n\t"    /* VA <- a */
        "vle32.v " VB ", (%1)\n\t"    /* VB <- b */
        : : "r"(a), "r"(b) : "memory", "v0", VA, VB
    );

    /* Timed section: only count mask generation and masked add */
    s = read_cycles();
    asm volatile(
        "vmsne.vv v0, " VA ", " VB "\n\t"    /* mask(a != b) */
        "vadd.vv " VO ", " VA ", " VB ", v0.t\n\t"  /* VO = a + b where mask */
        : : : "v0", VA, VB, VO, "memory"
    );
    uint64_t t1 = read_cycles() - s;

    /* store result (not counted) */
    asm volatile("vse32.v " VO ", (%0)\n\t" : : "r"(out) : "memory", VO);
    printf("cycles (vmsne,vadd): %lu\n", t1);
    printf("out: "); for (int i = 0; i < N; i++) printf("%2u ", out[i]); printf("\n");
    /* Expected: 3, 6, 9, ..., 48 (all elements active) */

    /* Test 2: 真正的混合 mask 测试 (部分激活) */
    printf("\n--- Test 2: Partial mask (even indices only) ---\n");
    
    /* 创建 b = a (让偶数位置相等，奇数位置不等) */
    for (int i = 0; i < N; i++) {
        b[i] = (i % 2 == 0) ? a[i] : a[i] + 100;  // 偶数同，奇数不同
    }
    printf("b:   "); for (int i = 0; i < N; i++) printf("%2u ", b[i]); printf("\n");

    VSET_E32_M1(vl, N);
    /* preload operands into vector regs (setup, not timed) */
    asm volatile(
        "vle32.v " VA ", (%0)\n\t"
        "vle32.v " VB ", (%1)\n\t"
        : : "r"(a), "r"(b) : "memory", "v0", VA, VB
    );

    /* Timed section: mask generation + masked add */
    s = read_cycles();
    asm volatile(
        "vmsne.vv v0, " VA ", " VB "\n\t"    /* mask=1 at odd indices */
        "vadd.vv " VO ", " VA ", " VB ", v0.t\n\t"  /* only at masked lanes */
        : : : "v0", VA, VB, VO, "memory"
    );
    uint64_t t2 = read_cycles() - s;

    /* store result (not counted) */
    asm volatile("vse32.v " VO ", (%0)\n\t" : : "r"(out) : "memory", VO);
    printf("cycles (vmsne,vadd): %lu\n", t2);
    printf("out: "); for (int i = 0; i < N; i++) printf("%2u ", out[i]); printf("\n");
    /* Expected: 
     *   even (i=0,2,4...): out[i] = unchanged (ta policy -> agnostic/0)
     *   odd  (i=1,3,5...): out[i] = a[i] + b[i] = (i+1) + (i+1+100) = 2i+102
     */

    /* Test 3: 多级 chaining (mask -> masked load -> masked add) */
    printf("\n--- Test 3: Multi-level chaining ---\n");
    
    /* 重新 init: a=1..16, b=101..116 (全不同) */
    for (int i = 0; i < N; i++) { a[i] = i+1; b[i] = i+101; }
    
    VSET_E32_M1(vl, N);
    /* preload operands into vector regs (setup, not timed) */
    asm volatile(
        "vle32.v " VA ", (%0)\n\t"           /* load a */
        "vle32.v " VB ", (%1)\n\t"           /* load b */
        : : "r"(a), "r"(b) : "memory", "v0", VA, VB
    );

    /* Timed section: mask generation + chained operations */
    s = read_cycles();
    asm volatile(
        "vmsgt.vx v0, " VA ", %0\n\t"        /* mask: a[i] > 8 */
        "vadd.vv " VO ", " VA ", " VB ", v0.t\n\t"  /* add only where a > 8 */
        "vsub.vv " VO ", " VO ", " VA ", v0.t\n\t" /* chained: sub only where mask still active */
        : : "r"(8) : "v0", VA, VB, VO, "memory"
    );
    uint64_t t3 = read_cycles() - s;

    /* store result (not counted) */
    asm volatile("vse32.v " VO ", (%0)\n\t" : : "r"(out) : "memory", VO);
    printf("cycles (vmsgt,vadd,vsub): %lu\n", t3);
    printf("out: "); for (int i = 0; i < N; i++) printf("%3u ", out[i]); printf("\n");
    /* Expected: i<8 (a<=8): unchanged; i>=8: (a+b)-a = b = i+101 */

    /* Test 4: mask depends on a previously computed vector (temp = a + b), then masked op uses that mask */
    printf("\n--- Test 4: mask depends on previous vector result (timed: vadd,vmsgt,vadd) ---\n");
    /* clear out to avoid residual values from prior tests */
    for (int i = 0; i < N; i++) out[i] = 0;

    /* prepare data: a=1..16, b=2..17 so temp = a+b ranges 3..33 */
    for (int i = 0; i < N; i++) { a[i] = i+1; b[i] = i+2; }
    const uint64_t TH = 20; /* threshold for mask */

    VSET_E32_M1(vl, N);
    /* preload operands into vector regs (setup, not timed) */
    asm volatile(
        "vle32.v " VA ", (%0)\n\t"    /* VA <- a */
        "vle32.v " VB ", (%1)\n\t"    /* VB <- b */
        : : "r"(a), "r"(b) : "memory", "v0", VA, VB
    );

    /* Timed section: only count the core sequence vadd (temp), vmsgt (mask), vadd (masked) */
    s = read_cycles();
    asm volatile(
        "vadd.vv " VT ", " VA ", " VB "\n\t"      /* temp = a + b */
        "vmsgt.vx v0, " VT ", %0\n\t"     /* mask = (temp > TH) */
        "vadd.vv " VO ", " VT ", " VB ", v0.t\n\t"/* masked add using temp */
        :
        : "r"(TH)
        : "v0", VA, VB, VT, VO, "memory"
    );
    uint64_t tdelta = read_cycles() - s;

    /* store result (not counted) */
    asm volatile("vse32.v " VO ", (%0)\n\t" : : "r"(out) : "memory", VO);

    printf("cycles (vadd,vmsgt,vadd): %lu\n", tdelta);
    printf("out: "); for (int i = 0; i < N; i++) printf("%3u ", out[i]); printf("\n");
    /* Expected: lanes where (a+b) > TH will be written (value = (a+b)+b), others remain 0 */

    /* Test 5 (control): force a memory round-trip so there is no forwarding/chaining opportunity */
    printf("\n--- Test 5: No-chaining control (store+reload, preload v11) (timed: vadd,vmsgt,vadd) ---\n");
    for (int i = 0; i < N; i++) out[i] = 0;

    /* prepare data: a=1..16, b=2..17 so temp = a+b ranges 3..33 */
    for (int i = 0; i < N; i++) { a[i] = i+1; b[i] = i+2; tmp[i] = 0; }
    const uint64_t TH2 = 20;

    VSET_E32_M1(vl, N);
    /* preload operands and perform store+reload BEFORE timed section (break forwarding) */
    asm volatile(
        "vle32.v " VA ", (%0)\n\t"    /* VA <- a */
        "vle32.v " VB ", (%1)\n\t"    /* VB <- b */
        "vadd.vv " VT ", " VA ", " VB "\n\t" /* VT = a + b */
        "vse32.v " VT ", (%2)\n\t" /* store temp to memory (break forwarding) */
        "vle32.v " VR ", (%2)\n\t" /* reload temp into VR */
        : : "r"(a), "r"(b), "r"(tmp) : "memory", VA, VB, VT, VR
    );

    /* Timed section: run the same 3-instruction sequence but using reloaded v11 to avoid chaining */
    s = read_cycles();
    asm volatile(
        "vadd.vv " VT ", " VR ", " VB "\n\t" /* VT = VR + VB (use reloaded temp) */
        "vmsgt.vx v0, " VT ", %0\n\t"    /* mask = (VT > TH2) */
        "vadd.vv " VO ", " VT ", " VB ", v0.t\n\t" /* masked add using VT */
        : : "r"(TH2) : "memory", "v0", VA, VB, VT, VR, VO
    );
    tdelta = read_cycles() - s;

    /* store result (not counted) */
    asm volatile("vse32.v " VO ", (%0)\n\t" : : "r"(out) : "memory", VO);

    printf("cycles (vadd,vmsgt,vadd): %lu\n", tdelta);
    printf("out: "); for (int i = 0; i < N; i++) printf("%3u ", out[i]); printf("\n");
    /* Expected: same functional result as Test 4 but any low-latency chaining forward should be prevented by the store+reload */

    return 0;
}
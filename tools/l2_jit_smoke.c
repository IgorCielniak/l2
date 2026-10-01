/*
 * Smoke test for the runtime Keystone-backed JIT.
 *
 * Compile:
 *   cc -O0 -g -I. tools/l2_jit_smoke.c -Lbuild -ll2 -Wl,-rpath,'$ORIGIN/../build' \
 *      -o build/l2_jit_smoke
 *
 * Run (uses the pip-bundled libkeystone.so if L2_KEYSTONE_PATH is set):
 *   L2_KEYSTONE_PATH=$(python3 -c "import keystone, os; print(os.path.join(os.path.dirname(keystone.__file__), 'libkeystone.so'))") \
 *     ./build/l2_jit_smoke
 */
#include <stdio.h>
#include <stdint.h>
#include "libs/l2.h"

// SysV C ABI function pointer type: takes (long, long), returns long.
typedef long (*add_fn_t)(long, long);

int main(void) {
    if (!l2_ks_available()) {
        fprintf(stderr, "[skip] keystone not available (set L2_KEYSTONE_PATH)\n");
        return 77;
    }

    // Trivial SysV asm: add rdi + rsi -> rax
    const char *src =
        "lea rax, [rdi + rsi]\n"
        "ret\n";

    void *fn = l2_jit_from_asm_cstr(src);
    if (!fn) {
        fprintf(stderr, "[fail] l2_jit_from_asm returned NULL\n");
        return 1;
    }

    add_fn_t add = (add_fn_t)fn;
    long r1 = add(3, 4);
    long r2 = add(100, 250);
    long r3 = add(-5, 5);

    printf("add(3, 4)     = %ld\n", r1);
    printf("add(100, 250) = %ld\n", r2);
    printf("add(-5, 5)    = %ld\n", r3);

    l2_jit_release(fn);

    // Assemble a second snippet: multiply rdi * rsi -> rax.
    void *mul_fn = l2_jit_from_asm_cstr(
        "mov rax, rdi\n"
        "imul rax, rsi\n"
        "ret\n"
    );
    if (!mul_fn) {
        fprintf(stderr, "[fail] mul JIT failed\n");
        return 1;
    }
    typedef long (*mul_fn_t)(long, long);
    mul_fn_t mul = (mul_fn_t)mul_fn;
    long r4 = mul(6, 7);
    printf("mul(6, 7)     = %ld\n", r4);
    l2_jit_release(mul_fn);

    return (r1 == 7 && r2 == 350 && r3 == 0 && r4 == 42) ? 0 : 1;
}

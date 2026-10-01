#include <stdio.h>
#include <stdint.h>
#include <sys/mman.h>
#include <string.h>
#include "libs/l2.h"

/* An 8-cell data stack for the toy test. */
static uint64_t stack[8];

int main(void) {
    /* Initialise stack with 42 at TOS. */
    uint64_t *tos = &stack[7];
    *tos = 42;

    void *fn = l2_compile_cstr("3 4 +");
    if (!fn) { fprintf(stderr, "l2_compile failed\n"); return 1; }

    /* Push initial r12 (tos) into the trampoline; result r12 will
     * point one cell lower with 7 there (since "3 4 +" pushes 7). */
    uint64_t new_r12 = l2_invoke_trampoline(fn, (uint64_t)tos);
    uint64_t *new_tos = (uint64_t *)new_r12;
    printf("orig tos value = %llu (was 42)\n",
           (unsigned long long)*(uint64_t *)tos);
    printf("new  tos value = %llu (expect 7)\n",
           (unsigned long long)*new_tos);
    printf("cells consumed = %lld\n",
           (long long)((int64_t)tos - (int64_t)new_tos) / 8);

    l2_release(fn);
    return (*new_tos == 7) ? 0 : 1;
}

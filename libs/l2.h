#ifndef L2_H
#define L2_H

#include <stdint.h>
#include <stddef.h>

/*
 * L2 runtime library — public C API.
 *
 * Formerly named "l2eval.h" back when the library only exposed the eval
 * helpers.  The library now also provides runtime compilation, a compile
 * event bus, and other runtime helpers.  A backward-compatible shim
 * "l2eval.h" still forwards to this header.
 */

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Run the existing L2 CLI driver from C.
 * Returns 0 on success, non-zero on failure.
 */
int l2_cli(int argc, char **argv);

typedef enum {
	L2_EVAL_STATUS_OK = 0,
	L2_EVAL_STATUS_ERROR = 1
} l2_eval_status_t;

/*
 * Evaluate source and return its integer result without narrowing.
 * On success, writes the top integer value to `out_result`, or 0 if the
 * source leaves no value. A non-integer result or evaluation failure returns
 * L2_EVAL_STATUS_ERROR. A valid negative integer, including -1, is success.
 */
l2_eval_status_t l2_eval_ex(const char *source, long source_len, int64_t *out_result);

/*
 * Legacy scalar API. Narrows the result to int and uses -1 for errors, so
 * some valid results are ambiguous; use l2_eval_ex for full-width results.
 * `source` points to UTF-8 bytes and `source_len` is the byte length.
 * Returns the top integer result produced by the evaluated source when one is
 * left on the compile-time stack.
 * Returns 0 when the source leaves no result.
 */
int l2_eval(const char *source, long source_len);

/*
 * Convenience wrapper for null-terminated C strings.
 */
int l2_eval_cstr(const char *source);

/*
 * Evaluate L2 source using the caller's data stack as input/output.
 * `stack_top_addr` must be the data stack pointer (r12) before pushing the
 * third argument (after the source addr + len are already on the stack).
 * The current data stack is imported as integers, the L2 source runs, and the
 * full resulting stack is written back to the runtime stack.
 * Values are exported as:
 *  - integers: one cell
 *  - strings/tokens: addr + len (two cells)
 */
void eval_env(const char *source, long source_len, long stack_top_addr);

/*
 * Compile a self-contained L2 snippet at runtime into an executable
 * function.  The returned pointer refers to native code that follows the
 * standard L2 calling convention:
 *   - r12 is the data-stack pointer (grows downward, 8-byte cells)
 *   - the function pops its inputs from r12 and pushes its outputs to r12
 *   - control returns via `ret`
 *
 * Use `l2_release()` to free the executable memory when the function is
 * no longer needed.  Returns NULL on failure.
 */
void *l2_compile(const char *source, long source_len);

/*
 * Convenience wrapper for null-terminated C strings.  See l2_compile.
 */
void *l2_compile_cstr(const char *source);

/*
 * Release memory returned by l2_compile.  Safe to call with NULL.
 */
void l2_release(void *fn_ptr);

/*
 * SysV bridge for invoking any L2 runtime-ABI trampoline (returned
 * by l2_compile or l2_jit_from_asm) from host code, without inline
 * asm on the host side.  Moves `r12_in` into the r12 register, calls
 * `fn`, and returns the resulting r12 value (the new data-stack top).
 *
 * Use this from Python/other bindings so a plain SysV call is enough
 * to invoke L2 native code that follows the r12-in/r12-out convention.
 */
uint64_t l2_invoke_trampoline(void *fn, uint64_t r12_in);

/*
 * Keystone-backed JIT (optional; loaded via dlopen at first use).
 *
 *   l2_ks_available()      returns 1 if libkeystone can be loaded and
 *                          the engine successfully opened; 0 otherwise.
 *                          Sets L2_KEYSTONE_PATH env to override the
 *                          loader search path.
 *
 *   l2_ks_assemble(src, out_buf, out_len)
 *                          Assembles the given x86-64 asm text into a
 *                          fresh malloc'd byte buffer.  Caller frees
 *                          with free().  Returns 0 on success, -1 on
 *                          failure (diagnostic printed to stderr).
 *
 *   l2_jit_from_asm(src)   Convenience: assemble + install into a
 *                          fresh RX page via W^X.  Returns a function
 *                          pointer callable directly (the asm is
 *                          responsible for the caller's ABI --
 *                          typically L2's r12 in/out for `call`).
 *
 *   l2_jit_release(fn_ptr) Frees a page allocated by l2_jit_from_asm.
 */
int l2_ks_available(void);
int l2_ks_assemble(const char *asm_src, uint8_t **out_buf, size_t *out_len);
void *l2_jit_from_asm(const char *asm_src, long src_len);
void *l2_jit_from_asm_cstr(const char *asm_src);
void l2_jit_release(void *fn_ptr);

#ifdef __cplusplus
}
#endif

#endif

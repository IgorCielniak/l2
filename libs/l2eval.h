#ifndef L2EVAL_H
#define L2EVAL_H

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Run the existing L2 CLI driver from C.
 * Returns 0 on success, non-zero on failure.
 */
int l2_cli(int argc, char **argv);

/*
 * Compile and run L2 source text at runtime.
 * `source` points to UTF-8 bytes and `source_len` is the byte length.
 * Returns the top integer result produced by the evaluated source when one is
 * left on the compile-time stack.
 * Returns 0 when the source leaves no result.
 * Returns -1 when compilation/setup fails.
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

#ifdef __cplusplus
}
#endif

#endif

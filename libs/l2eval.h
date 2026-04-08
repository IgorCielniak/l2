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
 * Returns the executed program's exit status on success.
 * Returns -1 when compilation/setup fails.
 */
int l2_eval(const char *source, long source_len);

/*
 * Convenience wrapper for null-terminated C strings.
 */
int l2_eval_cstr(const char *source);

#ifdef __cplusplus
}
#endif

#endif

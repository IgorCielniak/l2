#ifndef _GNU_SOURCE
#define _GNU_SOURCE  /* for dladdr / Dl_info (used by the JIT loader) */
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <ctype.h>
#include <stdarg.h>
#include <errno.h>
#include <limits.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/mman.h>
#include <dlfcn.h>
#include <dirent.h>
#include <fcntl.h>
#include <setjmp.h>

/* Errors in this file end the process with exit().  While a guard is set
 * (see l2_eval_seed_embedded_program), a failing exit() jumps back to it
 * instead, so a step that may fail does not take the host process down. */
static jmp_buf *g_l2_exit_guard = NULL;

static void l2_exit(int code) {
    if (code != 0 && g_l2_exit_guard) {
        jmp_buf *guard = g_l2_exit_guard;
        g_l2_exit_guard = NULL;
        longjmp(*guard, 1);
    }
    exit(code);
}
#define exit(code) l2_exit(code)

/* Nesting depth of eval runs (eval'd code can call host words that eval);
 * an error inside one names the flags that expose the host's words. */
static int g_l2_eval_depth = 0;

#define ARRAY_LEN(x) (sizeof(x) / sizeof((x)[0]))

int l2_cli(int argc, char **argv);
void eval_env(const char *source, long source_len, long stack_top_addr);
int eval_program(const char *source, long source_len);
int eval_program_cstr(const char *source);
const char *l2_source_catalog_lookup(const char *name, long name_len, long *out_len);
void l2_set_embedded_source(const char *source, long source_len);
void l2_set_eval_stack_top(uintptr_t stack_top_addr);
typedef enum {
    L2_EVAL_STATUS_OK = 0,
    L2_EVAL_STATUS_ERROR = 1
} l2_eval_status_t;
l2_eval_status_t l2_eval_ex(const char *source, long source_len, int64_t *out_result);
// Legacy aliases for backward compatibility
int l2_eval(const char *source, long source_len);
int l2_eval_cstr(const char *source);
int l2_eval_program(const char *source, long source_len);
int l2_eval_inline(const char *source, long source_len);
int l2_eval_inline_cstr(const char *source);
void eval(const char *source, long source_len);
int eval_cstr(const char *source);
int eval_scalar(const char *source, long source_len);

// Runtime compilation returns an executable L2-ABI trampoline; release it with
// l2_release. Snippets use the C CT VM fallback when native JIT is unavailable.

void *l2_compile(const char *source, long source_len);
void *l2_compile_cstr(const char *source);
void l2_release(void *fn_ptr);
uint64_t l2_invoke_trampoline(void *fn, uint64_t r12_in);

// Keystone is loaded lazily. JIT code uses L2's r12 stack ABI and is released
// with l2_jit_release; set L2_KEYSTONE_PATH to select a library explicitly.
int l2_ks_available(void);
int l2_ks_assemble(const char *asm_src, uint8_t **out_buf, size_t *out_len);
/* Like l2_ks_assemble, without the error message (for attempts that have a
 * fallback). */
static bool g_ks_quiet = false;
static int l2_ks_assemble_quiet(const char *asm_src, uint8_t **out_buf, size_t *out_len) {
    g_ks_quiet = true;
    int rc = l2_ks_assemble(asm_src, out_buf, out_len);
    g_ks_quiet = false;
    return rc;
}
static void *l2_jit_install(const uint8_t *bytes, size_t len);
void *l2_jit_from_asm(const char *asm_src, long src_len);
void *l2_jit_from_asm_cstr(const char *asm_src);
void l2_jit_release(void *fn_ptr);



static void *xmalloc(size_t size) {
    void *ptr = malloc(size);
    if (!ptr) {
        fprintf(stderr, "[error] out of memory\n");
        exit(1);
    }
    return ptr;
}




static void *xrealloc(void *ptr, size_t size) {
    void *out = realloc(ptr, size);
    if (!out) {
        fprintf(stderr, "[error] out of memory\n");
        exit(1);
    }
    return out;
}

static char *str_dup(const char *src) {
    if (!src) {
        return NULL;
    }
    size_t len = strlen(src);
    char *out = (char *)xmalloc(len + 1);
    memcpy(out, src, len + 1);
    return out;
}

static char *str_printf(const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    va_list args2;
    va_copy(args2, args);
    int needed = vsnprintf(NULL, 0, fmt, args2);
    va_end(args2);
    if (needed < 0) {
        va_end(args);
        return str_dup("");
    }
    char *buf = (char *)xmalloc((size_t)needed + 1);
    vsnprintf(buf, (size_t)needed + 1, fmt, args);
    va_end(args);
    return buf;
}

static bool str_starts_with(const char *text, const char *prefix) {
    if (!text || !prefix) {
        return false;
    }
    size_t len = strlen(prefix);
    return strncmp(text, prefix, len) == 0;
}

static bool str_equals(const char *a, const char *b) {
    if (!a || !b) {
        return false;
    }
    return strcmp(a, b) == 0;
}

static uint64_t hash_str(const char *text) {
    uint64_t hash = 1469598103934665603ULL;
    while (*text) {
        hash ^= (unsigned char)(*text++);
        hash *= 1099511628211ULL;
    }
    return hash;
}

#define VEC_DECL(name, type) \
    typedef struct { \
        type *data; \
        size_t len; \
        size_t cap; \
    } name

#define VEC_INIT(vec) do { (vec)->data = NULL; (vec)->len = 0; (vec)->cap = 0; } while (0)

#define VEC_FREE(vec) do { free((vec)->data); (vec)->data = NULL; (vec)->len = 0; (vec)->cap = 0; } while (0)

#define VEC_PUSH(vec, value) do { \
    if ((vec)->len + 1 > (vec)->cap) { \
        (vec)->cap = (vec)->cap ? (vec)->cap * 2 : 8; \
        (vec)->data = xrealloc((vec)->data, (vec)->cap * sizeof(*(vec)->data)); \
    } \
    (vec)->data[(vec)->len++] = (value); \
} while (0)

#define VEC_POP(vec) ((vec)->len ? (vec)->data[--(vec)->len] : (vec)->data[0])

VEC_DECL(StrVec, char *);
VEC_DECL(IntVec, int);

/* Concatenate strings in linear time.  NULL items are skipped; `sep` (may be
 * NULL) is written between consecutive items and `suffix` (may be NULL)
 * after every item.  Repeated strcat() would rescan the growing buffer on
 * every append, which is quadratic in the output size. */
static char *str_join_parts(char *const *items, size_t count, const char *sep, const char *suffix) {
    size_t sep_len = sep ? strlen(sep) : 0;
    size_t suffix_len = suffix ? strlen(suffix) : 0;
    size_t total = 0;
    size_t present = 0;
    for (size_t i = 0; i < count; i++) {
        if (!items[i]) {
            continue;
        }
        total += strlen(items[i]) + suffix_len;
        present++;
    }
    if (present > 1) {
        total += sep_len * (present - 1);
    }
    char *out = (char *)xmalloc(total + 1);
    char *cursor = out;
    bool first = true;
    for (size_t i = 0; i < count; i++) {
        if (!items[i]) {
            continue;
        }
        if (!first && sep_len) {
            memcpy(cursor, sep, sep_len);
            cursor += sep_len;
        }
        first = false;
        size_t len = strlen(items[i]);
        memcpy(cursor, items[i], len);
        cursor += len;
        if (suffix_len) {
            memcpy(cursor, suffix, suffix_len);
            cursor += suffix_len;
        }
    }
    *cursor = '\0';
    return out;
}

static bool strvec_contains(StrVec *vec, const char *value) {
    if (!vec || !value) {
        return false;
    }
    for (size_t i = 0; i < vec->len; i++) {
        if (strcmp(vec->data[i], value) == 0) {
            return true;
        }
    }
    return false;
}

typedef struct {
    char *lexeme;
    int line;
    int column;
    int start;
    int end;
} Token;

VEC_DECL(TokenVec, Token);

typedef struct {
    char *path;
    int line;
    int column;
} SourceLocation;

typedef struct {
    char *path;
    int start_line;
    int end_line;
    int local_start_line;
} FileSpan;

VEC_DECL(FileSpanVec, FileSpan);

typedef enum {
    OP_LITERAL,
    OP_WORD,
    OP_BRANCH_ZERO,
    OP_JUMP,
    OP_LABEL,
    OP_FOR_BEGIN,
    OP_FOR_END,
    OP_LIST_BEGIN,
    OP_LIST_END,
    OP_RET,
    OP_SYNTAX,   /* push a quotation's Syntax value (data.ptr: QuoteTemplate) */
    OP_BINDING,  /* push the value bound to a macro parameter (data.word) */
    OP_WORD_PTR  /* push the address of a word (`&name`; data.word) */
} OpKind;

/* A parsed quotation: literal tokens, `$name` splices, and `$( )` unquotes
 * (whose values the code before the quotation op left on the VM's unquote
 * stack). */
typedef struct {
    int kind;        /* 0 token, 1 splice, 2 unquote */
    char *lexeme;    /* token text, or the splice name */
    char *prefix;    /* unquote: text pasted before / after its value */
    char *suffix;
    int line;
    int column;
} QuotePart;

typedef struct {
    QuotePart *parts;
    size_t len;
    size_t unquotes;
} QuoteTemplate;

typedef enum {
    LIT_INT,
    LIT_FLOAT,
    LIT_STRING
} LiteralKind;

typedef struct {
    OpKind kind;
    LiteralKind lit_kind;
    SourceLocation *loc;
    union {
        int64_t i64;
        double f64;
        char *str;
        char *word;
        char *label;
        struct {
            char *loop;
            char *end;
        } loop;
        void *ptr;
    } data;
} Op;

VEC_DECL(OpVec, Op);

typedef struct {
    char *name;
    OpVec body;
    bool immediate;
    bool compile_only;
    bool runtime_only;
    char *terminator;
    bool inline_def;
    bool preserve_def;
} Definition;

typedef struct {
    char *name;
    char *body;
    bool immediate;
    bool compile_only;
    bool runtime_only;
    bool effect_string_io;
    bool inline_def;
    bool preserve_def;
} AsmDefinition;

typedef enum {
    FORM_DEF,
    FORM_ASM
} FormKind;

typedef struct {
    FormKind kind;
    void *ptr;
} Form;

VEC_DECL(FormVec, Form);

typedef struct {
    StrVec text;
    StrVec data;
    StrVec bss;
} Emission;

typedef struct {
    StrVec *text;
    bool debug_enabled;
    SourceLocation *current_loc;
} FunctionEmitter;

typedef struct Word Word;
typedef struct CompileTimeVM CompileTimeVM;
typedef struct Parser Parser;

/* forward-declare ct intrinsic used in bootstrap */
static void ct_intrinsic_eval_program(CompileTimeVM *vm);


typedef void (*MacroFn)(Parser *parser);
typedef void (*IntrinsicEmitter)(FunctionEmitter *builder);
typedef void (*CompileTimeIntrinsic)(CompileTimeVM *vm);

struct Word {
    char *name;
    bool immediate;
    bool compile_only;
    bool runtime_only;
    bool compile_time_override;
    bool is_extern;
    int extern_inputs;
    int extern_outputs;
    char **extern_arg_types;
    int extern_arg_count;
    char *extern_ret_type;
    bool inline_def;
    Definition *definition;
    Definition *ct_definition;
    Definition *prev_definition;
    AsmDefinition *asm_def;
    AsmDefinition *ct_asm_def;
    AsmDefinition *prev_asm_def;
    MacroFn macro;
    IntrinsicEmitter intrinsic;
    CompileTimeIntrinsic ct_intrinsic;
    char **macro_expansion;
    int macro_param_count;
    /* A general macro: its compile-time body and parameter names. */
    Definition *macro_body;
    char **macro_params;
    /* Set for words eval read from the host program's embedded source: the
     * definition it read, so a later redefinition by eval'd code is not
     * mistaken for the host's word (see ct_word_call_native). */
    bool host_seeded;
    Definition *host_definition;
    /* Set by ct-control-register-override: the parser runs this (immediate)
     * word for its name instead of the built-in control structure. */
    bool control_override;
};

typedef struct {
    char **keys;
    void **values;
    size_t cap;
    size_t len;
} StrMap;

static void strmap_init(StrMap *map) {
    map->keys = NULL;
    map->values = NULL;
    map->cap = 0;
    map->len = 0;
}

static void strmap_free(StrMap *map) {
    free(map->keys);
    free(map->values);
    map->keys = NULL;
    map->values = NULL;
    map->cap = 0;
    map->len = 0;
}

static void strmap_grow(StrMap *map) {
    size_t new_cap = map->cap ? map->cap * 2 : 128;
    char **new_keys = (char **)xmalloc(new_cap * sizeof(char *));
    void **new_vals = (void **)xmalloc(new_cap * sizeof(void *));
    for (size_t i = 0; i < new_cap; i++) {
        new_keys[i] = NULL;
        new_vals[i] = NULL;
    }
    if (map->keys) {
        for (size_t i = 0; i < map->cap; i++) {
            if (!map->keys[i]) {
                continue;
            }
            uint64_t hash = hash_str(map->keys[i]);
            size_t idx = (size_t)(hash & (new_cap - 1));
            while (new_keys[idx]) {
                idx = (idx + 1) & (new_cap - 1);
            }
            new_keys[idx] = map->keys[i];
            new_vals[idx] = map->values[i];
        }
    }
    free(map->keys);
    free(map->values);
    map->keys = new_keys;
    map->values = new_vals;
    map->cap = new_cap;
}

static void strmap_set(StrMap *map, const char *key, void *value) {
    if (!map->cap || (map->len + 1) * 3 >= map->cap * 2) {
        strmap_grow(map);
    }
    uint64_t hash = hash_str(key);
    size_t idx = (size_t)(hash & (map->cap - 1));
    while (map->keys[idx]) {
        if (strcmp(map->keys[idx], key) == 0) {
            map->values[idx] = value;
            return;
        }
        idx = (idx + 1) & (map->cap - 1);
    }
    map->keys[idx] = str_dup(key);
    map->values[idx] = value;
    map->len++;
}

static void *strmap_get(StrMap *map, const char *key) {
    if (!map->cap) {
        return NULL;
    }
    uint64_t hash = hash_str(key);
    size_t idx = (size_t)(hash & (map->cap - 1));
    size_t start = idx;
    while (map->keys[idx]) {
        if (strcmp(map->keys[idx], key) == 0) {
            return map->values[idx];
        }
        idx = (idx + 1) & (map->cap - 1);
        if (idx == start) {
            break;
        }
    }
    return NULL;
}

static bool strmap_has(StrMap *map, const char *key) {
    return strmap_get(map, key) != NULL;
}

typedef struct {
    StrMap words;
} Dictionary;

static void dictionary_init(Dictionary *dict) {
    strmap_init(&dict->words);
}

static Word *dictionary_lookup(Dictionary *dict, const char *name) {
    return (Word *)strmap_get(&dict->words, name);
}

static void dictionary_register(Dictionary *dict, Word *word) {
    strmap_set(&dict->words, word->name, word);
}

typedef struct {
    StrVec custom_tokens;
    StrVec token_order;
} Reader;

static void reader_init(Reader *reader) {
    VEC_INIT(&reader->custom_tokens);
    VEC_INIT(&reader->token_order);
    const char *defaults[] = {"(", ")", "{", "}", ";", ",", "[", "]"};
    for (size_t i = 0; i < ARRAY_LEN(defaults); i++) {
        VEC_PUSH(&reader->custom_tokens, str_dup(defaults[i]));
    }
    for (size_t i = 0; i < reader->custom_tokens.len; i++) {
        VEC_PUSH(&reader->token_order, reader->custom_tokens.data[i]);
    }
}

typedef struct {
    const char *source;
    size_t length;
    size_t index;
    int line;
    int column;
    Reader *reader;
    /* Raw `:asm` bodies, as in the Python reader: `:asm NAME [( .. )] {`
     * outside a quotation makes the body one raw token; inside a quotation the
     * body stays tokenized, but a `;` comment is one token to the line end. */
    int asm_state;
    bool in_quotation;
    bool quoted_asm;
    bool raw_pending;
} Tokenizer;

static void tokenizer_init(Tokenizer *tokenizer, Reader *reader, const char *source) {
    tokenizer->source = source;
    tokenizer->length = strlen(source);
    tokenizer->index = 0;
    tokenizer->line = 1;
    tokenizer->column = 0;
    tokenizer->reader = reader;
    tokenizer->asm_state = 0;
    tokenizer->in_quotation = false;
    tokenizer->quoted_asm = false;
    tokenizer->raw_pending = false;
}

static bool tokenizer_next_basic(Tokenizer *tokenizer, Token *out);

/* Index of the `}` closing a raw :asm body that starts at `pos`, or -1:
 * `;` comments run to the line end and quotes to their match (or the line
 * end), so neither can close the body. */
static long asm_raw_body_end(const char *src, size_t len, size_t pos) {
    size_t i = pos;
    while (i < len) {
        char ch = src[i];
        if (ch == '}') return (long)i;
        if (ch == ';') {
            while (i < len && src[i] != '\n') i++;
            continue;
        }
        if (ch == '\'' || ch == '"' || ch == '`') {
            size_t j = i + 1;
            while (j < len && src[j] != '\n' && src[j] != ch) j++;
            i = (j < len && src[j] == ch) ? j + 1 : j;
            continue;
        }
        i++;
    }
    return -1;
}

static void tokenizer_advance_to(Tokenizer *tokenizer, size_t target) {
    while (tokenizer->index < target) {
        if (tokenizer->source[tokenizer->index] == '\n') {
            tokenizer->line++;
            tokenizer->column = 0;
        } else {
            tokenizer->column++;
        }
        tokenizer->index++;
    }
}

static bool tokenizer_take_span(Tokenizer *tokenizer, size_t start, size_t end, Token *out) {
    tokenizer_advance_to(tokenizer, start);
    out->line = tokenizer->line;
    out->column = tokenizer->column;
    out->start = (int)start;
    out->end = (int)end;
    out->lexeme = (char *)xmalloc(end - start + 1);
    memcpy(out->lexeme, tokenizer->source + start, end - start);
    out->lexeme[end - start] = '\0';
    tokenizer_advance_to(tokenizer, end);
    return true;
}

/* Feed one lexeme to the `:asm NAME [( .. )] {` recognizer. */
static bool tokenizer_asm_header(Tokenizer *tokenizer, const char *lex) {
    switch (tokenizer->asm_state) {
    case 0:
        if (strcmp(lex, ":asm") == 0) tokenizer->asm_state = 1;
        return false;
    case 1:
        tokenizer->asm_state = 2;
        return false;
    case 2:
        if (strcmp(lex, "(") == 0) {
            tokenizer->asm_state = 3;
            return false;
        }
        tokenizer->asm_state = strcmp(lex, ":asm") == 0 ? 1 : 0;
        return strcmp(lex, "{") == 0;
    default:
        if (strcmp(lex, ")") == 0) tokenizer->asm_state = 2;
        return false;
    }
}

static bool tokenizer_next(Tokenizer *tokenizer, Token *out) {
    const char *src = tokenizer->source;
    size_t len = tokenizer->length;
    if (tokenizer->raw_pending) {
        tokenizer->raw_pending = false;
        long close = asm_raw_body_end(src, len, tokenizer->index);
        if (close >= 0) {
            size_t body = tokenizer->index;
            while (body < (size_t)close && isspace((unsigned char)src[body])) body++;
            if (body < (size_t)close) {
                return tokenizer_take_span(tokenizer, body, (size_t)close, out);
            }
            tokenizer_advance_to(tokenizer, (size_t)close);
        }
    }
    if (tokenizer->quoted_asm) {
        size_t i = tokenizer->index;
        while (i < len && isspace((unsigned char)src[i])) i++;
        if (i < len && src[i] == ';') {
            size_t end = i;
            while (end < len && src[end] != '\n') end++;
            return tokenizer_take_span(tokenizer, i, end, out);
        }
    }
    if (!tokenizer_next_basic(tokenizer, out)) {
        return false;
    }
    const char *lex = out->lexeme;
    if (strcmp(lex, "`") == 0) {
        tokenizer->in_quotation = !tokenizer->in_quotation;
        tokenizer->quoted_asm = false;
    } else if (tokenizer->quoted_asm) {
        if (strcmp(lex, "}") == 0) tokenizer->quoted_asm = false;
    } else if (tokenizer_asm_header(tokenizer, lex)) {
        if (tokenizer->in_quotation) {
            tokenizer->quoted_asm = true;
        } else {
            tokenizer->raw_pending = true;
        }
    }
    return true;
}

static bool tokenizer_next_basic(Tokenizer *tokenizer, Token *out) {
    const char *src = tokenizer->source;
    size_t len = tokenizer->length;
    size_t idx = tokenizer->index;
    int line = tokenizer->line;
    int col = tokenizer->column;

    while (idx < len) {
        char ch = src[idx];
        if (ch == '"') {
            size_t start = idx;
            int token_line = line;
            int token_col = col;
            idx++;
            col++;
            bool escape = false;
            while (idx < len) {
                char c = src[idx++];
                if (c == '\n') {
                    line++;
                    col = 0;
                } else {
                    col++;
                }
                if (escape) {
                    escape = false;
                    continue;
                }
                if (c == '\\') {
                    escape = true;
                    continue;
                }
                if (c == '"') {
                    size_t end = idx;
                    size_t tok_len = end - start;
                    char *lex = (char *)xmalloc(tok_len + 1);
                    memcpy(lex, src + start, tok_len);
                    lex[tok_len] = '\0';
                    out->lexeme = lex;
                    out->line = token_line;
                    out->column = token_col;
                    out->start = (int)start;
                    out->end = (int)end;
                    tokenizer->index = idx;
                    tokenizer->line = line;
                    tokenizer->column = col;
                    return true;
                }
            }
            fprintf(stderr, "[error] unterminated string literal\n");
            exit(1);
        }
        if (ch == '#') {
            while (idx < len && src[idx] != '\n' && src[idx] != '\r') {
                idx++;
            }
            continue;
        }
        /* Fixed backtick rule: '`' and the escaped backtick '\`' are always
         * tokens of their own and end a word in progress. */
        if (ch == '`' || (ch == '\\' && idx + 1 < len && src[idx + 1] == '`')) {
            size_t tok_len = ch == '`' ? 1 : 2;
            char *lex = (char *)xmalloc(tok_len + 1);
            memcpy(lex, src + idx, tok_len);
            lex[tok_len] = '\0';
            out->lexeme = lex;
            out->line = line;
            out->column = col;
            out->start = (int)idx;
            out->end = (int)(idx + tok_len);
            tokenizer->index = idx + tok_len;
            tokenizer->line = line;
            tokenizer->column = col + (int)tok_len;
            return true;
        }

        bool matched = false;
        const char *matched_tok = NULL;
        for (size_t i = 0; i < tokenizer->reader->token_order.len; i++) {
            const char *tok = tokenizer->reader->token_order.data[i];
            size_t tok_len = strlen(tok);
            if (tok_len == 0) {
                continue;
            }
            if (idx + tok_len <= len && strncmp(src + idx, tok, tok_len) == 0) {
                matched = true;
                matched_tok = tok;
                size_t start = idx;
                int token_line = line;
                int token_col = col;
                idx += tok_len;
                col += (int)tok_len;
                out->lexeme = str_dup(matched_tok);
                out->line = token_line;
                out->column = token_col;
                out->start = (int)start;
                out->end = (int)idx;
                tokenizer->index = idx;
                tokenizer->line = line;
                tokenizer->column = col;
                return true;
            }
        }
        if (matched) {
            continue;
        }
        if (isspace((unsigned char)ch)) {
            if (ch == '\n') {
                line++;
                col = 0;
            } else {
                col++;
            }
            idx++;
            continue;
        }
        size_t start = idx;
        int token_line = line;
        int token_col = col;
        while (idx < len) {
            char c = src[idx];
            bool is_sep = isspace((unsigned char)c) || c == '"' || c == '#' || c == '`' ||
                          (c == '\\' && idx + 1 < len && src[idx + 1] == '`');
            if (is_sep) {
                break;
            }
            bool token_hit = false;
            for (size_t i = 0; i < tokenizer->reader->token_order.len; i++) {
                const char *tok = tokenizer->reader->token_order.data[i];
                size_t tok_len = strlen(tok);
                if (tok_len && idx + tok_len <= len && strncmp(src + idx, tok, tok_len) == 0) {
                    token_hit = true;
                    break;
                }
            }
            if (token_hit) {
                break;
            }
            idx++;
            col++;
        }
        size_t tok_len = idx - start;
        if (tok_len) {
            char *lex = (char *)xmalloc(tok_len + 1);
            memcpy(lex, src + start, tok_len);
            lex[tok_len] = '\0';
            out->lexeme = lex;
            out->line = token_line;
            out->column = token_col;
            out->start = (int)start;
            out->end = (int)idx;
            tokenizer->index = idx;
            tokenizer->line = line;
            tokenizer->column = col;
            return true;
        }
        idx++;
        col++;
    }

    tokenizer->index = idx;
    tokenizer->line = line;
    tokenizer->column = col;
    return false;
}

struct Parser {
    Dictionary *dictionary;
    Reader *reader;
    TokenVec tokens;
    size_t pos;
    Tokenizer tokenizer;
    bool tokenizer_exhausted;
    struct {
        FormVec forms;
        StrMap variables;
        StrVec *prelude;
        StrVec *bss;
    } module;
    Definition *current_def;
    Word **definition_stack;
    size_t definition_stack_len;
    size_t definition_stack_cap;
    Word *last_defined;
    FileSpanVec file_spans;
    char *source;
    struct {
        char *name;
        StrVec tokens;
        int param_count;
        bool active;
    } macro_recording;
    struct {
        bool active;
        char *name;
        char **params;
        int param_count;
        size_t control_depth;
        Definition *def;
    } macro_body;
    /* Quotations waiting for their `$( )` expressions to be parsed. */
    QuoteTemplate **pending_quotes;
    size_t pending_quote_len;
    struct {
        char *type;
        char *false_label;
        char *end_label;
        char *begin_label;
        char *loop_label;
        int line;
        int column;
        StrVec with_names;
        /* A frame pushed by ct-control-push (NULL for built-in frames): a
         * map whose "close_ops" list `end` emits. */
        struct CtMap *frame;
    } *control_stack;
    size_t control_len;
    size_t control_cap;
    int label_counter;
    int gensym_counter;
    char *token_hook;
    Token last_token;
    bool has_last_token;
    StrMap variable_labels;
    StrMap variable_words;
    CompileTimeVM *ct_vm;
    /* Assembly output buffers: CT .text and .data fragments, and
     * the runtime BSS buffer, which holds the standard layout until it is
     * replaced or cleared (bss_replaced). */
    StrVec *custom_prelude;
    StrVec *custom_data;
    StrVec *custom_bss;
    bool bss_replaced;
    bool pending_inline_def;
    bool pending_preserve_def;
    bool uses_libc;
    bool uses_libm;
    char *primary_path;
};

/* forward declarations for helpers used by CT eval intrinsic */
static char *path_dirname(const char *path);
static char *l2_default_root_dir(void);
static char *expand_imports_from_source(const char *content, const char *base_dir, StrVec *include_dirs, StrMap *visited, FileSpanVec *spans, int *line_counter);
static void parse_tokens(Parser *parser, const char *source);
static void parse_tokens_continue(Parser *parser, const char *source);

typedef enum {
    CT_NIL,
    CT_INT,
    CT_STR,
    CT_TOKEN,
    CT_LIST,
    CT_MAP,
    CT_SYNTAX   /* a quotation value: as.list holds CT_TOKEN items */
} CtValueKind;

typedef struct CtValue CtValue;

VEC_DECL(CtValueVec, CtValue);

typedef struct {
    CtValueVec items;
} CtList;

/* CT map: insertion-ordered entries with typed keys
 * (string, integer, or nil), plus an index from an encoded key to the entry
 * position.  Updating an existing key keeps its stored key and position. */
typedef struct CtMap {
    CtValue *keys;
    CtValue *values;
    size_t cap;
    size_t len;
    StrMap index;
} CtMap;

struct CtValue {
    CtValueKind kind;
    union {
        int64_t i64;
        char *str;
        Token token;
        CtList *list;
        CtMap *map;
    } as;
};

struct CompileTimeVM {
    Parser *parser;
    Dictionary *dictionary;
    CtValueVec stack;
    CtValueVec rstack;
    IntVec loop_remaining;
    IntVec loop_begin;
    IntVec loop_initial;
    StrVec call_stack;
    /* Macro expansion: the running body's parameter bindings and output. */
    char **binding_names;
    CtValue *binding_values;
    size_t binding_len;
    TokenVec *emit_out;
    /* `$( )` values awaiting their quotation, and expression stack depths. */
    CtValueVec unquote_values;
    IntVec unquote_depths;
};

static CtValue ct_make_nil(void) {
    CtValue v = {0};
    v.kind = CT_NIL;
    return v;
}

static CtValue ct_make_int(int64_t i) {
    CtValue v = {0};
    v.kind = CT_INT;
    v.as.i64 = i;
    return v;
}

static CtValue ct_make_str(const char *s) {
    CtValue v = {0};
    v.kind = CT_STR;
    v.as.str = str_dup(s);
    return v;
}

static CtValue ct_make_token(Token token) {
    CtValue v = {0};
    v.kind = CT_TOKEN;
    v.as.token = token;
    return v;
}

static CtValue ct_make_list(CtList *list) {
    CtValue v = {0};
    v.kind = CT_LIST;
    v.as.list = list;
    return v;
}

static CtValue ct_make_map(CtMap *map) {
    CtValue v = {0};
    v.kind = CT_MAP;
    v.as.map = map;
    return v;
}


static void ct_stack_init(CtValueVec *vec) {
    VEC_INIT(vec);
}

static void ct_stack_push(CtValueVec *vec, CtValue value) {
    VEC_PUSH(vec, value);
}

static CtValue ct_stack_pop(CtValueVec *vec) {
    if (!vec->len) {
        CtValue v = ct_make_nil();
        return v;
    }
    return VEC_POP(vec);
}

static CtValue ct_stack_peek(CtValueVec *vec) {
    if (!vec->len) {
        CtValue v = ct_make_nil();
        return v;
    }
    return vec->data[vec->len - 1];
}

static bool try_parse_int(const char *lexeme, int64_t *out);

static int64_t ct_value_to_int(CtValue v) {
    if (v.kind == CT_INT) {
        return v.as.i64;
    }
    if (v.kind == CT_STR && v.as.str) {
        int64_t parsed = 0;
        if (try_parse_int(v.as.str, &parsed)) {
            return parsed;
        }
    }
    if (v.kind == CT_TOKEN && v.as.token.lexeme) {
        int64_t parsed = 0;
        if (try_parse_int(v.as.token.lexeme, &parsed)) {
            return parsed;
        }
    }
    return 0;
}

static CtList *ct_list_new(void) {
    CtList *list = (CtList *)xmalloc(sizeof(CtList));
    VEC_INIT(&list->items);
    return list;
}

static CtMap *ct_map_new(void) {
    CtMap *map = (CtMap *)xmalloc(sizeof(CtMap));
    map->keys = NULL;
    map->values = NULL;
    map->cap = 0;
    map->len = 0;
    strmap_init(&map->index);
    return map;
}

/* Encode a supported map key; returns NULL for unsupported key kinds. */
static char *ct_map_encode_key(CtValue key) {
    switch (key.kind) {
        case CT_STR:
            return str_printf("s%s", key.as.str ? key.as.str : "");
        case CT_INT:
            return str_printf("i%lld", (long long)key.as.i64);
        case CT_NIL:
            return str_dup("n");
        default:
            return NULL;
    }
}

static long ct_map_find(CtMap *map, const char *encoded) {
    void *slot = strmap_get(&map->index, encoded);
    return slot ? (long)((uintptr_t)slot - 1) : -1;
}

/* Insert or update; the key must already be validated as supported. */
static void ct_map_put(CtMap *map, CtValue key, CtValue value) {
    char *encoded = ct_map_encode_key(key);
    long found = ct_map_find(map, encoded);
    if (found >= 0) {
        map->values[found] = value;
        free(encoded);
        return;
    }
    if (map->len + 1 > map->cap) {
        map->cap = map->cap ? map->cap * 2 : 8;
        map->keys = (CtValue *)xrealloc(map->keys, map->cap * sizeof(CtValue));
        map->values = (CtValue *)xrealloc(map->values, map->cap * sizeof(CtValue));
    }
    CtValue stored_key = key.kind == CT_STR ? ct_make_str(key.as.str ? key.as.str : "") : key;
    map->keys[map->len] = stored_key;
    map->values[map->len] = value;
    map->len++;
    strmap_set(&map->index, encoded, (void *)(uintptr_t)map->len);
    free(encoded);
}

static bool ct_map_lookup(CtMap *map, CtValue key, CtValue *out) {
    char *encoded = ct_map_encode_key(key);
    if (!encoded) {
        return false;
    }
    long found = ct_map_find(map, encoded);
    free(encoded);
    if (found < 0) {
        return false;
    }
    *out = map->values[found];
    return true;
}

static void ct_map_clear_entries(CtMap *map) {
    map->len = 0;
    strmap_init(&map->index);
}

static void emit_line(FunctionEmitter *builder, const char *line) {
    VEC_PUSH(builder->text, str_dup(line));
}

static void emitter_init(FunctionEmitter *builder, StrVec *text, bool debug) {
    builder->text = text;
    builder->debug_enabled = debug;
    builder->current_loc = NULL;
}

static char *sanitize_label(const char *name) {
    size_t len = strlen(name);
    char *out = (char *)xmalloc(len * 4 + 2);
    size_t pos = 0;
    for (size_t i = 0; i < len; i++) {
        unsigned char ch = (unsigned char)name[i];
        if (isalnum(ch) || ch == '_') {
            out[pos++] = ch;
        } else {
            pos += (size_t)sprintf(out + pos, "_%02x", ch);
        }
    }
    if (pos == 0) {
        out[pos++] = 'a';
    }
    if (isdigit((unsigned char)out[0])) {
        memmove(out + 1, out, pos);
        out[0] = '_';
        pos++;
    }
    out[pos] = '\0';
    return out;
}

static char *path_basename(const char *path);
static char *path_join(const char *a, const char *b);

static SourceLocation *location_for_token(Parser *parser, Token token) {
    for (size_t i = 0; i < parser->file_spans.len; i++) {
        FileSpan span = parser->file_spans.data[i];
        if (token.line >= span.start_line && token.line < span.end_line) {
            int local_line = span.local_start_line + (token.line - span.start_line);
            SourceLocation *loc = (SourceLocation *)xmalloc(sizeof(SourceLocation));
            loc->path = path_basename(span.path);
            loc->line = local_line;
            loc->column = token.column;
            return loc;
        }
    }
    SourceLocation *loc = (SourceLocation *)xmalloc(sizeof(SourceLocation));
    loc->path = parser->primary_path ? path_basename(parser->primary_path) : str_dup("<source>");
    loc->line = token.line;
    loc->column = token.column;
    return loc;
}

static void parser_push_control(Parser *parser, const char *type) {
    if (parser->control_len + 1 > parser->control_cap) {
        parser->control_cap = parser->control_cap ? parser->control_cap * 2 : 16;
        parser->control_stack = xrealloc(parser->control_stack, parser->control_cap * sizeof(*parser->control_stack));
    }
    parser->control_stack[parser->control_len].type = str_dup(type);
    parser->control_stack[parser->control_len].false_label = NULL;
    parser->control_stack[parser->control_len].end_label = NULL;
    parser->control_stack[parser->control_len].begin_label = NULL;
    parser->control_stack[parser->control_len].loop_label = NULL;
    parser->control_stack[parser->control_len].line = parser->has_last_token ? parser->last_token.line : 0;
    parser->control_stack[parser->control_len].column = parser->has_last_token ? parser->last_token.column : 0;
    VEC_INIT(&parser->control_stack[parser->control_len].with_names);
    parser->control_stack[parser->control_len].frame = NULL;
    parser->control_len++;
}

static int parser_pop_control(Parser *parser, const char *expected_type) {
    if (!parser->control_len) {
        return -1;
    }
    if (expected_type && strcmp(parser->control_stack[parser->control_len - 1].type, expected_type) != 0) {
        return -2;
    }
    parser->control_len--;
    return 0;
}

static void parser_emit_op(Parser *parser, Op op) {
    if (op.loc == NULL && parser->has_last_token) {
        op.loc = location_for_token(parser, parser->last_token);
    }
    if (parser->current_def) {
        VEC_PUSH(&parser->current_def->body, op);
    } else {
        Form form = {0};
        form.kind = FORM_DEF;
        Definition *dummy = (Definition *)xmalloc(sizeof(Definition));
        *dummy = (Definition){0};
        dummy->name = str_dup("<top>");
        VEC_INIT(&dummy->body);
        VEC_PUSH(&dummy->body, op);
        form.ptr = dummy;
        VEC_PUSH(&parser->module.forms, form);
    }
}

static void parser_init(Parser *parser, Dictionary *dict, Reader *reader) {
    parser->dictionary = dict;
    parser->reader = reader;
    VEC_INIT(&parser->tokens);
    parser->pos = 0;
    parser->tokenizer_exhausted = false;
    VEC_INIT(&parser->module.forms);
    strmap_init(&parser->module.variables);
    parser->module.prelude = NULL;
    parser->module.bss = NULL;
    parser->current_def = NULL;
    parser->definition_stack = NULL;
    parser->definition_stack_len = 0;
    parser->definition_stack_cap = 0;
    parser->last_defined = NULL;
    VEC_INIT(&parser->file_spans);
    parser->source = NULL;
    parser->macro_recording.active = false;
    memset(&parser->macro_body, 0, sizeof(parser->macro_body));
    parser->pending_quotes = NULL;
    parser->pending_quote_len = 0;
    parser->control_stack = NULL;
    parser->control_len = 0;
    parser->control_cap = 0;
    parser->label_counter = 0;
    parser->gensym_counter = 0;
    parser->token_hook = NULL;
    parser->has_last_token = false;
    strmap_init(&parser->variable_labels);
    strmap_init(&parser->variable_words);
    parser->ct_vm = NULL;
    parser->custom_prelude = NULL;
    parser->custom_data = NULL;
    parser->custom_bss = NULL;
    parser->bss_replaced = false;
    parser->pending_inline_def = false;
    parser->pending_preserve_def = false;
    parser->uses_libc = false;
    parser->uses_libm = false;
    parser->primary_path = NULL;
}

static void register_builtin_syscall(Parser *parser) {
    AsmDefinition *def = (AsmDefinition *)xmalloc(sizeof(AsmDefinition));
    memset(def, 0, sizeof(AsmDefinition));
    def->name = str_dup("syscall");
    def->body = str_dup(
        "    mov rax, [r12]\n"
        "    add r12, 8\n"
        "    mov rcx, [r12]\n"
        "    add r12, 8\n"
        "    cmp rcx, 6\n"
        "    jle .sys_args\n"
        "    mov rcx, 6\n"
        ".sys_args:\n"
        "    cmp rcx, 6\n"
        "    jl .arg5\n"
        "    mov r9, [r12]\n"
        "    add r12, 8\n"
        ".arg5:\n"
        "    cmp rcx, 5\n"
        "    jl .arg4\n"
        "    mov r8, [r12]\n"
        "    add r12, 8\n"
        ".arg4:\n"
        "    cmp rcx, 4\n"
        "    jl .arg3\n"
        "    mov r10, [r12]\n"
        "    add r12, 8\n"
        ".arg3:\n"
        "    cmp rcx, 3\n"
        "    jl .arg2\n"
        "    mov rdx, [r12]\n"
        "    add r12, 8\n"
        ".arg2:\n"
        "    cmp rcx, 2\n"
        "    jl .arg1\n"
        "    mov rsi, [r12]\n"
        "    add r12, 8\n"
        ".arg1:\n"
        "    cmp rcx, 1\n"
        "    jl .do_syscall\n"
        "    mov rdi, [r12]\n"
        "    add r12, 8\n"
        ".do_syscall:\n"
        "    syscall\n"
        "    sub r12, 8\n"
        "    mov [r12], rax\n"
    );

    Word *word = dictionary_lookup(parser->dictionary, def->name);
    if (!word) {
        word = (Word *)xmalloc(sizeof(Word));
        memset(word, 0, sizeof(Word));
        word->name = str_dup(def->name);
        dictionary_register(parser->dictionary, word);
    }
    word->asm_def = def;
    Form form = {0};
    form.kind = FORM_ASM;
    form.ptr = def;
    VEC_PUSH(&parser->module.forms, form);
}

static void ensure_tokens(Parser *parser, size_t upto) {
    if (parser->tokenizer_exhausted) {
        return;
    }
    while (parser->tokens.len <= upto && !parser->tokenizer_exhausted) {
        Token tok = {0};
        if (!tokenizer_next(&parser->tokenizer, &tok)) {
            parser->tokenizer_exhausted = true;
            break;
        }
        VEC_PUSH(&parser->tokens, tok);
    }
}

static bool parser_eof(Parser *parser) {
    ensure_tokens(parser, parser->pos);
    return parser->pos >= parser->tokens.len;
}

static Token parser_peek_token(Parser *parser) {
    ensure_tokens(parser, parser->pos);
    if (parser->pos >= parser->tokens.len) {
        Token empty = {0};
        empty.lexeme = NULL;
        return empty;
    }
    return parser->tokens.data[parser->pos];
}

static Token parser_next_token(Parser *parser) {
    ensure_tokens(parser, parser->pos);
    if (parser->pos >= parser->tokens.len) {
        Token empty = {0};
        empty.lexeme = NULL;
        return empty;
    }
    Token tok = parser->tokens.data[parser->pos++];
    parser->last_token = tok;
    parser->has_last_token = true;
    return tok;
}

static char *parser_new_label(Parser *parser, const char *prefix) {
    char *label = str_printf("L_%s_%d", prefix, parser->label_counter++);
    return label;
}

static void ct_vm_init(CompileTimeVM *vm, Parser *parser) {
    vm->parser = parser;
    vm->dictionary = parser->dictionary;
    ct_stack_init(&vm->stack);
    ct_stack_init(&vm->rstack);
    VEC_INIT(&vm->loop_remaining);
    VEC_INIT(&vm->loop_begin);
    VEC_INIT(&vm->loop_initial);
    VEC_INIT(&vm->call_stack);
    vm->binding_names = NULL;
    vm->binding_values = NULL;
    vm->binding_len = 0;
    vm->emit_out = NULL;
    ct_stack_init(&vm->unquote_values);
    VEC_INIT(&vm->unquote_depths);
}

static void ct_vm_reset(CompileTimeVM *vm) {
    vm->stack.len = 0;
    vm->rstack.len = 0;
    vm->loop_remaining.len = 0;
    vm->loop_begin.len = 0;
    vm->loop_initial.len = 0;
    vm->call_stack.len = 0;
}

static bool try_parse_int(const char *lexeme, int64_t *out);
static void parser_inject_tokens(Parser *parser, TokenVec *injected);

static void ct_trace_error(CompileTimeVM *vm, const char *msg) {
    fprintf(stderr, "[error] %s\n", msg);
    if (vm && vm->call_stack.len) {
        fprintf(stderr, "[error] compile-time call stack:\n");
        for (size_t i = 0; i < vm->call_stack.len; i++) {
            fprintf(stderr, "  - %s\n", vm->call_stack.data[i]);
        }
    }
    exit(1);
}

// Pop the data stack; popping an empty one is an error, as in the Python
// compile-time VM (it used to yield nil, so `drop` on an empty stack passed).
static CtValue ct_pop(CompileTimeVM *vm) {
    if (!vm->stack.len) {
        const char *word = vm->call_stack.len ? vm->call_stack.data[vm->call_stack.len - 1] : "?";
        char *msg = str_printf("stack underflow in '%s': the stack is empty", word);
        ct_trace_error(vm, msg);
        free(msg);
    }
    return VEC_POP(&vm->stack);
}

static int64_t ct_pop_int(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    if (v.kind == CT_STR) {
        int64_t out = 0;
        if (try_parse_int(v.as.str, &out)) {
            return out;
        }
    }
    if (v.kind != CT_INT) {
        const char *kind = "unknown";
        const char *extra = "";
        if (v.kind == CT_NIL) {
            kind = "nil";
        } else if (v.kind == CT_STR) {
            kind = "string";
            extra = v.as.str ? v.as.str : "";
        } else if (v.kind == CT_TOKEN) {
            kind = "token";
            extra = v.as.token.lexeme ? v.as.token.lexeme : "";
        } else if (v.kind == CT_LIST) {
            kind = "list";
        } else if (v.kind == CT_MAP) {
            kind = "map";
        }
        char *msg = NULL;
        if (extra[0] != '\0') {
            msg = str_printf("expected integer on compile-time stack (got %s: %s)", kind, extra);
        } else {
            msg = str_printf("expected integer on compile-time stack (got %s)", kind);
        }
        ct_trace_error(vm, msg);
        free(msg);
    }
    return v.as.i64;
}


static char *ct_pop_str(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    if (v.kind == CT_TOKEN) {
        return str_dup(v.as.token.lexeme);
    }
    if (v.kind != CT_STR) {
        ct_trace_error(vm, "expected string on compile-time stack");
    }
    return str_dup(v.as.str);
}

static CtList *ct_pop_list(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    if (v.kind != CT_LIST) {
        ct_trace_error(vm, "expected list on compile-time stack");
    }
    return v.as.list;
}

static Token ct_pop_token(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    if (v.kind == CT_TOKEN) {
        return v.as.token;
    }
    if (v.kind == CT_STR) {
        Token tok = {0};
        tok.lexeme = v.as.str;
        tok.line = 0;
        tok.column = 0;
        tok.start = 0;
        tok.end = 0;
        return tok;
    }
    ct_trace_error(vm, "expected token on compile-time stack");
    Token tok = {0};
    tok.lexeme = str_dup("");
    return tok;
}

static void ct_word_call(CompileTimeVM *vm, Word *word);
static char *ct_string_from_value(CtValue v);
// Try a JIT-safe :asm word on an integer-only CT stack; otherwise use the
// existing CT execution path.
static bool ct_try_asm_jit(CompileTimeVM *vm, Word *word, AsmDefinition *asm_def);

static const char *ct_kind_name(CtValueKind kind);

/* An :asm word neither ct_try_asm_io nor ct_try_asm_jit could run.  Its
 * machine code works on raw 8-byte cells, so it cannot take a list, map,
 * string, token or syntax value; carrying on would leave the stack as it was
 * and fail later with a confusing underflow, so stop here instead. */
static void ct_asm_unrunnable(CompileTimeVM *vm, Word *word) {
    char *msg = NULL;
    for (size_t i = vm->stack.len; i-- > 0;) {
        if (vm->stack.data[i].kind != CT_INT) {
            msg = str_printf("asm word '%s' cannot run at compile time on a %s value (stack item %zu from the top); "
                             "asm works on integer cells only", word->name,
                             ct_kind_name(vm->stack.data[i].kind), vm->stack.len - 1 - i);
            break;
        }
    }
    if (!msg) {
        msg = l2_ks_available()
            ? str_printf("asm word '%s' could not be run at compile time: its body could not be JIT-compiled", word->name)
            : str_printf("asm word '%s' cannot run at compile time: the Keystone assembler is not available "
                         "(set L2_KEYSTONE_PATH)", word->name);
    }
    ct_trace_error(vm, msg);
    free(msg);
}

static bool ct_try_asm_io(CompileTimeVM *vm, Word *word, AsmDefinition *asm_def) {
    if (strcmp(word->name, "puti") == 0) {
        int64_t v = ct_pop_int(vm);
        fprintf(stdout, "%lld", (long long)v);
        fflush(stdout);
        return true;
    }
    if (strcmp(word->name, "putu") == 0) {
        int64_t v = ct_pop_int(vm);
        fprintf(stdout, "%llu", (unsigned long long)v);
        fflush(stdout);
        return true;
    }
    if (strcmp(word->name, "print") == 0) {
        CtValue v = ct_pop(vm);
        if (v.kind == CT_STR) {
            fputs(v.as.str ? v.as.str : "", stdout);
        } else if (v.kind == CT_TOKEN) {
            fputs(v.as.token.lexeme ? v.as.token.lexeme : "", stdout);
        } else if (v.kind == CT_INT) {
            fprintf(stdout, "%lld", (long long)v.as.i64);
        } else {
            char *txt = ct_string_from_value(v);
            fputs(txt, stdout);
            free(txt);
        }
        fputc('\n', stdout);
        fflush(stdout);
        return true;
    }
    if (asm_def && asm_def->effect_string_io) {
        CtValue v = ct_pop(vm);
        if (v.kind == CT_STR) {
            FILE *out = stdout;
            if (strcmp(word->name, "ewrite_buf") == 0) {
                out = stderr;
            }
            fputs(v.as.str ? v.as.str : "", out);
            fflush(out);
        } else if (v.kind == CT_TOKEN) {
            FILE *out = stdout;
            if (strcmp(word->name, "ewrite_buf") == 0) {
                out = stderr;
            }
            fputs(v.as.token.lexeme ? v.as.token.lexeme : "", out);
            fflush(out);
        } else {
            ct_pop(vm);
        }
        return true;
    }
    if (strcmp(word->name, "putc") == 0) {
        CtValue v = ct_pop(vm);
        int ch = 0;
        if (v.kind == CT_INT) {
            ch = (int)v.as.i64;
        } else if (v.kind == CT_STR && v.as.str && v.as.str[0]) {
            ch = (unsigned char)v.as.str[0];
        }
        fputc(ch, stdout);
        fflush(stdout);
        return true;
    }
    return false;
}

static CtValue ct_eval_quotation(CompileTimeVM *vm, QuoteTemplate *tpl);
static bool ct_lookup_binding(CompileTimeVM *vm, const char *name, CtValue *out);

/* ---- Calling the host program's native words from eval ----------------
 * A program that uses eval gets `l2_word_table` from the compiler: a count,
 * then (name pointer, name bytes, code address) records of its emitted
 * words.  When eval'd code calls one of the host's own words, eval runs the
 * word's machine code in place (L2 calling convention: r12 data stack, r13
 * return stack) instead of interpreting a copy of its source. */
#if defined(__GNUC__)
extern const unsigned long long l2_word_table[] __attribute__((weak));
#else
extern const unsigned long long l2_word_table[];
#endif

static void *l2_native_word_address(const char *name) {
    if (&l2_word_table == NULL || l2_word_table == NULL || !name) {
        return NULL;
    }
    size_t name_len = strlen(name);
    unsigned long long count = l2_word_table[0];
    const unsigned long long *record = l2_word_table + 1;
    for (unsigned long long i = 0; i < count; i++, record += 3) {
        if ((size_t)record[1] == name_len && memcmp((const char *)(uintptr_t)record[0], name, name_len) == 0) {
            return (void *)(uintptr_t)record[2];
        }
    }
    return NULL;
}

/* Call native L2 code: r12 and r13 are set to the given stacks, every
 * callee-saved register is preserved (L2 code uses rbx, rbp and r12-r15
 * freely), and the call is made with a 16-byte aligned stack outside any
 * red zone.  Returns the final r12. */
#if defined(__x86_64__)
uint64_t l2_native_call(void *fn, uint64_t r12_in, uint64_t r13_in);
__asm__(
    ".text\n"
    ".globl l2_native_call\n"
    ".hidden l2_native_call\n"
    ".type l2_native_call, @function\n"
    "l2_native_call:\n"
    "    pushq %rbp\n"
    "    movq %rsp, %rbp\n"
    "    pushq %rbx\n"
    "    pushq %r12\n"
    "    pushq %r13\n"
    "    pushq %r14\n"
    "    pushq %r15\n"
    "    subq $8, %rsp\n"
    "    movq %rsi, %r12\n"
    "    movq %rdx, %r13\n"
    "    callq *%rdi\n"
    "    movq %r12, %rax\n"
    "    leaq -40(%rbp), %rsp\n"
    "    popq %r15\n"
    "    popq %r14\n"
    "    popq %r13\n"
    "    popq %r12\n"
    "    popq %rbx\n"
    "    popq %rbp\n"
    "    ret\n"
    ".size l2_native_call, .-l2_native_call\n"
);
#define L2_HAVE_NATIVE_CALL 1
#endif

#define L2_NATIVE_DSTACK_CELLS (64 * 1024)
#define L2_NATIVE_RSTACK_CELLS (16 * 1024)

/* Run `word` from the host program natively when it is the host's own
 * definition (not one eval'd code defined or replaced) and a native
 * address exists.  Integers move as one cell and strings as (pointer,
 * length), as at runtime; any other value kind on the stack keeps the
 * word in the compile-time VM.  Returns false when the word did not run. */
/* A process that runs eval without a native word table (the compiler
 * running `main` at compile time) can supply this hook instead: it runs the
 * host word `name` on the data stack cells [sp, top), with room down to
 * `top - capacity` cells, and returns the new stack pointer, or 0 when it has
 * no such word. */
typedef uint64_t (*L2HostWordCaller)(const char *name, uint64_t sp, uint64_t top, uint64_t capacity);
static L2HostWordCaller g_l2_host_word_caller = NULL;

void l2_set_host_word_caller(L2HostWordCaller caller) {
    g_l2_host_word_caller = caller;
}

static bool ct_word_call_native(CompileTimeVM *vm, Word *word) {
#if defined(L2_HAVE_NATIVE_CALL)
    if (word->macro_body || word->macro_expansion || word->immediate || word->compile_only ||
        word->is_extern || word->ct_intrinsic || word->ct_definition || word->asm_def || word->ct_asm_def) {
        return false;
    }
    bool host_word = word->definition == NULL ||
                     (word->host_seeded && word->definition == word->host_definition);
    if (!host_word) {
        return false;
    }
    void *fn = l2_native_word_address(word->name);
    if (!fn && !g_l2_host_word_caller) {
        return false;
    }
    size_t cells = 0;
    for (size_t i = 0; i < vm->stack.len; i++) {
        CtValueKind kind = vm->stack.data[i].kind;
        if (kind == CT_INT || kind == CT_NIL) {
            cells += 1;
        } else if (kind == CT_STR || kind == CT_TOKEN) {
            cells += 2;
        } else {
            return false;
        }
    }
    if (cells >= L2_NATIVE_DSTACK_CELLS / 2) {
        return false;
    }
    uint64_t *dstack = (uint64_t *)xmalloc(L2_NATIVE_DSTACK_CELLS * sizeof(uint64_t));
    uint64_t *rstack = (uint64_t *)xmalloc(L2_NATIVE_RSTACK_CELLS * sizeof(uint64_t));
    uint64_t *top = dstack + L2_NATIVE_DSTACK_CELLS;
    /* Bottom value first, so the top of the stack ends up at the lowest address. */
    uint64_t *sp = top;
    size_t *value_cell = (size_t *)xmalloc((vm->stack.len + 1) * sizeof(size_t));
    for (size_t i = 0; i < vm->stack.len; i++) {
        uint64_t pair[2];
        size_t count = 0;
        CtValue *value = &vm->stack.data[i];
        if (value->kind == CT_INT) {
            pair[0] = (uint64_t)value->as.i64;
            count = 1;
        } else if (value->kind == CT_NIL) {
            pair[0] = 0;
            count = 1;
        } else {
            const char *text = value->kind == CT_STR ? value->as.str : value->as.token.lexeme;
            text = text ? text : "";
            pair[0] = (uint64_t)(uintptr_t)text;
            pair[1] = (uint64_t)strlen(text);
            count = 2;
        }
        value_cell[i] = (size_t)(top - sp);
        for (size_t c = 0; c < count; c++) {
            *--sp = pair[c];
        }
    }
    value_cell[vm->stack.len] = (size_t)(top - sp);
    uint64_t *snapshot = (uint64_t *)xmalloc((cells + 1) * sizeof(uint64_t));
    memcpy(snapshot, sp, cells * sizeof(uint64_t));

    uint64_t *new_sp;
    if (fn) {
        new_sp = (uint64_t *)(uintptr_t)l2_native_call(
            fn, (uint64_t)(uintptr_t)sp, (uint64_t)(uintptr_t)(rstack + L2_NATIVE_RSTACK_CELLS));
    } else {
        new_sp = (uint64_t *)(uintptr_t)g_l2_host_word_caller(
            word->name, (uint64_t)(uintptr_t)sp, (uint64_t)(uintptr_t)top, (uint64_t)L2_NATIVE_DSTACK_CELLS);
        if (!new_sp) {
            free(snapshot);
            free(value_cell);
            free(rstack);
            free(dstack);
            return false;
        }
    }
    if (new_sp > top || new_sp < dstack) {
        fprintf(stderr, "[error] native word '%s' left an invalid data stack pointer\n", word->name);
        exit(1);
    }
    size_t new_cells = (size_t)(top - new_sp);
    /* Values at the bottom that the word did not touch keep their kind (a
     * string stays a string); everything from the first changed cell up is
     * read back as integers. */
    size_t keep = 0;
    while (keep < vm->stack.len && value_cell[keep + 1] <= new_cells) {
        size_t lo = value_cell[keep];
        size_t hi = value_cell[keep + 1];
        bool same = true;
        for (size_t c = lo; c < hi; c++) {
            /* cell index c counts from the bottom: address top - 1 - c */
            if (top[-1 - (ptrdiff_t)c] != snapshot[cells - 1 - c]) {
                same = false;
                break;
            }
        }
        if (!same) {
            break;
        }
        keep++;
    }
    vm->stack.len = keep;
    for (size_t c = value_cell[keep]; c < new_cells; c++) {
        ct_stack_push(&vm->stack, ct_make_int((int64_t)top[-1 - (ptrdiff_t)c]));
    }
    free(snapshot);
    free(value_cell);
    free(rstack);
    free(dstack);
    return true;
#else
    (void)vm;
    (void)word;
    return false;
#endif
}

/* Word pointers (`&name`) made at compile time: the value is the Word's
 * address, recorded here so `call`/`jmp` can tell it from a native address. */
static Word **g_ct_word_ptrs;
static size_t g_ct_word_ptr_len;
static size_t g_ct_word_ptr_cap;

static int64_t ct_word_ptr_handle(Word *word) {
    for (size_t i = 0; i < g_ct_word_ptr_len; i++) {
        if (g_ct_word_ptrs[i] == word) {
            return (int64_t)(uintptr_t)word;
        }
    }
    if (g_ct_word_ptr_len == g_ct_word_ptr_cap) {
        g_ct_word_ptr_cap = g_ct_word_ptr_cap ? g_ct_word_ptr_cap * 2 : 16;
        g_ct_word_ptrs = (Word **)xrealloc(g_ct_word_ptrs, g_ct_word_ptr_cap * sizeof(Word *));
    }
    g_ct_word_ptrs[g_ct_word_ptr_len++] = word;
    return (int64_t)(uintptr_t)word;
}

static Word *ct_word_ptr_target(int64_t value) {
    for (size_t i = 0; i < g_ct_word_ptr_len; i++) {
        if ((int64_t)(uintptr_t)g_ct_word_ptrs[i] == value) {
            return g_ct_word_ptrs[i];
        }
    }
    return NULL;
}

static void ct_execute_nodes(CompileTimeVM *vm, OpVec *nodes) {
    StrMap labels;
    strmap_init(&labels);
    for (size_t i = 0; i < nodes->len; i++) {
        Op *node = &nodes->data[i];
        if (node->kind == OP_LABEL) {
            strmap_set(&labels, node->data.label, (void *)(uintptr_t)i);
        }
    }

    IntVec begin_stack;
    VEC_INIT(&begin_stack);
    size_t ip = 0;
    while (ip < nodes->len) {
        Op node = nodes->data[ip];
        if (node.kind == OP_LITERAL) {
            if (node.lit_kind == LIT_INT) {
                ct_stack_push(&vm->stack, ct_make_int(node.data.i64));
            } else if (node.lit_kind == LIT_FLOAT) {
                /* A float is its IEEE-754 bit pattern, as at runtime. */
                int64_t bits;
                memcpy(&bits, &node.data.f64, sizeof(bits));
                ct_stack_push(&vm->stack, ct_make_int(bits));
            } else if (node.lit_kind == LIT_STRING) {
                ct_stack_push(&vm->stack, ct_make_str(node.data.str));
            }
            ip++;
            continue;
        }
        if (node.kind == OP_WORD) {
            const char *name = node.data.word;
            if (strcmp(name, "begin") == 0) {
                VEC_PUSH(&begin_stack, (int)ip);
                ip++;
                continue;
            }
            if (strcmp(name, "again") == 0) {
                if (!begin_stack.len) {
                    fprintf(stderr, "[error] 'again' without matching 'begin'\n");
                    exit(1);
                }
                ip = (size_t)begin_stack.data[begin_stack.len - 1] + 1;
                continue;
            }
            if (strcmp(name, "continue") == 0) {
                if (!begin_stack.len) {
                    fprintf(stderr, "[error] 'continue' outside begin/again loop\n");
                    exit(1);
                }
                ip = (size_t)begin_stack.data[begin_stack.len - 1] + 1;
                continue;
            }
            if (strcmp(name, "exit") == 0) {
                return;
            }
            Word *word = dictionary_lookup(vm->dictionary, name);
            if (!word) {
                fprintf(stderr, "[error] unknown word '%s' during compile-time execution\n", name);
                exit(1);
            }
            ct_word_call(vm, word);
            ip++;
            continue;
        }
        if (node.kind == OP_WORD_PTR) {
            Word *target = dictionary_lookup(vm->dictionary, node.data.word);
            if (!target) {
                fprintf(stderr, "[error] unknown word '%s' referenced by pointer during compile-time execution\n",
                        node.data.word);
                exit(1);
            }
            ct_stack_push(&vm->stack, ct_make_int(ct_word_ptr_handle(target)));
            ip++;
            continue;
        }
        if (node.kind == OP_SYNTAX) {
            ct_stack_push(&vm->stack, ct_eval_quotation(vm, (QuoteTemplate *)node.data.ptr));
            ip++;
            continue;
        }
        if (node.kind == OP_BINDING) {
            CtValue value;
            if (!ct_lookup_binding(vm, node.data.word, &value)) {
                char msg[256];
                snprintf(msg, sizeof(msg), "unbound '$%s'", node.data.word);
                ct_trace_error(vm, msg);
            }
            ct_stack_push(&vm->stack, value);
            ip++;
            continue;
        }
        if (node.kind == OP_BRANCH_ZERO) {
            CtValue v = ct_pop(vm);
            bool flag = false;
            if (v.kind == CT_INT) {
                flag = v.as.i64 != 0;
            }
            if (!flag) {
                void *target = strmap_get(&labels, node.data.label);
                if (!target) {
                    fprintf(stderr, "[error] unknown label '%s' during compile-time execution\n", node.data.label);
                    exit(1);
                }
                ip = (size_t)(uintptr_t)target;
            } else {
                ip++;
            }
            continue;
        }
        if (node.kind == OP_JUMP) {
            void *target = strmap_get(&labels, node.data.label);
            if (!target) {
                fprintf(stderr, "[error] unknown label '%s' during compile-time execution\n", node.data.label);
                exit(1);
            }
            ip = (size_t)(uintptr_t)target;
            continue;
        }
        if (node.kind == OP_FOR_BEGIN) {
            int64_t count = ct_pop_int(vm);
            if (count <= 0) {
                ip++;
                continue;
            }
            VEC_PUSH(&vm->loop_remaining, (int)count);
            VEC_PUSH(&vm->loop_begin, (int)ip);
            VEC_PUSH(&vm->loop_initial, (int)count);
            ip++;
            continue;
        }
        if (node.kind == OP_FOR_END) {
            if (!vm->loop_remaining.len) {
                fprintf(stderr, "[error] 'next' without matching 'for'\n");
                exit(1);
            }
            int idx = (int)vm->loop_remaining.len - 1;
            vm->loop_remaining.data[idx] -= 1;
            if (vm->loop_remaining.data[idx] > 0) {
                ip = (size_t)vm->loop_begin.data[idx] + 1;
            } else {
                vm->loop_remaining.len--;
                vm->loop_begin.len--;
                vm->loop_initial.len--;
                ip++;
            }
            continue;
        }
        if (node.kind == OP_RET) {
            return;
        }
        ip++;
    }
}

static void parser_macro_expand_into(Parser *parser, Word *word, TokenVec *out);

static void ct_word_call(CompileTimeVM *vm, Word *word) {
    if ((strcmp(word->name, "call") == 0 || strcmp(word->name, "jmp") == 0) && vm->stack.len > 0) {
        /* `&name call`: a word pointer made at compile time names a Word,
         * not native code, so the call runs that word here. */
        CtValue top = vm->stack.data[vm->stack.len - 1];
        Word *target = top.kind == CT_INT ? ct_word_ptr_target(top.as.i64) : NULL;
        if (target) {
            vm->stack.len--;
            ct_word_call(vm, target);
            return;
        }
    }
    if (ct_word_call_native(vm, word)) {
        return;
    }
    if (word->macro_body || word->macro_expansion) {
        /* A macro called from compile-time code reads its arguments from the
         * input and adds its output to the running macro's output (or, from
         * other code, to the input read next). */
        TokenVec local;
        VEC_INIT(&local);
        parser_macro_expand_into(vm->parser, word, vm->emit_out ? vm->emit_out : &local);
        if (!vm->emit_out) parser_inject_tokens(vm->parser, &local);
        return;
    }
    VEC_PUSH(&vm->call_stack, str_dup(word->name));
    if (word->runtime_only) {
        fprintf(stderr, "[error] word '%s' is runtime-only and cannot be executed at compile time\n", word->name);
        exit(1);
    }
    if (word->compile_time_override) {
        if (word->ct_definition) {
            ct_execute_nodes(vm, &word->ct_definition->body);
            vm->call_stack.len--;
            return;
        }
        if (word->definition) {
            ct_execute_nodes(vm, &word->definition->body);
            vm->call_stack.len--;
            return;
        }
        if (word->ct_intrinsic) {
            word->ct_intrinsic(vm);
            vm->call_stack.len--;
            return;
        }
        if (word->ct_asm_def) {
            if (ct_try_asm_io(vm, word, word->ct_asm_def)) {
                vm->call_stack.len--;
                return;
            }
            if (ct_try_asm_jit(vm, word, word->ct_asm_def)) {
                vm->call_stack.len--;
                return;
            }
            ct_asm_unrunnable(vm, word);
            vm->call_stack.len--;
            return;
        }
    }
    bool prefer_def = (word->definition && (word->immediate || word->compile_only));
    if (!prefer_def && word->ct_intrinsic) {
        word->ct_intrinsic(vm);
        vm->call_stack.len--;
        return;
    }
    Definition *def = word->definition;
    if (word->compile_only && word->ct_definition) {
        def = word->ct_definition;
    }
    if (!def) {
        if (word->ct_intrinsic) {
            word->ct_intrinsic(vm);
            vm->call_stack.len--;
            return;
        }
        if (word->asm_def || word->ct_asm_def) {
            AsmDefinition *asm_def = word->ct_asm_def ? word->ct_asm_def : word->asm_def;
            if (ct_try_asm_io(vm, word, asm_def)) {
                vm->call_stack.len--;
                return;
            }
            if (ct_try_asm_jit(vm, word, asm_def)) {
                vm->call_stack.len--;
                return;
            }
            ct_asm_unrunnable(vm, word);
            vm->call_stack.len--;
            return;
        }
        if (word->is_extern) {
            int pops = word->extern_arg_count > 0 ? word->extern_arg_count : word->extern_inputs;
            for (int i = 0; i < pops; i++) {
                ct_pop(vm);
            }
            int outputs = 0;
            if (word->extern_arg_count > 0) {
                if (!word->extern_ret_type || strcmp(word->extern_ret_type, "void") != 0) {
                    outputs = 1;
                }
            } else {
                outputs = word->extern_outputs;
            }
            for (int i = 0; i < outputs; i++) {
                ct_stack_push(&vm->stack, ct_make_int(0));
            }
            vm->call_stack.len--;
            return;
        }
        if (g_l2_eval_depth > 0 && !word->host_seeded) {
            fprintf(stderr,
                    "[error] eval: unknown word '%s'\n"
                    "[error] if it is one of the program's own words, build the program with "
                    "--source-embed (eval reads its source) or with --word-table plus --no-dce or "
                    "`preserve word` (eval calls its native code)\n",
                    word->name);
        } else {
            fprintf(stderr, "[error] word '%s' has no compile-time definition\n", word->name);
        }
        exit(1);
    }
    ct_execute_nodes(vm, &def->body);
    vm->call_stack.len--;
}

static bool ct_truthy(CtValue v) {
    if (v.kind == CT_NIL) {
        return false;
    }
    if (v.kind == CT_INT) {
        return v.as.i64 != 0;
    }
    if (v.kind == CT_STR) {
        return v.as.str && v.as.str[0] != '\0';
    }
    return true;
}

static char *ct_string_from_value(CtValue v) {
    if (v.kind == CT_TOKEN) {
        return str_dup(v.as.token.lexeme);
    }
    if (v.kind == CT_STR) {
        return str_dup(v.as.str);
    }
    if (v.kind == CT_INT) {
        return str_printf("%lld", (long long)v.as.i64);
    }
    return str_dup("");
}

static void ct_intrinsic_dup(CompileTimeVM *vm) {
    CtValue v = ct_stack_peek(&vm->stack);
    ct_stack_push(&vm->stack, v);
}

static void ct_intrinsic_drop(CompileTimeVM *vm) {
    ct_pop(vm);
}

static void ct_intrinsic_swap(CompileTimeVM *vm) {
    CtValue a = ct_pop(vm);
    CtValue b = ct_pop(vm);
    ct_stack_push(&vm->stack, a);
    ct_stack_push(&vm->stack, b);
}

static void ct_intrinsic_over(CompileTimeVM *vm) {
    if (vm->stack.len < 2) {
        fprintf(stderr, "[error] over expects at least 2 items\n");
        exit(1);
    }
    CtValue v = vm->stack.data[vm->stack.len - 2];
    ct_stack_push(&vm->stack, v);
}

static void ct_intrinsic_rot(CompileTimeVM *vm) {
    if (vm->stack.len < 3) {
        fprintf(stderr, "[error] rot expects at least 3 items\n");
        exit(1);
    }
    CtValue a = vm->stack.data[vm->stack.len - 3];
    CtValue b = vm->stack.data[vm->stack.len - 2];
    CtValue c = vm->stack.data[vm->stack.len - 1];
    vm->stack.data[vm->stack.len - 3] = b;
    vm->stack.data[vm->stack.len - 2] = c;
    vm->stack.data[vm->stack.len - 1] = a;
}

static void ct_intrinsic_pick(CompileTimeVM *vm) {
    int64_t idx = ct_pop_int(vm);
    if (idx < 0 || (size_t)(idx + 1) > vm->stack.len) {
        fprintf(stderr, "[error] pick index out of range\n");
        exit(1);
    }
    CtValue v = vm->stack.data[vm->stack.len - 1 - (size_t)idx];
    ct_stack_push(&vm->stack, v);
}

static void ct_intrinsic_rpick(CompileTimeVM *vm) {
    int64_t idx = ct_pop_int(vm);
    if (idx < 0 || (size_t)(idx + 1) > vm->rstack.len) {
        fprintf(stderr, "[error] rpick index out of range\n");
        exit(1);
    }
    CtValue v = vm->rstack.data[vm->rstack.len - 1 - (size_t)idx];
    ct_stack_push(&vm->stack, v);
}

static void ct_intrinsic_to_r(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    ct_stack_push(&vm->rstack, v);
}

static void ct_intrinsic_from_r(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->rstack);
    ct_stack_push(&vm->stack, v);
}

static void ct_intrinsic_r_fetch(CompileTimeVM *vm) {
    ct_stack_push(&vm->stack, ct_stack_peek(&vm->rstack));
}

static void ct_intrinsic_rdrop(CompileTimeVM *vm) {
    ct_stack_pop(&vm->rstack);
}

static void ct_intrinsic_add(CompileTimeVM *vm) {
    int64_t b = ct_pop_int(vm);
    int64_t a = ct_pop_int(vm);
    ct_stack_push(&vm->stack, ct_make_int(a + b));
}

static void ct_intrinsic_sub(CompileTimeVM *vm) {
    int64_t b = ct_pop_int(vm);
    int64_t a = ct_pop_int(vm);
    ct_stack_push(&vm->stack, ct_make_int(a - b));
}

static void ct_intrinsic_mul(CompileTimeVM *vm) {
    int64_t b = ct_pop_int(vm);
    int64_t a = ct_pop_int(vm);
    ct_stack_push(&vm->stack, ct_make_int(a * b));
}

static void ct_intrinsic_div(CompileTimeVM *vm) {
    int64_t b = ct_pop_int(vm);
    int64_t a = ct_pop_int(vm);
    if (b == 0) {
        fprintf(stderr, "[error] division by zero in compile-time VM\n");
        exit(1);
    }
    ct_stack_push(&vm->stack, ct_make_int(a / b));
}

static void ct_intrinsic_mod(CompileTimeVM *vm) {
    int64_t b = ct_pop_int(vm);
    int64_t a = ct_pop_int(vm);
    if (b == 0) {
        fprintf(stderr, "[error] modulo by zero in compile-time VM\n");
        exit(1);
    }
    ct_stack_push(&vm->stack, ct_make_int(a % b));
}

static void ct_intrinsic_eq(CompileTimeVM *vm) {
    CtValue b = ct_pop(vm);
    CtValue a = ct_pop(vm);
    if (a.kind == CT_INT && b.kind == CT_INT) {
        ct_stack_push(&vm->stack, ct_make_int(a.as.i64 == b.as.i64));
        return;
    }
    char *sa = ct_string_from_value(a);
    char *sb = ct_string_from_value(b);
    bool eq = strcmp(sa, sb) == 0;
    free(sa);
    free(sb);
    ct_stack_push(&vm->stack, ct_make_int(eq));
}

static void ct_intrinsic_gt(CompileTimeVM *vm) {
    int64_t b = ct_pop_int(vm);
    int64_t a = ct_pop_int(vm);
    ct_stack_push(&vm->stack, ct_make_int(a > b));
}

static void ct_intrinsic_lt(CompileTimeVM *vm) {
    int64_t b = ct_pop_int(vm);
    int64_t a = ct_pop_int(vm);
    ct_stack_push(&vm->stack, ct_make_int(a < b));
}

static void ct_intrinsic_ge(CompileTimeVM *vm) {
    int64_t b = ct_pop_int(vm);
    int64_t a = ct_pop_int(vm);
    ct_stack_push(&vm->stack, ct_make_int(a >= b));
}

static void ct_intrinsic_le(CompileTimeVM *vm) {
    int64_t b = ct_pop_int(vm);
    int64_t a = ct_pop_int(vm);
    ct_stack_push(&vm->stack, ct_make_int(a <= b));
}

static void ct_intrinsic_ne(CompileTimeVM *vm) {
    CtValue b = ct_pop(vm);
    CtValue a = ct_pop(vm);
    if (a.kind == CT_INT && b.kind == CT_INT) {
        ct_stack_push(&vm->stack, ct_make_int(a.as.i64 != b.as.i64));
        return;
    }
    char *sa = ct_string_from_value(a);
    char *sb = ct_string_from_value(b);
    bool ne = strcmp(sa, sb) != 0;
    free(sa);
    free(sb);
    ct_stack_push(&vm->stack, ct_make_int(ne));
}

static void ct_intrinsic_not(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    ct_stack_push(&vm->stack, ct_make_int(!ct_truthy(v)));
}

/* ---------------------------------------------------------------------------
 * CT data and value words (nil, lists, maps, strings) for the C compile-time VM.
 * Same names, stack effects, and strict inputs as the Python compiler:
 * reads consume their subject, mutations return the subject first, invalid
 * types/indexes/keys are CT errors raised before any mutation.
 * ------------------------------------------------------------------------- */

static const char *ct_kind_name(CtValueKind kind) {
    switch (kind) {
        case CT_NIL: return "nil";
        case CT_INT: return "integer";
        case CT_STR: return "string";
        case CT_TOKEN: return "token";
        case CT_LIST: return "list";
        case CT_MAP: return "map";
        case CT_SYNTAX: return "syntax";
    }
    return "unknown";
}

static void ct_value_type_error(CompileTimeVM *vm, const char *word, const char *expected, CtValue got) {
    char *msg = str_printf("%s expects %s, got %s", word, expected, ct_kind_name(got.kind));
    ct_trace_error(vm, msg);
    free(msg);
}

/* Strict string pop: tokens and integers are not coerced. */
static char *ct_strict_pop_string(CompileTimeVM *vm, const char *word) {
    CtValue v = ct_pop(vm);
    if (v.kind != CT_STR) {
        ct_value_type_error(vm, word, "a string", v);
    }
    return str_dup(v.as.str ? v.as.str : "");
}

static int64_t ct_strict_pop_index(CompileTimeVM *vm, const char *word) {
    CtValue v = ct_pop(vm);
    if (v.kind != CT_INT) {
        ct_value_type_error(vm, word, "an integer index", v);
    }
    return v.as.i64;
}

static CtList *ct_strict_pop_list(CompileTimeVM *vm, const char *word) {
    CtValue v = ct_pop(vm);
    if (v.kind != CT_LIST) {
        ct_value_type_error(vm, word, "a list", v);
    }
    return v.as.list;
}

static CtMap *ct_strict_pop_map(CompileTimeVM *vm, const char *word) {
    CtValue v = ct_pop(vm);
    if (v.kind != CT_MAP) {
        ct_value_type_error(vm, word, "a map", v);
    }
    return v.as.map;
}

static void ct_check_map_key(CompileTimeVM *vm, const char *word, CtValue key) {
    if (key.kind == CT_STR || key.kind == CT_INT || key.kind == CT_NIL) {
        return;
    }
    char *msg = str_printf("%s: unsupported map key type %s; keys must be strings, integers, booleans, or nil",
                           word, ct_kind_name(key.kind));
    ct_trace_error(vm, msg);
    free(msg);
}

/* Normalize a Python-style index for an existing element. */
static size_t ct_checked_index(CompileTimeVM *vm, const char *word, CtList *list, int64_t index) {
    int64_t len = (int64_t)list->items.len;
    if (index < -len || index >= len) {
        char *msg = str_printf("%s: index %lld out of range for list of length %lld",
                               word, (long long)index, (long long)len);
        ct_trace_error(vm, msg);
        free(msg);
    }
    return (size_t)(index < 0 ? index + len : index);
}

/* Decode one UTF-8 code point starting at *p; advances *p.  Invalid bytes
 * decode as themselves so scanning always makes progress. */
static uint32_t utf8_next(const unsigned char **p) {
    const unsigned char *s = *p;
    uint32_t c = s[0];
    int extra = 0;
    if (c >= 0xF0 && c <= 0xF4) { c &= 0x07; extra = 3; }
    else if (c >= 0xE0) { c &= 0x0F; extra = 2; }
    else if (c >= 0xC2 && c < 0xE0) { c &= 0x1F; extra = 1; }
    else { *p = s + 1; return c; }
    for (int i = 1; i <= extra; i++) {
        if ((s[i] & 0xC0) != 0x80) { *p = s + 1; return s[0]; }
        c = (c << 6) | (s[i] & 0x3F);
    }
    *p = s + extra + 1;
    return c;
}

/* The Unicode White_Space property, frozen as part of the L2 spec. */
static bool l2_is_whitespace_cp(uint32_t c) {
    return (c >= 0x09 && c <= 0x0D) || c == 0x20 || c == 0x85 || c == 0xA0 || c == 0x1680 ||
           (c >= 0x2000 && c <= 0x200A) || c == 0x2028 || c == 0x2029 || c == 0x202F ||
           c == 0x205F || c == 0x3000;
}

static size_t utf8_codepoint_count(const char *text) {
    size_t count = 0;
    for (const unsigned char *p = (const unsigned char *)text; *p; p++) {
        if ((*p & 0xC0) != 0x80) {
            count++;
        }
    }
    return count;
}

/* L2 identifier rule: first code point is an ASCII letter, `_`, or any code
 * point >= U+0080; later ones may also be ASCII digits.  In UTF-8 every
 * byte of a non-ASCII code point is >= 0x80, so a byte test is exact. */
static bool l2_is_identifier(const char *text) {
    if (!text || !*text) {
        return false;
    }
    for (const unsigned char *p = (const unsigned char *)text; *p; p++) {
        unsigned char c = *p;
        bool ok = c == '_' || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c >= 0x80 ||
                  (p != (const unsigned char *)text && c >= '0' && c <= '9');
        if (!ok) {
            return false;
        }
    }
    return true;
}

/* try-string>int parser.  The C VM has 64-bit integers, so values outside
 * that range fail to parse (the Python VM uses arbitrary precision). */
static bool l2_parse_int(const char *text, int64_t *out) {
    const unsigned char *start = (const unsigned char *)text;
    const unsigned char *end = start + strlen(text);
    /* trim leading White_Space */
    while (start < end) {
        const unsigned char *next = start;
        uint32_t c = utf8_next(&next);
        if (!l2_is_whitespace_cp(c)) break;
        start = next;
    }
    /* trim trailing White_Space: scan forward remembering the last non-space end */
    const unsigned char *last_end = start;
    for (const unsigned char *p = start; p < end;) {
        uint32_t c = utf8_next(&p);
        if (!l2_is_whitespace_cp(c)) last_end = p;
    }
    end = last_end;
    if (start >= end) return false;
    bool negative = false;
    if (*start == '+' || *start == '-') {
        negative = *start == '-';
        start++;
    }
    int base = 10;
    if (end - start >= 2 && start[0] == '0') {
        char k = (char)(start[1] | 0x20);
        if (k == 'x') base = 16;
        else if (k == 'b') base = 2;
        else if (k == 'o') base = 8;
        if (base != 10) start += 2;
    }
    if (start >= end) return false;
    unsigned __int128 value = 0;
    bool previous_underscore = true;
    for (const unsigned char *p = start; p < end; p++) {
        unsigned char c = *p;
        if (c == '_') {
            if (previous_underscore) return false;
            previous_underscore = true;
            continue;
        }
        int digit = -1;
        if (c >= '0' && c <= '9') digit = c - '0';
        else if ((c | 0x20) >= 'a' && (c | 0x20) <= 'f') digit = (c | 0x20) - 'a' + 10;
        if (digit < 0 || digit >= base) return false;
        value = value * (unsigned)base + (unsigned)digit;
        if (value > (unsigned __int128)INT64_MAX + 1) return false;
        previous_underscore = false;
    }
    if (previous_underscore) return false;
    if (!negative && value > (unsigned __int128)INT64_MAX) return false;
    *out = negative ? (int64_t)(-(__int128)value) : (int64_t)value;
    return true;
}

static void ct_intrinsic_nil(CompileTimeVM *vm) {
    ct_stack_push(&vm->stack, ct_make_nil());
}

static void ct_intrinsic_nilp(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    ct_stack_push(&vm->stack, ct_make_int(v.kind == CT_NIL));
}

static void ct_intrinsic_string_equal(CompileTimeVM *vm) {
    char *b = ct_strict_pop_string(vm, "string-equal?");
    char *a = ct_strict_pop_string(vm, "string-equal?");
    bool eq = strcmp(a, b) == 0;
    free(a);
    free(b);
    ct_stack_push(&vm->stack, ct_make_int(eq));
}

static void ct_intrinsic_string_length(CompileTimeVM *vm) {
    char *s = ct_strict_pop_string(vm, "string-length");
    ct_stack_push(&vm->stack, ct_make_int((int64_t)utf8_codepoint_count(s)));
    free(s);
}

static void ct_intrinsic_string_append(CompileTimeVM *vm) {
    char *b = ct_strict_pop_string(vm, "string-append");
    char *a = ct_strict_pop_string(vm, "string-append");
    char *out = str_printf("%s%s", a, b);
    free(a);
    free(b);
    ct_stack_push(&vm->stack, ct_make_str(out));
    free(out);
}

static void ct_intrinsic_try_string_to_int(CompileTimeVM *vm) {
    char *s = ct_strict_pop_string(vm, "try-string>int");
    int64_t out = 0;
    bool ok = l2_parse_int(s, &out);
    ct_stack_push(&vm->stack, ct_make_int(ok ? out : 0));
    ct_stack_push(&vm->stack, ct_make_int(ok ? 1 : 0));
    free(s);
}

static void ct_intrinsic_int_to_string(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    if (v.kind != CT_INT) {
        ct_value_type_error(vm, "int>string", "an integer", v);
    }
    char *out = str_printf("%lld", (long long)v.as.i64);
    ct_stack_push(&vm->stack, ct_make_str(out));
    free(out);
}

static void ct_intrinsic_identifierp(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    const char *text = NULL;
    if (v.kind == CT_STR) text = v.as.str;
    else if (v.kind == CT_TOKEN) text = v.as.token.lexeme;
    ct_stack_push(&vm->stack, ct_make_int(text && l2_is_identifier(text)));
}

static void ct_intrinsic_list_new(CompileTimeVM *vm) {
    CtList *list = ct_list_new();
    ct_stack_push(&vm->stack, ct_make_list(list));
}

static void ct_intrinsic_list_append(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    CtList *list = ct_strict_pop_list(vm, "list-append");
    VEC_PUSH(&list->items, v);
    ct_stack_push(&vm->stack, ct_make_list(list));
}

/* list-pop / list-pop-front: list -- list value found? */
static void ct_intrinsic_list_pop(CompileTimeVM *vm) {
    CtList *list = ct_strict_pop_list(vm, "list-pop");
    ct_stack_push(&vm->stack, ct_make_list(list));
    if (!list->items.len) {
        ct_stack_push(&vm->stack, ct_make_nil());
        ct_stack_push(&vm->stack, ct_make_int(0));
        return;
    }
    CtValue v = VEC_POP(&list->items);
    ct_stack_push(&vm->stack, v);
    ct_stack_push(&vm->stack, ct_make_int(1));
}

static void ct_intrinsic_list_pop_front(CompileTimeVM *vm) {
    CtList *list = ct_strict_pop_list(vm, "list-pop-front");
    ct_stack_push(&vm->stack, ct_make_list(list));
    if (!list->items.len) {
        ct_stack_push(&vm->stack, ct_make_nil());
        ct_stack_push(&vm->stack, ct_make_int(0));
        return;
    }
    CtValue v = list->items.data[0];
    memmove(&list->items.data[0], &list->items.data[1], (list->items.len - 1) * sizeof(CtValue));
    list->items.len--;
    ct_stack_push(&vm->stack, v);
    ct_stack_push(&vm->stack, ct_make_int(1));
}

static void ct_intrinsic_list_peek_front(CompileTimeVM *vm) {
    CtList *list = ct_strict_pop_list(vm, "list-peek-front");
    if (!list->items.len) {
        ct_trace_error(vm, "list-peek-front: list is empty");
    }
    ct_stack_push(&vm->stack, list->items.data[0]);
}

static void ct_intrinsic_list_push_front(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    CtList *list = ct_strict_pop_list(vm, "list-push-front");
    if (list->items.len + 1 > list->items.cap) {
        size_t new_cap = list->items.cap ? list->items.cap * 2 : 8;
        list->items.data = xrealloc(list->items.data, new_cap * sizeof(CtValue));
        list->items.cap = new_cap;
    }
    memmove(&list->items.data[1], &list->items.data[0], list->items.len * sizeof(CtValue));
    list->items.data[0] = v;
    list->items.len++;
    ct_stack_push(&vm->stack, ct_make_list(list));
}

static void ct_intrinsic_list_length(CompileTimeVM *vm) {
    CtList *list = ct_strict_pop_list(vm, "list-length");
    ct_stack_push(&vm->stack, ct_make_int((int64_t)list->items.len));
}

static void ct_intrinsic_list_empty(CompileTimeVM *vm) {
    CtList *list = ct_strict_pop_list(vm, "list-empty?");
    ct_stack_push(&vm->stack, ct_make_int(list->items.len == 0));
}

static void ct_intrinsic_list_get(CompileTimeVM *vm) {
    int64_t idx = ct_strict_pop_index(vm, "list-get");
    CtList *list = ct_strict_pop_list(vm, "list-get");
    size_t at = ct_checked_index(vm, "list-get", list, idx);
    ct_stack_push(&vm->stack, list->items.data[at]);
}

static void ct_intrinsic_list_set(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    int64_t idx = ct_strict_pop_index(vm, "list-set");
    CtList *list = ct_strict_pop_list(vm, "list-set");
    size_t at = ct_checked_index(vm, "list-set", list, idx);
    list->items.data[at] = v;
    ct_stack_push(&vm->stack, ct_make_list(list));
}

static void ct_intrinsic_list_extend(CompileTimeVM *vm) {
    CtList *source = ct_strict_pop_list(vm, "list-extend");
    CtList *target = ct_strict_pop_list(vm, "list-extend");
    size_t count = source->items.len;  /* snapshot: self-extension appends once */
    for (size_t i = 0; i < count; i++) {
        VEC_PUSH(&target->items, source->items.data[i]);
    }
    ct_stack_push(&vm->stack, ct_make_list(target));
}

static void ct_intrinsic_list_reverse(CompileTimeVM *vm) {
    CtList *list = ct_strict_pop_list(vm, "list-reverse");
    if (list->items.len > 1) {
        size_t i = 0;
        size_t j = list->items.len - 1;
        while (i < j) {
            CtValue tmp = list->items.data[i];
            list->items.data[i] = list->items.data[j];
            list->items.data[j] = tmp;
            i++;
            j--;
        }
    }
    ct_stack_push(&vm->stack, ct_make_list(list));
}

static void ct_intrinsic_list_clear(CompileTimeVM *vm) {
    CtList *list = ct_strict_pop_list(vm, "list-clear");
    list->items.len = 0;
    ct_stack_push(&vm->stack, ct_make_list(list));
}

static void ct_intrinsic_list_last(CompileTimeVM *vm) {
    CtList *list = ct_strict_pop_list(vm, "list-last");
    if (!list->items.len) {
        ct_trace_error(vm, "list-last: list is empty");
    }
    ct_stack_push(&vm->stack, list->items.data[list->items.len - 1]);
}

/* list-clone: list -- new-list (shallow). */
static void ct_intrinsic_list_clone(CompileTimeVM *vm) {
    CtList *list = ct_strict_pop_list(vm, "list-clone");
    CtList *out = ct_list_new();
    for (size_t i = 0; i < list->items.len; i++) {
        VEC_PUSH(&out->items, list->items.data[i]);
    }
    ct_stack_push(&vm->stack, ct_make_list(out));
}

static void ct_intrinsic_map_new(CompileTimeVM *vm) {
    CtMap *map = ct_map_new();
    ct_stack_push(&vm->stack, ct_make_map(map));
}

static void ct_intrinsic_map_set(CompileTimeVM *vm) {
    CtValue val = ct_pop(vm);
    CtValue key = ct_pop(vm);
    CtMap *map = ct_strict_pop_map(vm, "map-set");
    ct_check_map_key(vm, "map-set", key);
    ct_map_put(map, key, val);
    ct_stack_push(&vm->stack, ct_make_map(map));
}

/* map-get: map key -- value found? */
static void ct_intrinsic_map_get(CompileTimeVM *vm) {
    CtValue key = ct_pop(vm);
    CtMap *map = ct_strict_pop_map(vm, "map-get");
    ct_check_map_key(vm, "map-get", key);
    CtValue out = ct_make_nil();
    bool ok = ct_map_lookup(map, key, &out);
    ct_stack_push(&vm->stack, ok ? out : ct_make_nil());
    ct_stack_push(&vm->stack, ct_make_int(ok));
}

/* map-has?: map key -- found? */
static void ct_intrinsic_map_has(CompileTimeVM *vm) {
    CtValue key = ct_pop(vm);
    CtMap *map = ct_strict_pop_map(vm, "map-has?");
    ct_check_map_key(vm, "map-has?", key);
    CtValue out = ct_make_nil();
    ct_stack_push(&vm->stack, ct_make_int(ct_map_lookup(map, key, &out)));
}

static void ct_intrinsic_map_length(CompileTimeVM *vm) {
    CtMap *map = ct_strict_pop_map(vm, "map-length");
    ct_stack_push(&vm->stack, ct_make_int((int64_t)map->len));
}

static void ct_intrinsic_map_empty(CompileTimeVM *vm) {
    CtMap *map = ct_strict_pop_map(vm, "map-empty?");
    ct_stack_push(&vm->stack, ct_make_int(map->len == 0));
}

static void ct_intrinsic_map_clear(CompileTimeVM *vm) {
    CtMap *map = ct_strict_pop_map(vm, "map-clear");
    ct_map_clear_entries(map);
    ct_stack_push(&vm->stack, ct_make_map(map));
}

static void ct_intrinsic_map_keys(CompileTimeVM *vm) {
    CtMap *map = ct_strict_pop_map(vm, "map-keys");
    CtList *out = ct_list_new();
    for (size_t i = 0; i < map->len; i++) {
        VEC_PUSH(&out->items, map->keys[i]);
    }
    ct_stack_push(&vm->stack, ct_make_list(out));
}

static void ct_intrinsic_map_values(CompileTimeVM *vm) {
    CtMap *map = ct_strict_pop_map(vm, "map-values");
    CtList *out = ct_list_new();
    for (size_t i = 0; i < map->len; i++) {
        VEC_PUSH(&out->items, map->values[i]);
    }
    ct_stack_push(&vm->stack, ct_make_list(out));
}

static void ct_intrinsic_string_contains(CompileTimeVM *vm) {
    char *needle = ct_strict_pop_string(vm, "string-contains?");
    char *text = ct_strict_pop_string(vm, "string-contains?");
    bool found = strstr(text, needle) != NULL;
    free(text);
    free(needle);
    ct_stack_push(&vm->stack, ct_make_int(found));
}

static void ct_intrinsic_string_starts_with(CompileTimeVM *vm) {
    char *prefix = ct_strict_pop_string(vm, "string-starts-with?");
    char *text = ct_strict_pop_string(vm, "string-starts-with?");
    size_t plen = strlen(prefix);
    bool ok = strncmp(text, prefix, plen) == 0;
    free(text);
    free(prefix);
    ct_stack_push(&vm->stack, ct_make_int(ok));
}

static void ct_intrinsic_string_ends_with(CompileTimeVM *vm) {
    char *suffix = ct_strict_pop_string(vm, "string-ends-with?");
    char *text = ct_strict_pop_string(vm, "string-ends-with?");
    size_t slen = strlen(suffix);
    size_t tlen = strlen(text);
    bool ok = false;
    if (slen <= tlen) {
        ok = strcmp(text + (tlen - slen), suffix) == 0;
    }
    free(text);
    free(suffix);
    ct_stack_push(&vm->stack, ct_make_int(ok));
}

/* Strict token pop for the Token accessor words: strings are not coerced. */
static Token ct_strict_pop_token(CompileTimeVM *vm, const char *word) {
    CtValue v = ct_pop(vm);
    if (v.kind != CT_TOKEN) {
        ct_value_type_error(vm, word, "a token", v);
    }
    return v.as.token;
}

static void ct_intrinsic_token_lexeme(CompileTimeVM *vm) {
    Token tok = ct_strict_pop_token(vm, "token-lexeme");
    ct_stack_push(&vm->stack, ct_make_str(tok.lexeme));
}

/* token-with-lexeme: token lexeme -- new-token.  The C VM has no source
 * provenance, so the new token keeps the template's coordinates. */
static void ct_intrinsic_token_with_lexeme(CompileTimeVM *vm) {
    char *lexeme = ct_strict_pop_string(vm, "token-with-lexeme");
    if (!*lexeme) {
        ct_trace_error(vm, "token-with-lexeme: lexeme must not be empty");
    }
    Token tok = ct_strict_pop_token(vm, "token-with-lexeme");
    tok.lexeme = lexeme;
    ct_stack_push(&vm->stack, ct_make_token(tok));
}

static void ct_intrinsic_token_line(CompileTimeVM *vm) {
    Token tok = ct_strict_pop_token(vm, "token-line");
    ct_stack_push(&vm->stack, ct_make_int(tok.line));
}

/* Columns are 1-based in the CT API (the tokenizer counts from 0). */
static void ct_intrinsic_token_column(CompileTimeVM *vm) {
    Token tok = ct_strict_pop_token(vm, "token-column");
    ct_stack_push(&vm->stack, ct_make_int(tok.column + 1));
}

static void ct_intrinsic_parser_consume(CompileTimeVM *vm) {
    Token tok = parser_next_token(vm->parser);
    if (!tok.lexeme) {
        ct_trace_error(vm, "ct-parser-consume: unexpected end of input");
    }
    ct_stack_push(&vm->stack, ct_make_token(tok));
}

static void ct_intrinsic_parser_peek(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    if (v.kind != CT_INT) {
        char *msg = str_printf("ct-parser-peek expects an integer offset, got %s", ct_kind_name(v.kind));
        ct_trace_error(vm, msg);
    }
    if (v.as.i64 < 0) {
        char *msg = str_printf("ct-parser-peek: offset must not be negative, got %lld", (long long)v.as.i64);
        ct_trace_error(vm, msg);
    }
    Parser *parser = vm->parser;
    size_t idx = parser->pos + (size_t)v.as.i64;
    ensure_tokens(parser, idx);
    if (idx >= parser->tokens.len) {
        ct_stack_push(&vm->stack, ct_make_nil());
        return;
    }
    ct_stack_push(&vm->stack, ct_make_token(parser->tokens.data[idx]));
}

static void ct_intrinsic_parser_eof(CompileTimeVM *vm) {
    ct_stack_push(&vm->stack, ct_make_int(parser_eof(vm->parser) ? 1 : 0));
}

static void ct_intrinsic_parser_inject(CompileTimeVM *vm) {
    CtList *list = ct_pop_list(vm);
    for (size_t i = 0; i < list->items.len; i++) {
        if (list->items.data[i].kind != CT_TOKEN) {
            char *msg = str_printf("ct-parser-inject: element %zu is %s, expected token",
                                   i, ct_kind_name(list->items.data[i].kind));
            ct_trace_error(vm, msg);
        }
    }
    TokenVec injected;
    VEC_INIT(&injected);
    for (size_t i = 0; i < list->items.len; i++) {
        VEC_PUSH(&injected, list->items.data[i].as.token);
    }
    parser_inject_tokens(vm->parser, &injected);
}

static void ct_intrinsic_set_token_hook(CompileTimeVM *vm) {
    char *name = ct_pop_str(vm);
    if (vm->parser->token_hook) {
        free(vm->parser->token_hook);
    }
    vm->parser->token_hook = name;
}

static void ct_intrinsic_clear_token_hook(CompileTimeVM *vm) {
    if (vm->parser->token_hook) {
        free(vm->parser->token_hook);
        vm->parser->token_hook = NULL;
    }
}

static void ct_intrinsic_parse_error(CompileTimeVM *vm) {
    char *msg = ct_pop_str(vm);
    fprintf(stderr, "[error] %s\n", msg);
    free(msg);
    exit(1);
}

static StrVec *ct_output_buffer(StrVec **slot) {
    if (!*slot) {
        *slot = (StrVec *)xmalloc(sizeof(StrVec));
        VEC_INIT(*slot);
    }
    return *slot;
}

/* fragment -- */
static void ct_output_append(CompileTimeVM *vm, StrVec **slot, const char *word) {
    CtValue v = ct_pop(vm);
    if (v.kind != CT_STR) {
        ct_value_type_error(vm, word, "a string", v);
    }
    VEC_PUSH(ct_output_buffer(slot), str_dup(v.as.str));
}

/* fragment-list -- : validated in full before the buffer changes. */
static void ct_output_set(CompileTimeVM *vm, StrVec **slot, const char *word) {
    CtList *list = ct_strict_pop_list(vm, word);
    for (size_t i = 0; i < list->items.len; i++) {
        if (list->items.data[i].kind != CT_STR) {
            ct_value_type_error(vm, word, "a list of strings", list->items.data[i]);
        }
    }
    StrVec *buffer = ct_output_buffer(slot);
    buffer->len = 0;
    for (size_t i = 0; i < list->items.len; i++) {
        VEC_PUSH(buffer, str_dup(list->items.data[i].as.str));
    }
}

static void ct_intrinsic_output_append_text_preamble(CompileTimeVM *vm) {
    ct_output_append(vm, &vm->parser->custom_prelude, "ct-output-append-text-preamble");
}

static void ct_intrinsic_output_set_text_preamble(CompileTimeVM *vm) {
    ct_output_set(vm, &vm->parser->custom_prelude, "ct-output-set-text-preamble");
}

static void ct_intrinsic_output_clear_text_preamble(CompileTimeVM *vm) {
    ct_output_buffer(&vm->parser->custom_prelude)->len = 0;
}

static void ct_intrinsic_output_append_data(CompileTimeVM *vm) {
    ct_output_append(vm, &vm->parser->custom_data, "ct-output-append-data");
}

static void ct_intrinsic_output_set_data(CompileTimeVM *vm) {
    ct_output_set(vm, &vm->parser->custom_data, "ct-output-set-data");
}

static void ct_intrinsic_output_clear_data(CompileTimeVM *vm) {
    ct_output_buffer(&vm->parser->custom_data)->len = 0;
}

/* Appending keeps the standard runtime BSS layout; set and clear replace it. */
static void ct_intrinsic_output_append_bss(CompileTimeVM *vm) {
    ct_output_append(vm, &vm->parser->custom_bss, "ct-output-append-bss");
}

static void ct_intrinsic_output_set_bss(CompileTimeVM *vm) {
    ct_output_set(vm, &vm->parser->custom_bss, "ct-output-set-bss");
    vm->parser->bss_replaced = true;
}

static void ct_intrinsic_output_clear_bss(CompileTimeVM *vm) {
    ct_output_buffer(&vm->parser->custom_bss)->len = 0;
    vm->parser->bss_replaced = true;
}

/* ct-definition-prefer-l2-by-name: name -- ok?  Marks the selected
 * definition to run its L2 body at compile time instead of the name's
 * compile-time intrinsic.  A missing name (or one with no definition)
 * pushes 0. */
static void ct_intrinsic_definition_prefer_l2_by_name(CompileTimeVM *vm) {
    char *name = ct_pop_str(vm);
    Word *word = dictionary_lookup(vm->dictionary, name);
    if (!word || (!word->definition && !word->ct_definition)) {
        free(name);
        ct_stack_push(&vm->stack, ct_make_int(0));
        return;
    }
    if (word->runtime_only) {
        fprintf(stderr, "[error] ct-definition-prefer-l2-by-name: '%s' is runtime-only and cannot run at compile time\n", word->name);
        exit(1);
    }
    word->compile_time_override = true;
    free(name);
    ct_stack_push(&vm->stack, ct_make_int(1));
}

static void ct_intrinsic_ct_flag(CompileTimeVM *vm) {
    ct_stack_push(&vm->stack, ct_make_int(1));
}

static CtList *ct_list_from_tokens(const char **tokens, size_t count) {
    CtList *list = ct_list_new();
    for (size_t i = 0; i < count; i++) {
        VEC_PUSH(&list->items, ct_make_str(tokens[i]));
    }
    return list;
}

static void ct_intrinsic_parser_current(CompileTimeVM *vm) {
    if (vm->parser && vm->parser->has_last_token) {
        ct_stack_push(&vm->stack, ct_make_token(vm->parser->last_token));
        return;
    }
    ct_stack_push(&vm->stack, ct_make_nil());
}

/* ---- Syntax values, quotations, and macros ---------------- */

static CtValue ct_make_syntax(CtList *list) {
    CtValue v = {0};
    v.kind = CT_SYNTAX;
    v.as.list = list;
    return v;
}

static Token token_copy_with(const Token *source, const char *lexeme) {
    Token tok = {0};
    tok.lexeme = str_dup(lexeme);
    if (source) {
        tok.line = source->line;
        tok.column = source->column;
    }
    return tok;
}

static void ct_list_push_token(CtList *list, Token tok) {
    VEC_PUSH(&list->items, ct_make_token(tok));
}

/* Append one `$( )` value to a quotation being built. */
static void ct_unquote_append(CompileTimeVM *vm, CtList *out, CtValue value, const QuotePart *part) {
    CtValue single[1];
    CtValue *items = single;
    size_t count = 1;
    single[0] = value;
    if (value.kind == CT_SYNTAX || value.kind == CT_LIST) {
        items = value.as.list->items.data;
        count = value.as.list->items.len;
    }
    bool pasting = (part->prefix && part->prefix[0]) || (part->suffix && part->suffix[0]);
    if (pasting && count != 1) {
        ct_trace_error(vm, "`$( )` pastes text onto its value, which needs exactly one token");
    }
    for (size_t i = 0; i < count; i++) {
        CtValue item = items[i];
        char numbuf[32];
        const char *text = NULL;
        if (item.kind == CT_TOKEN) {
            text = item.as.token.lexeme;
        } else if (item.kind == CT_INT) {
            snprintf(numbuf, sizeof(numbuf), "%lld", (long long)item.as.i64);
            text = numbuf;
        } else if (item.kind == CT_STR) {
            text = item.as.str;
            bool blank = !text[0];
            for (const char *c = text; *c; c++) {
                if (isspace((unsigned char)*c)) blank = true;
            }
            if (blank) {
                ct_trace_error(vm, "an unquoted string must be one non-empty word");
            }
        } else {
            char msg[128];
            snprintf(msg, sizeof(msg), "cannot unquote a %s; use a token, syntax, string, integer, or a list of them",
                     ct_kind_name(item.kind));
            ct_trace_error(vm, msg);
        }
        char *lexeme = str_printf("%s%s%s", pasting && part->prefix ? part->prefix : "", text,
                                  pasting && part->suffix ? part->suffix : "");
        Token tok = {0};
        tok.lexeme = lexeme;
        tok.line = part->line;
        tok.column = part->column;
        ct_list_push_token(out, tok);
    }
}

static bool ct_lookup_binding(CompileTimeVM *vm, const char *name, CtValue *out) {
    for (size_t i = vm->binding_len; i > 0; i--) {
        if (strcmp(vm->binding_names[i - 1], name) == 0) {
            *out = vm->binding_values[i - 1];
            return true;
        }
    }
    return false;
}

static CtValue ct_eval_quotation(CompileTimeVM *vm, QuoteTemplate *tpl) {
    CtList *out = ct_list_new();
    size_t base = vm->unquote_values.len;
    if (tpl->unquotes) {
        if (vm->unquote_values.len < tpl->unquotes) {
            ct_trace_error(vm, "quotation is missing its `$( )` values");
        }
        base = vm->unquote_values.len - tpl->unquotes;
    }
    size_t next = base;
    for (size_t i = 0; i < tpl->len; i++) {
        QuotePart *part = &tpl->parts[i];
        if (part->kind == 0) {
            Token tok = {0};
            tok.lexeme = str_dup(part->lexeme);
            tok.line = part->line;
            tok.column = part->column;
            ct_list_push_token(out, tok);
        } else if (part->kind == 1) {
            CtValue value;
            if (!ct_lookup_binding(vm, part->lexeme, &value)) {
                char msg[256];
                snprintf(msg, sizeof(msg), "unbound '$%s' in quotation", part->lexeme);
                ct_trace_error(vm, msg);
            }
            if (value.kind == CT_TOKEN) {
                ct_list_push_token(out, token_copy_with(&value.as.token, value.as.token.lexeme));
            } else if (value.kind == CT_SYNTAX) {
                for (size_t j = 0; j < value.as.list->items.len; j++) {
                    Token *t = &value.as.list->items.data[j].as.token;
                    ct_list_push_token(out, token_copy_with(t, t->lexeme));
                }
            } else {
                char msg[256];
                snprintf(msg, sizeof(msg), "'$%s' is a %s; only a token or syntax can be spliced",
                         part->lexeme, ct_kind_name(value.kind));
                ct_trace_error(vm, msg);
            }
        } else {
            ct_unquote_append(vm, out, vm->unquote_values.data[next++], part);
        }
    }
    vm->unquote_values.len = base;
    return ct_make_syntax(out);
}

static CtList *ct_pop_syntax(CompileTimeVM *vm, const char *word) {
    CtValue v = ct_pop(vm);
    if (v.kind != CT_SYNTAX) {
        ct_value_type_error(vm, word, "a syntax value", v);
    }
    return v.as.list;
}

static void parser_inject_tokens(Parser *parser, TokenVec *injected);

/* emit [* | syntax] -> [*]: add the tokens to the running macro's output, or
 * (outside a macro) to the input read next. */
static void ct_intrinsic_emit(CompileTimeVM *vm) {
    CtList *syntax = ct_pop_syntax(vm, "emit");
    TokenVec local;
    VEC_INIT(&local);
    TokenVec *out = vm->emit_out ? vm->emit_out : &local;
    for (size_t i = 0; i < syntax->items.len; i++) {
        Token *t = &syntax->items.data[i].as.token;
        VEC_PUSH(out, token_copy_with(t, t->lexeme));
    }
    if (!vm->emit_out) {
        parser_inject_tokens(vm->parser, &local);
    }
}

static void ct_intrinsic_list_to_syntax(CompileTimeVM *vm) {
    CtList *list = ct_strict_pop_list(vm, "list>syntax");
    CtList *out = ct_list_new();
    for (size_t i = 0; i < list->items.len; i++) {
        if (list->items.data[i].kind != CT_TOKEN) {
            ct_value_type_error(vm, "list>syntax", "a list of tokens", list->items.data[i]);
        }
        VEC_PUSH(&out->items, list->items.data[i]);
    }
    ct_stack_push(&vm->stack, ct_make_syntax(out));
}

static void ct_intrinsic_syntax_to_list(CompileTimeVM *vm) {
    CtList *syntax = ct_pop_syntax(vm, "syntax>list");
    CtList *out = ct_list_new();
    for (size_t i = 0; i < syntax->items.len; i++) {
        VEC_PUSH(&out->items, syntax->items.data[i]);
    }
    ct_stack_push(&vm->stack, ct_make_list(out));
}

static void ct_intrinsic_syntax_length(CompileTimeVM *vm) {
    CtList *syntax = ct_pop_syntax(vm, "syntax-length");
    ct_stack_push(&vm->stack, ct_make_int((int64_t)syntax->items.len));
}

#define L2_UNQUOTE_BEGIN "\001unquote-begin"
#define L2_UNQUOTE_END "\001unquote-end"
#define L2_QUOTE_MARKER "\001quote:"

static void ct_intrinsic_unquote_begin(CompileTimeVM *vm) {
    VEC_PUSH(&vm->unquote_depths, (int)vm->stack.len);
}

/* The value is the top of the stack; the expression may consume one value
 * below it (net change 0 or +1), as in the Python compiler. */
static void ct_intrinsic_unquote_end(CompileTimeVM *vm) {
    int depth = vm->unquote_depths.len ? vm->unquote_depths.data[--vm->unquote_depths.len] : (int)vm->stack.len - 1;
    long left = (long)vm->stack.len - depth;
    if ((left != 0 && left != 1) || vm->stack.len == 0) {
        ct_trace_error(vm, "`$( )` must leave one value: it may consume at most one value and must push one");
    }
    VEC_PUSH(&vm->unquote_values, ct_pop(vm));
}

static Word *register_ct_intrinsic(Dictionary *dict, const char *name, CompileTimeIntrinsic fn) {
    Word *word = dictionary_lookup(dict, name);
    if (!word) {
        word = (Word *)xmalloc(sizeof(Word));
        memset(word, 0, sizeof(Word));
        word->name = str_dup(name);
        dictionary_register(dict, word);
    }
    word->ct_intrinsic = fn;
    word->compile_only = true;
    return word;
}

/* ---- Control frames and ops built by compile-time code -----------------
 * The Python compiler lets library code define control structures
 * (stdlib/control.sl): words make labels, emit ops, and push frames whose
 * "close_ops" the parser emits at the matching `end`.  A frame is a map; a
 * built-in frame is shown to CT code as a map of its type and labels. */

static CtValue ct_str_key(const char *key) {
    CtValue v = {0};
    v.kind = CT_STR;
    v.as.str = (char *)key;
    return v;
}

static CtMap *ct_map_copy(const CtMap *src) {
    CtMap *map = ct_map_new();
    for (size_t i = 0; i < src->len; i++) {
        ct_map_put(map, src->keys[i], src->values[i]);
    }
    return map;
}

static CtValue ct_map_get_or_nil(CtMap *map, const char *key) {
    CtValue out;
    if (ct_map_lookup(map, ct_str_key(key), &out)) {
        return out;
    }
    return ct_make_nil();
}

/* Build the op named `name` from `data`; false when the pair is unusable. */
static bool ct_build_op(const char *name, CtValue data, Op *out) {
    Op op = {0};
    if (strcmp(name, "label") == 0 || strcmp(name, "jump") == 0 || strcmp(name, "branch_zero") == 0) {
        if (data.kind != CT_STR) {
            return false;
        }
        op.kind = name[0] == 'l' ? OP_LABEL : name[0] == 'j' ? OP_JUMP : OP_BRANCH_ZERO;
        op.data.label = str_dup(data.as.str ? data.as.str : "");
    } else if (strcmp(name, "for_begin") == 0 || strcmp(name, "for_end") == 0) {
        if (data.kind != CT_MAP) {
            return false;
        }
        CtValue loop = ct_map_get_or_nil(data.as.map, "loop");
        CtValue end = ct_map_get_or_nil(data.as.map, "end");
        if (loop.kind != CT_STR || end.kind != CT_STR) {
            return false;
        }
        op.kind = name[4] == 'b' ? OP_FOR_BEGIN : OP_FOR_END;
        op.data.loop.loop = str_dup(loop.as.str);
        op.data.loop.end = str_dup(end.as.str);
    } else if (strcmp(name, "word") == 0 || strcmp(name, "word_ptr") == 0) {
        if (data.kind != CT_STR || !data.as.str || !*data.as.str) {
            return false;
        }
        op.kind = name[4] == '_' ? OP_WORD_PTR : OP_WORD;
        op.data.word = str_dup(data.as.str);
    } else if (strcmp(name, "literal") == 0) {
        op.kind = OP_LITERAL;
        if (data.kind == CT_INT) {
            op.lit_kind = LIT_INT;
            op.data.i64 = data.as.i64;
        } else if (data.kind == CT_STR) {
            op.lit_kind = LIT_STRING;
            op.data.str = str_dup(data.as.str ? data.as.str : "");
        } else {
            return false;
        }
    } else {
        return false;
    }
    *out = op;
    return true;
}

static void ct_emit_built_op(CompileTimeVM *vm, const char *word, const char *name, CtValue data) {
    Op op;
    if (!ct_build_op(name, data, &op)) {
        char *msg = str_printf("%s: cannot build a '%s' op from a %s", word, name, ct_kind_name(data.kind));
        ct_trace_error(vm, msg);
        free(msg);
        return;
    }
    parser_emit_op(vm->parser, op);
}

static char *ct_pop_op_name(CompileTimeVM *vm, const char *word) {
    char *name = ct_strict_pop_string(vm, word);
    if (!*name) {
        char *msg = str_printf("%s: op name must not be empty", word);
        ct_trace_error(vm, msg);
        free(msg);
    }
    return name;
}

/* ct-label-new [* | prefix] -> [* | label] */
static void ct_intrinsic_label_new(CompileTimeVM *vm) {
    char *prefix = ct_strict_pop_string(vm, "ct-label-new");
    char *label = parser_new_label(vm->parser, prefix);
    ct_stack_push(&vm->stack, ct_make_str(label));
    free(label);
    free(prefix);
}

/* ct-op-emit [*, op | data] -> [*] */
static void ct_intrinsic_op_emit(CompileTimeVM *vm) {
    CtValue data = ct_pop(vm);
    char *name = ct_pop_op_name(vm, "ct-op-emit");
    ct_emit_built_op(vm, "ct-op-emit", name, data);
    free(name);
}

/* ct-control-frame-new [* | type] -> [* | frame] */
static void ct_intrinsic_control_frame_new(CompileTimeVM *vm) {
    char *type = ct_strict_pop_string(vm, "ct-control-frame-new");
    CtMap *frame = ct_map_new();
    ct_map_put(frame, ct_str_key("type"), ct_make_str(type));
    free(type);
    ct_stack_push(&vm->stack, ct_make_map(frame));
}

/* ct-control-set [*, frame, key | value] -> [* | frame] */
static void ct_intrinsic_control_set(CompileTimeVM *vm) {
    CtValue value = ct_pop(vm);
    char *key = ct_strict_pop_string(vm, "ct-control-set");
    CtMap *frame = ct_strict_pop_map(vm, "ct-control-set");
    ct_map_put(frame, ct_str_key(key), value);
    free(key);
    ct_stack_push(&vm->stack, ct_make_map(frame));
}

/* ct-control-get [*, frame | key] -> [*, value | found?] */
static void ct_intrinsic_control_get(CompileTimeVM *vm) {
    char *key = ct_strict_pop_string(vm, "ct-control-get");
    CtMap *frame = ct_strict_pop_map(vm, "ct-control-get");
    CtValue value;
    bool found = ct_map_lookup(frame, ct_str_key(key), &value);
    free(key);
    ct_stack_push(&vm->stack, found ? value : ct_make_nil());
    ct_stack_push(&vm->stack, ct_make_int(found ? 1 : 0));
}

/* ct-control-get? [*, frame | key] -> [* | value-or-nil] */
static void ct_intrinsic_control_get_or_nil(CompileTimeVM *vm) {
    char *key = ct_strict_pop_string(vm, "ct-control-get?");
    CtMap *frame = ct_strict_pop_map(vm, "ct-control-get?");
    ct_stack_push(&vm->stack, ct_map_get_or_nil(frame, key));
    free(key);
}

/* ct-control-add-close-op [*, frame, op | data] -> [* | frame] */
static void ct_intrinsic_control_add_close_op(CompileTimeVM *vm) {
    CtValue data = ct_pop(vm);
    char *name = ct_pop_op_name(vm, "ct-control-add-close-op");
    CtMap *frame = ct_strict_pop_map(vm, "ct-control-add-close-op");
    CtValue close_ops = ct_map_get_or_nil(frame, "close_ops");
    if (close_ops.kind == CT_NIL) {
        close_ops = ct_make_list(ct_list_new());
        ct_map_put(frame, ct_str_key("close_ops"), close_ops);
    } else if (close_ops.kind != CT_LIST) {
        ct_value_type_error(vm, "ct-control-add-close-op", "a frame whose close_ops is a list or nil", close_ops);
    }
    CtMap *descriptor = ct_map_new();
    ct_map_put(descriptor, ct_str_key("op"), ct_make_str(name));
    ct_map_put(descriptor, ct_str_key("data"), data);
    VEC_PUSH(&close_ops.as.list->items, ct_make_map(descriptor));
    free(name);
    ct_stack_push(&vm->stack, ct_make_map(frame));
}

/* The control frame at `index` as CT code sees it (a fresh map). */
static CtMap *parser_control_frame_map(Parser *parser, size_t index) {
    if (parser->control_stack[index].frame) {
        return ct_map_copy(parser->control_stack[index].frame);
    }
    CtMap *map = ct_map_new();
    ct_map_put(map, ct_str_key("type"), ct_make_str(parser->control_stack[index].type));
    const char *keys[] = {"false", "end", "begin", "loop"};
    char *labels[] = {
        parser->control_stack[index].false_label, parser->control_stack[index].end_label,
        parser->control_stack[index].begin_label, parser->control_stack[index].loop_label,
    };
    for (int i = 0; i < 4; i++) {
        if (labels[i]) {
            ct_map_put(map, ct_str_key(keys[i]), ct_make_str(labels[i]));
        }
    }
    ct_map_put(map, ct_str_key("line"), ct_make_int(parser->control_stack[index].line));
    ct_map_put(map, ct_str_key("column"), ct_make_int(parser->control_stack[index].column));
    return map;
}

/* ct-control-push [* | frame] -> [*] (pushes a shallow copy) */
static void ct_intrinsic_control_push(CompileTimeVM *vm) {
    CtMap *frame = ct_map_copy(ct_strict_pop_map(vm, "ct-control-push"));
    CtValue type = ct_map_get_or_nil(frame, "type");
    Parser *parser = vm->parser;
    parser_push_control(parser, type.kind == CT_STR && type.as.str ? type.as.str : "");
    CtValue line = ct_map_get_or_nil(frame, "line");
    CtValue column = ct_map_get_or_nil(frame, "column");
    if (line.kind != CT_INT || column.kind != CT_INT) {
        line = ct_make_int(parser->control_stack[parser->control_len - 1].line);
        column = ct_make_int(parser->control_stack[parser->control_len - 1].column);
        ct_map_put(frame, ct_str_key("line"), line);
        ct_map_put(frame, ct_str_key("column"), column);
    }
    parser->control_stack[parser->control_len - 1].line = (int)line.as.i64;
    parser->control_stack[parser->control_len - 1].column = (int)column.as.i64;
    parser->control_stack[parser->control_len - 1].frame = frame;
}

/* ct-control-pop [*] -> [* | frame] */
static void ct_intrinsic_control_pop(CompileTimeVM *vm) {
    Parser *parser = vm->parser;
    if (!parser->control_len) {
        ct_trace_error(vm, "ct-control-pop: control stack underflow");
        return;
    }
    CtMap *frame = parser_control_frame_map(parser, parser->control_len - 1);
    parser->control_len--;
    ct_stack_push(&vm->stack, ct_make_map(frame));
}

/* ct-control-peek [*] -> [* | frame-or-nil] */
static void ct_intrinsic_control_peek(CompileTimeVM *vm) {
    Parser *parser = vm->parser;
    ct_stack_push(&vm->stack, parser->control_len
        ? ct_make_map(parser_control_frame_map(parser, parser->control_len - 1))
        : ct_make_nil());
}

/* ct-control-depth [*] -> [* | n] */
static void ct_intrinsic_control_depth(CompileTimeVM *vm) {
    ct_stack_push(&vm->stack, ct_make_int((int64_t)vm->parser->control_len));
}

/* ct-parser-last-line [*] -> [* | line] */
static void ct_intrinsic_parser_last_line(CompileTimeVM *vm) {
    Parser *parser = vm->parser;
    ct_stack_push(&vm->stack, ct_make_int(parser->has_last_token ? parser->last_token.line : 0));
}

/* ct-parser-register-block-opener [* | name] -> [*].  The Python compiler
 * uses the set when it scans ahead for a block's `end`; this parser does no
 * such scan, so the name is only checked. */
static void ct_intrinsic_parser_register_block_opener(CompileTimeVM *vm) {
    free(ct_pop_op_name(vm, "ct-parser-register-block-opener"));
}

static char *ct_pop_control_override_name(CompileTimeVM *vm, const char *word) {
    char *name = ct_strict_pop_string(vm, word);
    static const char *const overridable[] = {"if", "else", "for", "while", "do", NULL};
    for (int i = 0; overridable[i]; i++) {
        if (strcmp(name, overridable[i]) == 0) {
            return name;
        }
    }
    char *msg = str_printf("%s: only if, else, for, while, and do can be overridden, got '%s'", word, name);
    free(name);
    ct_trace_error(vm, msg);
    free(msg);
    return NULL;
}

/* ct-control-register-override [* | name] -> [*] */
static void ct_intrinsic_control_register_override(CompileTimeVM *vm) {
    char *name = ct_pop_control_override_name(vm, "ct-control-register-override");
    Word *word = dictionary_lookup(vm->dictionary, name);
    if (!word) {
        word = (Word *)xmalloc(sizeof(Word));
        memset(word, 0, sizeof(Word));
        word->name = str_dup(name);
        dictionary_register(vm->dictionary, word);
    }
    word->control_override = true;
    free(name);
}

/* ct-control-unregister-override [* | name] -> [*] */
static void ct_intrinsic_control_unregister_override(CompileTimeVM *vm) {
    char *name = ct_pop_control_override_name(vm, "ct-control-unregister-override");
    Word *word = dictionary_lookup(vm->dictionary, name);
    if (word) {
        word->control_override = false;
    }
    free(name);
}

/* `end` of a frame pushed by ct-control-push: check every close op, then pop
 * the frame and emit them, so a bad descriptor changes nothing. */
static void parser_close_ct_frame(Parser *parser) {
    CtMap *frame = parser->control_stack[parser->control_len - 1].frame;
    CtValue close_ops = ct_map_get_or_nil(frame, "close_ops");
    const char *type = parser->control_stack[parser->control_len - 1].type;
    if (close_ops.kind != CT_NIL && close_ops.kind != CT_LIST) {
        fprintf(stderr, "[error] 'end' of a '%s' frame: close_ops must be a list or nil\n", type);
        exit(1);
    }
    size_t count = close_ops.kind == CT_LIST ? close_ops.as.list->items.len : 0;
    Op *staged = (Op *)xmalloc((count + 1) * sizeof(Op));
    for (size_t i = 0; i < count; i++) {
        CtValue descriptor = close_ops.as.list->items.data[i];
        CtValue name = descriptor.kind == CT_MAP ? ct_map_get_or_nil(descriptor.as.map, "op") : ct_make_nil();
        CtValue data = descriptor.kind == CT_MAP ? ct_map_get_or_nil(descriptor.as.map, "data") : ct_make_nil();
        if (name.kind != CT_STR || !ct_build_op(name.as.str, data, &staged[i])) {
            fprintf(stderr, "[error] 'end' of a '%s' frame: close_ops[%zu] is not a usable op\n", type, i);
            exit(1);
        }
    }
    parser->control_len--;
    for (size_t i = 0; i < count; i++) {
        parser_emit_op(parser, staged[i]);
    }
    free(staged);
}

/* Whether a ct-control-register-override handler replaces the built-in
 * control word `lexeme`.  The handler must be an immediate word; it never
 * falls back to the built-in silently. */
static bool parser_control_overridden(Parser *parser, const char *lexeme) {
    if (lexeme[0] != 'i' && lexeme[0] != 'e' && lexeme[0] != 'f' && lexeme[0] != 'w' && lexeme[0] != 'd') {
        return false;
    }
    Word *word = dictionary_lookup(parser->dictionary, lexeme);
    if (!word || !word->control_override) {
        return false;
    }
    if (!word->immediate || word->runtime_only) {
        fprintf(stderr, "[error] control override '%s' is registered but its handler %s\n", lexeme,
                word->runtime_only ? "is runtime-only" : "is not immediate");
        exit(1);
    }
    return true;
}

/* syntax-concat [*, left | right] -> [* | syntax] */
static void ct_intrinsic_syntax_concat(CompileTimeVM *vm) {
    CtList *right = ct_pop_syntax(vm, "syntax-concat");
    CtList *left = ct_pop_syntax(vm, "syntax-concat");
    CtList *out = ct_list_new();
    for (size_t i = 0; i < left->items.len; i++) {
        VEC_PUSH(&out->items, left->items.data[i]);
    }
    for (size_t i = 0; i < right->items.len; i++) {
        VEC_PUSH(&out->items, right->items.data[i]);
    }
    ct_stack_push(&vm->stack, ct_make_syntax(out));
}

static void ct_intrinsic_nip(CompileTimeVM *vm) {
    CtValue top = ct_pop(vm);
    ct_pop(vm);
    ct_stack_push(&vm->stack, top);
}

static void ct_intrinsic_tuck(CompileTimeVM *vm) {
    CtValue b = ct_pop(vm);
    CtValue a = ct_pop(vm);
    ct_stack_push(&vm->stack, b);
    ct_stack_push(&vm->stack, a);
    ct_stack_push(&vm->stack, b);
}

static void ct_intrinsic_rot_back(CompileTimeVM *vm) {
    CtValue c = ct_pop(vm);
    CtValue b = ct_pop(vm);
    CtValue a = ct_pop(vm);
    ct_stack_push(&vm->stack, c);
    ct_stack_push(&vm->stack, a);
    ct_stack_push(&vm->stack, b);
}

static void ct_intrinsic_2dup(CompileTimeVM *vm) {
    CtValue b = ct_pop(vm);
    CtValue a = ct_pop(vm);
    ct_stack_push(&vm->stack, a);
    ct_stack_push(&vm->stack, b);
    ct_stack_push(&vm->stack, a);
    ct_stack_push(&vm->stack, b);
}

static void ct_intrinsic_2drop(CompileTimeVM *vm) {
    ct_pop(vm);
    ct_pop(vm);
}

/* Read the rest of the input into the token buffer (for words that need the
 * stream's full length). */
static void parser_tokenize_all(Parser *parser) {
    while (!parser->tokenizer_exhausted) {
        ensure_tokens(parser, parser->tokens.len);
    }
}

/* ct-parser-get-pos [*] -> [* | pos] */
static void ct_intrinsic_parser_get_pos(CompileTimeVM *vm) {
    ct_stack_push(&vm->stack, ct_make_int((int64_t)vm->parser->pos));
}

/* ct-parser-remaining [*] -> [* | n]: tokens left in the stream. */
static void ct_intrinsic_parser_remaining(CompileTimeVM *vm) {
    Parser *parser = vm->parser;
    parser_tokenize_all(parser);
    size_t left = parser->tokens.len > parser->pos ? parser->tokens.len - parser->pos : 0;
    ct_stack_push(&vm->stack, ct_make_int((int64_t)left));
}

/* ct-parser-set-pos [* | pos] -> [* | old-pos] */
static void ct_intrinsic_parser_set_pos(CompileTimeVM *vm) {
    CtValue v = ct_pop(vm);
    if (v.kind != CT_INT) {
        ct_value_type_error(vm, "ct-parser-set-pos", "an integer position", v);
    }
    Parser *parser = vm->parser;
    parser_tokenize_all(parser);
    if (v.as.i64 < 0 || (uint64_t)v.as.i64 > parser->tokens.len) {
        char *msg = str_printf("ct-parser-set-pos: position %lld is outside the token stream [0, %zu]",
                               (long long)v.as.i64, parser->tokens.len);
        ct_trace_error(vm, msg);
    }
    size_t old_pos = parser->pos;
    parser->pos = (size_t)v.as.i64;
    parser->has_last_token = parser->pos > 0;
    if (parser->pos > 0) {
        parser->last_token = parser->tokens.data[parser->pos - 1];
    }
    ct_stack_push(&vm->stack, ct_make_int((int64_t)old_pos));
}

/* ct-parser-collect-balanced [*, open | close] -> [*, tokens | found?]:
 * consume tokens up to the `close` that matches (nested open/close pairs
 * are kept in the result); found? is 0 when the input ran out first. */
static void ct_intrinsic_parser_collect_balanced(CompileTimeVM *vm) {
    char *close = ct_strict_pop_string(vm, "ct-parser-collect-balanced");
    char *open = ct_strict_pop_string(vm, "ct-parser-collect-balanced");
    if (!*open || !*close) {
        ct_trace_error(vm, "ct-parser-collect-balanced: delimiter must not be empty");
    }
    bool nests = strcmp(open, close) != 0;
    CtList *out = ct_list_new();
    size_t depth = 0;
    int found = 0;
    for (;;) {
        Token tok = parser_next_token(vm->parser);
        if (!tok.lexeme) {
            break;
        }
        if (strcmp(tok.lexeme, close) == 0) {
            if (depth == 0) {
                found = 1;
                break;
            }
            depth--;
        } else if (nests && strcmp(tok.lexeme, open) == 0) {
            depth++;
        }
        VEC_PUSH(&out->items, ct_make_token(tok));
    }
    free(open);
    free(close);
    ct_stack_push(&vm->stack, ct_make_list(out));
    ct_stack_push(&vm->stack, ct_make_int(found));
}

/* gensym [* | prefix] -> [* | name]: the prefix made an identifier
 * ([A-Za-z0-9_], trimmed of '_', "g" if empty, "g_" before a leading
 * digit) plus "_N", skipping names already in the dictionary. */
static void ct_intrinsic_gensym(CompileTimeVM *vm) {
    char *prefix = ct_strict_pop_string(vm, "gensym");
    for (char *c = prefix; *c; c++) {
        if (!isalnum((unsigned char)*c) && *c != '_') {
            *c = '_';
        }
    }
    char *start = prefix;
    while (*start == '_') {
        start++;
    }
    size_t len = strlen(start);
    while (len && start[len - 1] == '_') {
        start[--len] = '\0';
    }
    char *safe = !*start ? str_dup("g")
               : isdigit((unsigned char)*start) ? str_printf("g_%s", start)
               : str_dup(start);
    free(prefix);
    Parser *parser = vm->parser;
    char *candidate = NULL;
    for (;;) {
        candidate = str_printf("%s_%d", safe, ++parser->gensym_counter);
        if (!dictionary_lookup(vm->dictionary, candidate)) {
            break;
        }
        free(candidate);
    }
    free(safe);
    ct_stack_push(&vm->stack, ct_make_str(candidate));
    free(candidate);
}

/* string-replace [*, text, old | new] -> [* | text'] (every occurrence) */
static void ct_intrinsic_string_replace(CompileTimeVM *vm) {
    char *repl = ct_strict_pop_string(vm, "string-replace");
    char *old = ct_strict_pop_string(vm, "string-replace");
    char *text = ct_strict_pop_string(vm, "string-replace");
    if (!*old) {
        ct_trace_error(vm, "string-replace: substring to replace must not be empty");
    }
    size_t old_len = strlen(old), repl_len = strlen(repl);
    size_t count = 0;
    for (const char *p = strstr(text, old); p; p = strstr(p + old_len, old)) {
        count++;
    }
    char *out = (char *)xmalloc(strlen(text) + count * repl_len + 1);
    char *w = out;
    const char *r = text;
    for (const char *p = strstr(r, old); p; p = strstr(r, old)) {
        memcpy(w, r, (size_t)(p - r));
        w += p - r;
        memcpy(w, repl, repl_len);
        w += repl_len;
        r = p + old_len;
    }
    strcpy(w, r);
    ct_stack_push(&vm->stack, ct_make_str(out));
    free(out);
    free(text);
    free(old);
    free(repl);
}

static void bootstrap_dictionary(Dictionary *dict, Parser *parser, CompileTimeVM *vm) {
    (void)parser;
    register_ct_intrinsic(dict, "ct-label-new", ct_intrinsic_label_new);
    register_ct_intrinsic(dict, "ct-parser-get-pos", ct_intrinsic_parser_get_pos);
    register_ct_intrinsic(dict, "ct-parser-remaining", ct_intrinsic_parser_remaining);
    register_ct_intrinsic(dict, "ct-parser-set-pos", ct_intrinsic_parser_set_pos);
    register_ct_intrinsic(dict, "ct-parser-collect-balanced", ct_intrinsic_parser_collect_balanced);
    register_ct_intrinsic(dict, "gensym", ct_intrinsic_gensym);
    register_ct_intrinsic(dict, "string-replace", ct_intrinsic_string_replace);
    register_ct_intrinsic(dict, "ct-op-emit", ct_intrinsic_op_emit);
    register_ct_intrinsic(dict, "ct-control-frame-new", ct_intrinsic_control_frame_new);
    register_ct_intrinsic(dict, "ct-control-set", ct_intrinsic_control_set);
    register_ct_intrinsic(dict, "ct-control-get", ct_intrinsic_control_get);
    register_ct_intrinsic(dict, "ct-control-get?", ct_intrinsic_control_get_or_nil);
    register_ct_intrinsic(dict, "ct-control-add-close-op", ct_intrinsic_control_add_close_op);
    register_ct_intrinsic(dict, "ct-control-push", ct_intrinsic_control_push);
    register_ct_intrinsic(dict, "ct-control-pop", ct_intrinsic_control_pop);
    register_ct_intrinsic(dict, "ct-control-peek", ct_intrinsic_control_peek);
    register_ct_intrinsic(dict, "ct-control-depth", ct_intrinsic_control_depth);
    register_ct_intrinsic(dict, "ct-parser-last-line", ct_intrinsic_parser_last_line);
    register_ct_intrinsic(dict, "ct-parser-register-block-opener", ct_intrinsic_parser_register_block_opener);
    register_ct_intrinsic(dict, "ct-control-register-override", ct_intrinsic_control_register_override);
    register_ct_intrinsic(dict, "ct-control-unregister-override", ct_intrinsic_control_unregister_override);
    register_ct_intrinsic(dict, "syntax-concat", ct_intrinsic_syntax_concat);
    register_ct_intrinsic(dict, "nip", ct_intrinsic_nip);
    register_ct_intrinsic(dict, "tuck", ct_intrinsic_tuck);
    register_ct_intrinsic(dict, "-rot", ct_intrinsic_rot_back);
    register_ct_intrinsic(dict, "2dup", ct_intrinsic_2dup);
    register_ct_intrinsic(dict, "2drop", ct_intrinsic_2drop);
    register_ct_intrinsic(dict, "dup", ct_intrinsic_dup);
    register_ct_intrinsic(dict, "drop", ct_intrinsic_drop);
    register_ct_intrinsic(dict, "swap", ct_intrinsic_swap);
    register_ct_intrinsic(dict, "over", ct_intrinsic_over);
    register_ct_intrinsic(dict, "rot", ct_intrinsic_rot);
    register_ct_intrinsic(dict, "pick", ct_intrinsic_pick);
    register_ct_intrinsic(dict, "rpick", ct_intrinsic_rpick);
    register_ct_intrinsic(dict, ">r", ct_intrinsic_to_r);
    register_ct_intrinsic(dict, "r>", ct_intrinsic_from_r);
    register_ct_intrinsic(dict, "rdrop", ct_intrinsic_rdrop);
    register_ct_intrinsic(dict, "r@", ct_intrinsic_r_fetch);
    register_ct_intrinsic(dict, "+", ct_intrinsic_add);
    register_ct_intrinsic(dict, "-", ct_intrinsic_sub);
    register_ct_intrinsic(dict, "*", ct_intrinsic_mul);
    register_ct_intrinsic(dict, "/", ct_intrinsic_div);
    register_ct_intrinsic(dict, "%", ct_intrinsic_mod);
    register_ct_intrinsic(dict, "==", ct_intrinsic_eq);
    register_ct_intrinsic(dict, "!=", ct_intrinsic_ne);
    register_ct_intrinsic(dict, ">", ct_intrinsic_gt);
    register_ct_intrinsic(dict, "<", ct_intrinsic_lt);
    register_ct_intrinsic(dict, ">=", ct_intrinsic_ge);
    register_ct_intrinsic(dict, "<=", ct_intrinsic_le);
    register_ct_intrinsic(dict, "not", ct_intrinsic_not);
    register_ct_intrinsic(dict, "nil", ct_intrinsic_nil);
    register_ct_intrinsic(dict, "nil?", ct_intrinsic_nilp);
    register_ct_intrinsic(dict, "string-equal?", ct_intrinsic_string_equal);
    register_ct_intrinsic(dict, "string-length", ct_intrinsic_string_length);
    register_ct_intrinsic(dict, "string-append", ct_intrinsic_string_append);
    register_ct_intrinsic(dict, "try-string>int", ct_intrinsic_try_string_to_int);
    register_ct_intrinsic(dict, "string-contains?", ct_intrinsic_string_contains);
    register_ct_intrinsic(dict, "string-starts-with?", ct_intrinsic_string_starts_with);
    register_ct_intrinsic(dict, "string-ends-with?", ct_intrinsic_string_ends_with);
    register_ct_intrinsic(dict, "int>string", ct_intrinsic_int_to_string);
    register_ct_intrinsic(dict, "identifier?", ct_intrinsic_identifierp);
    register_ct_intrinsic(dict, "emit", ct_intrinsic_emit);
    register_ct_intrinsic(dict, "list>syntax", ct_intrinsic_list_to_syntax);
    register_ct_intrinsic(dict, "syntax>list", ct_intrinsic_syntax_to_list);
    register_ct_intrinsic(dict, "syntax-length", ct_intrinsic_syntax_length);
    register_ct_intrinsic(dict, L2_UNQUOTE_BEGIN, ct_intrinsic_unquote_begin);
    register_ct_intrinsic(dict, L2_UNQUOTE_END, ct_intrinsic_unquote_end);
    register_ct_intrinsic(dict, "list-new", ct_intrinsic_list_new);
    register_ct_intrinsic(dict, "list-append", ct_intrinsic_list_append);
    register_ct_intrinsic(dict, "list-pop", ct_intrinsic_list_pop);
    register_ct_intrinsic(dict, "list-pop-front", ct_intrinsic_list_pop_front);
    register_ct_intrinsic(dict, "list-peek-front", ct_intrinsic_list_peek_front);
    register_ct_intrinsic(dict, "list-push-front", ct_intrinsic_list_push_front);
    register_ct_intrinsic(dict, "list-reverse", ct_intrinsic_list_reverse);
    register_ct_intrinsic(dict, "list-clear", ct_intrinsic_list_clear);
    register_ct_intrinsic(dict, "list-length", ct_intrinsic_list_length);
    register_ct_intrinsic(dict, "list-empty?", ct_intrinsic_list_empty);
    register_ct_intrinsic(dict, "list-get", ct_intrinsic_list_get);
    register_ct_intrinsic(dict, "list-set", ct_intrinsic_list_set);
    register_ct_intrinsic(dict, "list-extend", ct_intrinsic_list_extend);
    register_ct_intrinsic(dict, "list-last", ct_intrinsic_list_last);
    register_ct_intrinsic(dict, "list-clone", ct_intrinsic_list_clone);
    register_ct_intrinsic(dict, "map-new", ct_intrinsic_map_new);
    register_ct_intrinsic(dict, "map-set", ct_intrinsic_map_set);
    register_ct_intrinsic(dict, "map-get", ct_intrinsic_map_get);
    register_ct_intrinsic(dict, "map-has?", ct_intrinsic_map_has);
    register_ct_intrinsic(dict, "map-length", ct_intrinsic_map_length);
    register_ct_intrinsic(dict, "map-empty?", ct_intrinsic_map_empty);
    register_ct_intrinsic(dict, "map-clear", ct_intrinsic_map_clear);
    register_ct_intrinsic(dict, "map-keys", ct_intrinsic_map_keys);
    register_ct_intrinsic(dict, "map-values", ct_intrinsic_map_values);
    register_ct_intrinsic(dict, "token-lexeme", ct_intrinsic_token_lexeme);
    register_ct_intrinsic(dict, "token-with-lexeme", ct_intrinsic_token_with_lexeme);
    register_ct_intrinsic(dict, "token-line", ct_intrinsic_token_line);
    register_ct_intrinsic(dict, "token-column", ct_intrinsic_token_column);
    register_ct_intrinsic(dict, "ct-parser-consume", ct_intrinsic_parser_consume);
    register_ct_intrinsic(dict, "ct-parser-peek", ct_intrinsic_parser_peek);
    register_ct_intrinsic(dict, "ct-parser-eof?", ct_intrinsic_parser_eof);
    register_ct_intrinsic(dict, "ct-parser-inject", ct_intrinsic_parser_inject);
    register_ct_intrinsic(dict, "ct-parser-current", ct_intrinsic_parser_current);
    register_ct_intrinsic(dict, "ct-output-append-data", ct_intrinsic_output_append_data);
    register_ct_intrinsic(dict, "ct-output-clear-data", ct_intrinsic_output_clear_data);
    register_ct_intrinsic(dict, "ct-output-set-data", ct_intrinsic_output_set_data);
    register_ct_intrinsic(dict, "ct-parser-set-token-hook", ct_intrinsic_set_token_hook);
    register_ct_intrinsic(dict, "ct-parser-clear-token-hook", ct_intrinsic_clear_token_hook);
    register_ct_intrinsic(dict, "ct-parser-error", ct_intrinsic_parse_error);
    register_ct_intrinsic(dict, "ct-output-clear-text-preamble", ct_intrinsic_output_clear_text_preamble);
    register_ct_intrinsic(dict, "ct-output-append-text-preamble", ct_intrinsic_output_append_text_preamble);
    register_ct_intrinsic(dict, "ct-output-set-text-preamble", ct_intrinsic_output_set_text_preamble);
    register_ct_intrinsic(dict, "ct-output-clear-bss", ct_intrinsic_output_clear_bss);
    register_ct_intrinsic(dict, "ct-output-append-bss", ct_intrinsic_output_append_bss);
    register_ct_intrinsic(dict, "ct-output-set-bss", ct_intrinsic_output_set_bss);
    register_ct_intrinsic(dict, "eval_program", ct_intrinsic_eval_program);
    register_ct_intrinsic(dict, "ct-definition-prefer-l2-by-name", ct_intrinsic_definition_prefer_l2_by_name);

    Word *ct_word = dictionary_lookup(dict, "CT");
    if (!ct_word) {
        ct_word = (Word *)xmalloc(sizeof(Word));
        memset(ct_word, 0, sizeof(Word));
        ct_word->name = str_dup("CT");
        dictionary_register(dict, ct_word);
    }
    ct_word->ct_intrinsic = ct_intrinsic_ct_flag;
    ct_word->compile_only = false;
    ct_word->runtime_only = false;
    ct_word->compile_time_override = false;

    AsmDefinition *ct_asm = (AsmDefinition *)xmalloc(sizeof(AsmDefinition));
    memset(ct_asm, 0, sizeof(AsmDefinition));
    ct_asm->name = str_dup("CT");
    ct_asm->body = str_dup(
        "    sub r12, 8\n"
        "    mov qword [r12], 0\n"
    );
    ct_word->asm_def = ct_asm;

    Form ct_form = {0};
    ct_form.kind = FORM_ASM;
    ct_form.ptr = ct_asm;
    VEC_PUSH(&parser->module.forms, ct_form);

    vm->dictionary = dict;
}

static void emit_push_literal(FunctionEmitter *builder, int64_t value) {
    emit_line(builder, str_printf("    ; push %lld", (long long)value));
    emit_line(builder, "    sub r12, 8");
    emit_line(builder, str_printf("    mov qword [r12], %lld", (long long)value));
}

static void emit_push_literal_u64(FunctionEmitter *builder, uint64_t value) {
    emit_line(builder, str_printf("    ; push %llu", (unsigned long long)value));
    emit_line(builder, "    sub r12, 8");
    emit_line(builder, str_printf("    mov rax, %llu", (unsigned long long)value));
    emit_line(builder, "    mov [r12], rax");
}

static void emit_push_label(FunctionEmitter *builder, const char *label) {
    emit_line(builder, str_printf("    ; push %s", label));
    emit_line(builder, str_printf("    lea rax, [rel %s]", label));
    emit_line(builder, "    sub r12, 8");
    emit_line(builder, "    mov [r12], rax");
}

static void emit_push_from(FunctionEmitter *builder, const char *reg) {
    emit_line(builder, "    sub r12, 8");
    emit_line(builder, str_printf("    mov [r12], %s", reg));
}

static void emit_pop_to(FunctionEmitter *builder, const char *reg) {
    emit_line(builder, str_printf("    mov %s, [r12]", reg));
    emit_line(builder, "    add r12, 8");
}

static void emission_init(Emission *emission) {
    VEC_INIT(&emission->text);
    VEC_INIT(&emission->data);
    VEC_INIT(&emission->bss);
}

typedef struct {
    Emission *emission;
    Dictionary *dictionary;
    StrMap string_labels;
    StrMap externs;
    StrMap label_cache;
    int unique_id;
    bool debug;
} EmitContext;

static void emit_extern(EmitContext *ctx, const char *name) {
    if (strmap_has(&ctx->externs, name)) {
        return;
    }
    strmap_set(&ctx->externs, name, (void *)1);
    VEC_PUSH(&ctx->emission->text, str_printf("extern %s", name));
}

static const char *emit_string_literal(EmitContext *ctx, const char *value) {
    char *label = (char *)strmap_get(&ctx->string_labels, value);
    if (label) {
        return label;
    }
    label = str_printf("__str_%d", ctx->unique_id++);
    strmap_set(&ctx->string_labels, value, label);
    StrVec bytes;
    VEC_INIT(&bytes);
    for (const unsigned char *p = (const unsigned char *)value; *p; p++) {
        VEC_PUSH(&bytes, str_printf("%u", (unsigned int)*p));
    }
    VEC_PUSH(&bytes, str_dup("0"));
    char *joined_bytes = str_join_parts(bytes.data, bytes.len, ", ", NULL);
    char *line = str_printf("%s: db %s", label, joined_bytes);
    free(joined_bytes);
    VEC_PUSH(&ctx->emission->data, line);
    return label;
}

static const char *emit_word_label(EmitContext *ctx, const char *name) {
    char *label = (char *)strmap_get(&ctx->label_cache, name);
    if (label) {
        return label;
    }
    char *sanitized = sanitize_label(name);
    label = str_printf("w_%s", sanitized);
    free(sanitized);
    strmap_set(&ctx->label_cache, name, label);
    return label;
}

static bool inline_stack_has(StrVec *stack, const char *name) {
    for (size_t i = 0; i < stack->len; i++) {
        if (strcmp(stack->data[i], name) == 0) {
            return true;
        }
    }
    return false;
}

static bool is_float_type(const char *type) {
    return type && (strcmp(type, "double") == 0 || strcmp(type, "float") == 0);
}

static void emit_extern_call(EmitContext *ctx, FunctionEmitter *builder, Word *word) {
    emit_extern(ctx, word->name);
    if (!word->extern_arg_types || word->extern_arg_count == 0) {
        emit_line(builder, str_printf("    call %s", word->name));
        if (word->extern_ret_type && strcmp(word->extern_ret_type, "void") != 0) {
            emit_push_from(builder, "rax");
        }
        return;
    }
    const char *int_regs[] = {"rdi", "rsi", "rdx", "rcx", "r8", "r9"};
    const char *float_regs[] = {"xmm0", "xmm1", "xmm2", "xmm3", "xmm4", "xmm5", "xmm6", "xmm7"};
    int int_idx = 0;
    int float_idx = 0;
    for (int i = 0; i < word->extern_arg_count; i++) {
        const char *type = word->extern_arg_types[i];
        int offset = (word->extern_arg_count - 1 - i) * 8;
        if (is_float_type(type)) {
            if (float_idx >= 8) {
                fprintf(stderr, "[error] too many float args for extern %s\n", word->name);
                exit(1);
            }
            emit_line(builder, str_printf("    movq %s, [r12 + %d]", float_regs[float_idx], offset));
            float_idx++;
        } else {
            if (int_idx >= 6) {
                fprintf(stderr, "[error] too many int args for extern %s\n", word->name);
                exit(1);
            }
            emit_line(builder, str_printf("    mov %s, [r12 + %d]", int_regs[int_idx], offset));
            int_idx++;
        }
    }
    emit_line(builder, str_printf("    add r12, %d", word->extern_arg_count * 8));
    emit_line(builder, "    mov r11, rsp");
    emit_line(builder, "    and r11, 15");
    char *align_label = str_printf(".L_align_%d", ctx->unique_id++);
    emit_line(builder, str_printf("    cmp r11, 0"));
    emit_line(builder, str_printf("    je %s", align_label));
    emit_line(builder, "    sub rsp, 8");
    emit_line(builder, "    xor eax, eax");
    emit_line(builder, str_printf("    mov al, %d", float_idx));
    emit_line(builder, str_printf("    call %s", word->name));
    emit_line(builder, "    add rsp, 8");
    emit_line(builder, str_printf("    jmp %s_done", align_label));
    emit_line(builder, str_printf("%s:", align_label));
    emit_line(builder, "    xor eax, eax");
    emit_line(builder, str_printf("    mov al, %d", float_idx));
    emit_line(builder, str_printf("    call %s", word->name));
    emit_line(builder, str_printf("%s_done:", align_label));
    free(align_label);
    if (word->extern_ret_type && strcmp(word->extern_ret_type, "void") == 0) {
        return;
    }
    if (word->extern_ret_type && is_float_type(word->extern_ret_type)) {
        emit_line(builder, "    sub r12, 8");
        emit_line(builder, "    movq [r12], xmm0");
    } else {
        emit_push_from(builder, "rax");
    }
}

static void emit_ops(EmitContext *ctx, FunctionEmitter *builder, OpVec *body, StrVec *inline_stack);

static void emit_word_call(EmitContext *ctx, FunctionEmitter *builder, const char *name, StrVec *inline_stack) {
    Word *word = dictionary_lookup(ctx->dictionary, name);
    if (!word) {
        fprintf(stderr, "[error] unknown word '%s' during emission\n", name);
        exit(1);
    }
    if (word->inline_def && word->definition) {
        if (inline_stack_has(inline_stack, word->name)) {
            fprintf(stderr, "[error] recursive inline word '%s'\n", word->name);
            exit(1);
        }
        VEC_PUSH(inline_stack, word->name);
        emit_ops(ctx, builder, &word->definition->body, inline_stack);
        inline_stack->len--;
        return;
    }
    if (word->is_extern && !word->extern_arg_types) {
        emit_extern(ctx, word->name);
        emit_line(builder, str_printf("    call %s", word->name));
        return;
    }
    if (word->asm_def) {
        emit_line(builder, str_printf("    call %s", emit_word_label(ctx, word->name)));
        return;
    }
    if (word->is_extern && word->extern_arg_types) {
        emit_extern_call(ctx, builder, word);
        return;
    }
    emit_line(builder, str_printf("    call %s", emit_word_label(ctx, word->name)));
}

static void emit_op(EmitContext *ctx, FunctionEmitter *builder, Op *op, StrVec *inline_stack) {
    switch (op->kind) {
        case OP_LITERAL: {
            if (op->lit_kind == LIT_INT) {
                emit_push_literal(builder, op->data.i64);
            } else if (op->lit_kind == LIT_FLOAT) {
                union { double f; uint64_t u; } conv;
                conv.f = op->data.f64;
                emit_push_literal_u64(builder, conv.u);
            } else if (op->lit_kind == LIT_STRING) {
                const char *label = emit_string_literal(ctx, op->data.str);
                emit_push_label(builder, label);
                emit_push_literal(builder, (int64_t)strlen(op->data.str));
            }
            break;
        }
        case OP_WORD:
            emit_word_call(ctx, builder, op->data.word, inline_stack);
            break;
        case OP_WORD_PTR: {
            Word *target = dictionary_lookup(ctx->dictionary, op->data.word);
            if (!target) {
                fprintf(stderr, "[error] unknown word '%s' referenced by pointer\n", op->data.word);
                exit(1);
            }
            if (target->is_extern) {
                emit_extern(ctx, target->name);
                emit_push_label(builder, target->name);
            } else {
                emit_push_label(builder, emit_word_label(ctx, target->name));
            }
            break;
        }
        case OP_BRANCH_ZERO:
            emit_pop_to(builder, "rax");
            emit_line(builder, "    cmp rax, 0");
            emit_line(builder, str_printf("    je %s", op->data.label));
            break;
        case OP_JUMP:
            emit_line(builder, str_printf("    jmp %s", op->data.label));
            break;
        case OP_LABEL:
            emit_line(builder, str_printf("%s:", op->data.label));
            break;
        case OP_FOR_BEGIN:
            emit_pop_to(builder, "rax");
            emit_line(builder, "    cmp rax, 0");
            emit_line(builder, str_printf("    jle %s", op->data.loop.end));
            emit_line(builder, "    sub r13, 8");
            emit_line(builder, "    mov [r13], rax");
            emit_line(builder, str_printf("%s:", op->data.loop.loop));
            break;
        case OP_FOR_END:
            emit_line(builder, "    mov rax, [r13]");
            emit_line(builder, "    dec rax");
            emit_line(builder, "    mov [r13], rax");
            emit_line(builder, "    cmp rax, 0");
            emit_line(builder, str_printf("    jg %s", op->data.loop.loop));
            emit_line(builder, "    add r13, 8");
            emit_line(builder, str_printf("%s:", op->data.loop.end));
            break;
        case OP_LIST_BEGIN:
            emit_line(builder, "    mov rax, [rel list_capture_sp]");
            emit_line(builder, "    mov [rax], r12");
            emit_line(builder, "    add rax, 8");
            emit_line(builder, "    mov [rel list_capture_sp], rax");
            break;
        case OP_LIST_END:
            char *list_done = str_printf(".list_copy_done_%d", ctx->unique_id++);
            char *list_loop = str_printf(".list_copy_loop_%d", ctx->unique_id++);
            emit_line(builder, "    mov rax, [rel list_capture_sp]");
            emit_line(builder, "    sub rax, 8");
            emit_line(builder, "    mov [rel list_capture_sp], rax");
            emit_line(builder, "    mov rbx, [rax]");
            emit_line(builder, "    mov rcx, rbx");
            emit_line(builder, "    sub rcx, r12");
            emit_line(builder, "    shr rcx, 3");
            emit_line(builder, "    mov r15, rcx");
            emit_line(builder, "    mov rdi, 0");
            emit_line(builder, "    mov rsi, rcx");
            emit_line(builder, "    add rsi, 1");
            emit_line(builder, "    shl rsi, 3");
            emit_line(builder, "    mov rdx, 3");
            emit_line(builder, "    mov r10, 34");
            emit_line(builder, "    mov r8, -1");
            emit_line(builder, "    mov r9, 0");
            emit_line(builder, "    mov rax, 9");
            emit_line(builder, "    syscall");
            emit_line(builder, "    mov [rax], r15");
            emit_line(builder, "    mov rcx, r15");
            emit_line(builder, "    cmp rcx, 0");
            emit_line(builder, str_printf("    je %s", list_done));
            emit_line(builder, "    lea rsi, [r12 + rcx*8 - 8]");
            emit_line(builder, "    lea rdi, [rax + 8]");
            emit_line(builder, str_printf("%s:", list_loop));
            emit_line(builder, "    mov rdx, [rsi]");
            emit_line(builder, "    mov [rdi], rdx");
            emit_line(builder, "    sub rsi, 8");
            emit_line(builder, "    add rdi, 8");
            emit_line(builder, "    dec rcx");
            emit_line(builder, str_printf("    jnz %s", list_loop));
            emit_line(builder, str_printf("%s:", list_done));
            emit_line(builder, "    mov r12, rbx");
            emit_line(builder, "    sub r12, 8");
            emit_line(builder, "    mov [r12], rax");
            free(list_done);
            free(list_loop);
            break;
        case OP_RET:
            emit_line(builder, "    ret");
            break;
    }
}

static void emit_ops(EmitContext *ctx, FunctionEmitter *builder, OpVec *body, StrVec *inline_stack) {
    for (size_t i = 0; i < body->len; i++) {
        emit_op(ctx, builder, &body->data[i], inline_stack);
    }
}

static void emit_definition(EmitContext *ctx, Definition *def) {
    FunctionEmitter builder;
    emitter_init(&builder, &ctx->emission->text, ctx->debug);
    const char *label = emit_word_label(ctx, def->name);
    if (strcmp(def->name, "main") == 0) {
        emit_line(&builder, str_printf("global %s", label));
    }
    emit_line(&builder, str_printf("%s:", label));
    StrVec inline_stack;
    VEC_INIT(&inline_stack);
    emit_ops(ctx, &builder, &def->body, &inline_stack);
    emit_line(&builder, "    ret");
}

static void emit_asm_definition(EmitContext *ctx, AsmDefinition *def) {
    if (!def || !def->body) {
        return;
    }
    VEC_PUSH(&ctx->emission->text, str_printf("%s:", emit_word_label(ctx, def->name)));
    const char *cursor = def->body;
    while (*cursor) {
        const char *line_end = strchr(cursor, '\n');
        size_t len = line_end ? (size_t)(line_end - cursor) : strlen(cursor);
        char *line = (char *)xmalloc(len + 1);
        memcpy(line, cursor, len);
        line[len] = '\0';
        if (len > 0) {
            char *trim = line;
            while (*trim && isspace((unsigned char)*trim)) {
                trim++;
            }
            size_t trim_len = strlen(trim);
            if (trim_len > 0 && trim[trim_len - 1] == ':') {
                VEC_PUSH(&ctx->emission->text, str_dup(trim));
                free(line);
            } else {
                VEC_PUSH(&ctx->emission->text, line);
            }
        } else {
            free(line);
        }
        if (!line_end) {
            break;
        }
        cursor = line_end + 1;
    }
    VEC_PUSH(&ctx->emission->text, str_dup("    ret"));
}

static void emit_default_prelude(Emission *emission) {
    VEC_PUSH(&emission->text, str_dup("%define DSTK_BYTES 65536"));
    VEC_PUSH(&emission->text, str_dup("%define RSTK_BYTES 65536"));
    VEC_PUSH(&emission->text, str_dup("%define PRINT_BUF_BYTES 4096"));
    VEC_PUSH(&emission->text, str_dup("global dstack_top"));
    VEC_PUSH(&emission->text, str_dup("global rstack_top"));
    VEC_PUSH(&emission->text, str_dup("global _start"));
    VEC_PUSH(&emission->text, str_dup("_start:"));
    VEC_PUSH(&emission->text, str_dup("    mov rbx, rsp"));
    VEC_PUSH(&emission->text, str_dup("    mov rax, [rbx]"));
    VEC_PUSH(&emission->text, str_dup("    mov [rel sys_argc], rax"));
    VEC_PUSH(&emission->text, str_dup("    lea rax, [rbx + 8]"));
    VEC_PUSH(&emission->text, str_dup("    mov [rel sys_argv], rax"));
    VEC_PUSH(&emission->text, str_dup("    lea r12, [rel dstack_top]"));
    VEC_PUSH(&emission->text, str_dup("    lea r13, [rel rstack_top]"));
    VEC_PUSH(&emission->text, str_dup("    lea rax, [rel list_capture_stack]"));
    VEC_PUSH(&emission->text, str_dup("    mov [rel list_capture_sp], rax"));
    VEC_PUSH(&emission->text, str_dup("    call w_main"));
    VEC_PUSH(&emission->text, str_dup("    mov rax, [r12]"));
    VEC_PUSH(&emission->text, str_dup("    mov rdi, rax"));
    VEC_PUSH(&emission->text, str_dup("    mov rax, 60"));
    VEC_PUSH(&emission->text, str_dup("    syscall"));
}

static void emit_libc_prelude(Emission *emission) {
    VEC_PUSH(&emission->text, str_dup("%define DSTK_BYTES 65536"));
    VEC_PUSH(&emission->text, str_dup("%define RSTK_BYTES 65536"));
    VEC_PUSH(&emission->text, str_dup("%define PRINT_BUF_BYTES 4096"));
    VEC_PUSH(&emission->text, str_dup("global dstack_top"));
    VEC_PUSH(&emission->text, str_dup("global rstack_top"));
    VEC_PUSH(&emission->text, str_dup("global main"));
    VEC_PUSH(&emission->text, str_dup("main:"));
    VEC_PUSH(&emission->text, str_dup("    mov [rel sys_argc], rdi"));
    VEC_PUSH(&emission->text, str_dup("    mov [rel sys_argv], rsi"));
    VEC_PUSH(&emission->text, str_dup("    lea r12, [rel dstack_top]"));
    VEC_PUSH(&emission->text, str_dup("    lea r13, [rel rstack_top]"));
    VEC_PUSH(&emission->text, str_dup("    lea rax, [rel list_capture_stack]"));
    VEC_PUSH(&emission->text, str_dup("    mov [rel list_capture_sp], rax"));
    VEC_PUSH(&emission->text, str_dup("    call w_main"));
    VEC_PUSH(&emission->text, str_dup("    mov rax, [r12]"));
    VEC_PUSH(&emission->text, str_dup("    ret"));
}

static void emit_default_bss(Emission *emission) {
    VEC_PUSH(&emission->bss, str_dup("align 16"));
    VEC_PUSH(&emission->bss, str_dup("dstack: resb DSTK_BYTES"));
    VEC_PUSH(&emission->bss, str_dup("dstack_top:"));
    VEC_PUSH(&emission->bss, str_dup("align 16"));
    VEC_PUSH(&emission->bss, str_dup("rstack: resb RSTK_BYTES"));
    VEC_PUSH(&emission->bss, str_dup("rstack_top:"));
    VEC_PUSH(&emission->bss, str_dup("align 16"));
    VEC_PUSH(&emission->bss, str_dup("print_buf: resb PRINT_BUF_BYTES"));
    VEC_PUSH(&emission->bss, str_dup("print_buf_end:"));
    VEC_PUSH(&emission->bss, str_dup("align 16"));
    VEC_PUSH(&emission->bss, str_dup("persistent: resb 64"));
    VEC_PUSH(&emission->bss, str_dup("persistent_end:"));
    VEC_PUSH(&emission->bss, str_dup("align 16"));
    VEC_PUSH(&emission->bss, str_dup("list_capture_sp: resq 1"));
    VEC_PUSH(&emission->bss, str_dup("list_capture_tmp: resq 1"));
    VEC_PUSH(&emission->bss, str_dup("list_capture_stack: resq 1024"));
}

static Emission emit_module(Parser *parser, Dictionary *dict, bool debug) {
    Emission emission;
    emission_init(&emission);
    EmitContext ctx;
    ctx.emission = &emission;
    ctx.dictionary = dict;
    strmap_init(&ctx.string_labels);
    strmap_init(&ctx.externs);
    strmap_init(&ctx.label_cache);
    ctx.unique_id = 0;
    ctx.debug = debug;

    if (parser->uses_libc) {
        emit_libc_prelude(&emission);
    } else {
        emit_default_prelude(&emission);
    }
    /* The CT text preamble follows the runtime prelude; it never replaces it. */
    if (parser->custom_prelude) {
        for (size_t i = 0; i < parser->custom_prelude->len; i++) {
            if (parser->custom_prelude->data[i][0]) {  /* empty fragments emit nothing */
                VEC_PUSH(&emission.text, str_dup(parser->custom_prelude->data[i]));
            }
        }
    }

    VEC_PUSH(&emission.data, str_dup("sys_argc: dq 0"));
    VEC_PUSH(&emission.data, str_dup("sys_argv: dq 0"));
    if (parser->custom_data) {
        for (size_t i = 0; i < parser->custom_data->len; i++) {
            if (parser->custom_data->data[i][0]) {  /* empty fragments emit nothing */
                VEC_PUSH(&emission.data, str_dup(parser->custom_data->data[i]));
            }
        }
    }

    if (!parser->bss_replaced) {
        emit_default_bss(&emission);
    }
    if (parser->custom_bss) {
        for (size_t i = 0; i < parser->custom_bss->len; i++) {
            if (parser->custom_bss->data[i][0]) {  /* empty fragments emit nothing */
                VEC_PUSH(&emission.bss, str_dup(parser->custom_bss->data[i]));
            }
        }
    }

    for (size_t i = 0; i < parser->module.forms.len; i++) {
        Form form = parser->module.forms.data[i];
        if (form.kind == FORM_DEF) {
            Definition *def = (Definition *)form.ptr;
            if (def->compile_only) {
                continue;
            }
            Word *word = dictionary_lookup(dict, def->name);
            if (!word || word->definition != def) {
                continue;
            }
            emit_definition(&ctx, def);
        } else if (form.kind == FORM_ASM) {
            AsmDefinition *def = (AsmDefinition *)form.ptr;
            if (def->compile_only) {
                continue;
            }
            Word *word = dictionary_lookup(dict, def->name);
            if (!word || word->asm_def != def) {
                continue;
            }
            emit_asm_definition(&ctx, def);
        }
    }

    for (size_t i = 0; i < parser->variable_labels.cap; i++) {
        if (!parser->variable_labels.keys || !parser->variable_labels.keys[i]) {
            continue;
        }
        const char *label = (const char *)parser->variable_labels.values[i];
        if (label) {
            VEC_PUSH(&emission.data, str_printf("%s: dq 0", label));
        }
    }

    return emission;
}

static bool ends_with_line_terminator(const char *text) {
    size_t n = strlen(text);
    return n > 0 && (text[n - 1] == '\n' || text[n - 1] == '\r');
}

/* Join emitted segments with the fragment boundary rule (as the Python
 * compiler): one LF between two segments only when neither side has a line
 * terminator there, and a final LF when the last segment lacks one.  Compiler
 * lines never carry terminators, so for them this is a plain line join. */
static char *join_segments(char *const *items, size_t count) {
    size_t total = 1;
    for (size_t i = 0; i < count; i++) {
        if (items[i]) total += strlen(items[i]) + 1;
    }
    char *out = (char *)xmalloc(total);
    char *cursor = out;
    const char *prev = NULL;
    for (size_t i = 0; i < count; i++) {
        const char *item = items[i];
        if (!item) continue;
        if (prev && !ends_with_line_terminator(prev) && item[0] != '\n' && item[0] != '\r') {
            *cursor++ = '\n';
        }
        size_t n = strlen(item);
        memcpy(cursor, item, n);
        cursor += n;
        prev = item;
    }
    if (prev && !ends_with_line_terminator(prev)) {
        *cursor++ = '\n';
    }
    *cursor = '\0';
    return out;
}

static char *emission_snapshot(Emission *emission) {
    StrVec parts;
    VEC_INIT(&parts);
    if (emission->text.len) {
        VEC_PUSH(&parts, str_dup("section .text"));
        for (size_t i = 0; i < emission->text.len; i++) {
            if (emission->text.data[i]) {
                VEC_PUSH(&parts, str_dup(emission->text.data[i]));
            }
        }
    }
    if (emission->data.len) {
        VEC_PUSH(&parts, str_dup("section .data"));
        VEC_PUSH(&parts, str_dup("data_start:"));
        for (size_t i = 0; i < emission->data.len; i++) {
            if (emission->data.data[i]) {
                VEC_PUSH(&parts, str_dup(emission->data.data[i]));
            }
        }
        VEC_PUSH(&parts, str_dup("data_end:"));
    }
    if (emission->bss.len) {
        VEC_PUSH(&parts, str_dup("section .bss"));
        for (size_t i = 0; i < emission->bss.len; i++) {
            if (emission->bss.data[i]) {
                VEC_PUSH(&parts, str_dup(emission->bss.data[i]));
            }
        }
    }
    VEC_PUSH(&parts, str_dup("section .note.GNU-stack noalloc noexec nowrite"));
    char *buf = join_segments(parts.data, parts.len);
    return buf;
}

/* helper prototypes (defined later) */
static Emission emit_module(Parser *parser, Dictionary *dict, bool debug);
static char *emission_snapshot(Emission *emission);
static char *make_eval_workdir(void);
static void write_file(const char *path, const char *data);
static void run_nasm(const char *asm_path, const char *obj_path, bool debug);
static void run_linker(const char *obj_path, const char *exe_path, bool debug, StrVec *libs, bool shared, bool use_libc);
static void run_cmd(char *const argv[]);
static void cleanup_eval_workdir(const char *work_dir);

static void ct_intrinsic_eval_program(CompileTimeVM *vm) {
    long source_len = -1;
    char *source = NULL;
    if (vm->stack.len >= 2 && vm->stack.data[vm->stack.len - 1].kind == CT_INT && vm->stack.data[vm->stack.len - 2].kind == CT_STR) {
        source_len = (long)ct_pop_int(vm);
        source = ct_pop_str(vm);
    } else {
        source = ct_pop_str(vm);
        if (source) {
            source_len = (long)strlen(source);
        }
    }
    if (!source) {
        ct_stack_push(&vm->stack, ct_make_int(0));
        return;
    }

    /* Call the runtime eval_program which uses the CT VM internally.
       This ensures IO (puti, print, etc.) is executed at compile time. */
    int result = eval_program(source, source_len);
    ct_stack_push(&vm->stack, ct_make_int((int64_t)result));
    free(source);
}

static void write_file(const char *path, const char *data) {
    FILE *f = fopen(path, "w");
    if (!f) {
        fprintf(stderr, "[error] failed to write %s: %s\n", path, strerror(errno));
        exit(1);
    }
    fputs(data, f);
    fclose(f);
}

static void run_cmd(char *const argv[]) {
    pid_t pid = fork();
    if (pid < 0) {
        fprintf(stderr, "[error] fork failed: %s\n", strerror(errno));
        exit(1);
    }
    if (pid == 0) {
        execvp(argv[0], argv);
        fprintf(stderr, "[error] failed to exec %s: %s\n", argv[0], strerror(errno));
        _exit(1);
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        fprintf(stderr, "[error] waitpid failed: %s\n", strerror(errno));
        exit(1);
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        fprintf(stderr, "[error] command failed\n");
        exit(1);
    }
}

static int run_cmd_status(char *const argv[]) {
    pid_t pid = fork();
    if (pid < 0) {
        fprintf(stderr, "[error] fork failed: %s\n", strerror(errno));
        return 127;
    }
    if (pid == 0) {
        execvp(argv[0], argv);
        fprintf(stderr, "[error] failed to exec %s: %s\n", argv[0], strerror(errno));
        _exit(127);
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        fprintf(stderr, "[error] waitpid failed: %s\n", strerror(errno));
        return 127;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 127;
}

static int run_l2_cli_in_child(int argc, char **argv) {
    pid_t pid = fork();
    if (pid < 0) {
        fprintf(stderr, "[error] fork failed: %s\n", strerror(errno));
        return 127;
    }
    if (pid == 0) {
        int rc = l2_cli(argc, argv);
        fflush(NULL);
        _exit(rc == 0 ? 0 : 1);
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        fprintf(stderr, "[error] waitpid failed: %s\n", strerror(errno));
        return 127;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 127;
}

typedef struct {
    bool initialized;
    Dictionary dict;
    Reader reader;
    Parser parser;
    CompileTimeVM vm;
    StrVec include_dirs;
    char *root_dir;
    char *stdlib_dir;
    bool embedded_seeded;
} L2EvalContext;

static L2EvalContext g_eval_ctx;
static L2EvalContext g_eval_env_ctx;

static uintptr_t g_l2_eval_stack_top_override = 0;
static char *g_l2_source_embed_owned = NULL;
static const unsigned char *g_l2_source_embed_ptr = NULL;
static size_t g_l2_source_embed_len = 0;

static const unsigned char *l2_eval_get_embedded_source(size_t *out_len);

void l2_set_eval_stack_top(uintptr_t stack_top_addr) {
    g_l2_eval_stack_top_override = stack_top_addr;
}

void l2_set_embedded_source(const char *source, long source_len) {
    free(g_l2_source_embed_owned);
    g_l2_source_embed_owned = NULL;
    g_l2_source_embed_ptr = NULL;
    g_l2_source_embed_len = 0;

    if (!source || source_len <= 0) {
        return;
    }

    size_t len = (size_t)source_len;
    g_l2_source_embed_owned = (char *)xmalloc(len + 1);
    memcpy(g_l2_source_embed_owned, source, len);
    g_l2_source_embed_owned[len] = '\0';
    g_l2_source_embed_ptr = (const unsigned char *)g_l2_source_embed_owned;
    g_l2_source_embed_len = len;
}

#ifdef L2_AS_LIBRARY
extern unsigned char dstack_top __attribute__((weak));
static unsigned char l2_eval_fallback_stack[8 * 1024 * 1024];

static uintptr_t l2_eval_stack_top_addr(void) {
    if (g_l2_eval_stack_top_override != 0) {
        return g_l2_eval_stack_top_override;
    }
    if (&dstack_top != NULL) {
        return (uintptr_t)&dstack_top;
    }
    return (uintptr_t)(l2_eval_fallback_stack + sizeof(l2_eval_fallback_stack));
}
#else
extern unsigned char dstack_top;

static uintptr_t l2_eval_stack_top_addr(void) {
    if (g_l2_eval_stack_top_override != 0) {
        return g_l2_eval_stack_top_override;
    }
    return (uintptr_t)&dstack_top;
}
#endif

static char *l2_default_root_dir(void);

static void l2_eval_ctx_init(L2EvalContext *ctx) {
    if (ctx->initialized) {
        return;
    }
    dictionary_init(&ctx->dict);
    reader_init(&ctx->reader);
    parser_init(&ctx->parser, &ctx->dict, &ctx->reader);
    ct_vm_init(&ctx->vm, &ctx->parser);
    ctx->parser.ct_vm = &ctx->vm;
    bootstrap_dictionary(&ctx->dict, &ctx->parser, &ctx->vm);
    register_builtin_syscall(&ctx->parser);
    ctx->root_dir = l2_default_root_dir();
    ctx->stdlib_dir = path_join(ctx->root_dir, "stdlib");
    VEC_INIT(&ctx->include_dirs);
    VEC_PUSH(&ctx->include_dirs, str_dup(ctx->root_dir));
    VEC_PUSH(&ctx->include_dirs, str_dup(ctx->stdlib_dir));
    ctx->embedded_seeded = false;
    ctx->initialized = true;
}

static void l2_eval_ctx_prepare_parser(L2EvalContext *ctx) {
    parser_init(&ctx->parser, &ctx->dict, &ctx->reader);
    ctx->parser.ct_vm = &ctx->vm;
    ctx->vm.parser = &ctx->parser;
    ctx->vm.dictionary = &ctx->dict;
    ctx->vm.loop_remaining.len = 0;
    ctx->vm.loop_begin.len = 0;
    ctx->vm.loop_initial.len = 0;
    ctx->vm.call_stack.len = 0;
}

static void l2_eval_seed_embedded_program(L2EvalContext *ctx) {
    if (!ctx || ctx->embedded_seeded) {
        return;
    }
    size_t embed_len = 0;
    const unsigned char *embed_source = l2_eval_get_embedded_source(&embed_len);
    if (!embed_source || embed_len == 0) {
        ctx->embedded_seeded = true;
        return;
    }

    char *owned_source = (char *)xmalloc(embed_len + 1);
    memcpy(owned_source, embed_source, embed_len);
    owned_source[embed_len] = '\0';
    char *root_dir = str_dup(ctx->root_dir ? ctx->root_dir : l2_default_root_dir());
    StrMap visited;
    FileSpanVec spans;
    VEC_INIT(&spans);
    strmap_init(&visited);
    int line_counter = 1;

    /* The host program may rely on compile-time words that only the Python
     * compiler has (stdlib/control.sl's `ct-label-new`, gvars.sl's
     * `syntax-concat`, ...), and this VM exits on those.  Seed under an exit
     * guard with stderr muted; on failure drop the half-seeded context and
     * go on with a fresh one, so eval runs without the host's words.  The
     * value stack is set aside first: eval_env has already filled it. */
    CtValueVec saved_stack = ctx->vm.stack;
    ct_stack_init(&ctx->vm.stack);
    fflush(stderr);
    int saved_stderr = dup(STDERR_FILENO);
    int devnull = open("/dev/null", O_WRONLY | O_CLOEXEC);
    if (saved_stderr >= 0 && devnull >= 0) {
        dup2(devnull, STDERR_FILENO);
    }
    if (devnull >= 0) {
        close(devnull);
    }
    char *volatile expanded = NULL;
    jmp_buf guard;
    jmp_buf *outer_guard = g_l2_exit_guard;
    volatile bool seeded = false;
    if (setjmp(guard) == 0) {
        g_l2_exit_guard = &guard;
        expanded = expand_imports_from_source(owned_source, root_dir, &ctx->include_dirs, &visited, &spans, &line_counter);
        if (expanded && *expanded) {
            parse_tokens(&ctx->parser, expanded);
        }
        seeded = true;
    }
    g_l2_exit_guard = outer_guard;
    fflush(stderr);
    if (saved_stderr >= 0) {
        dup2(saved_stderr, STDERR_FILENO);
        close(saved_stderr);
    }

    if (seeded) {
        /* Remember which definitions came from the host program, so eval
         * calls the host's native code for them until eval'd code redefines
         * one. */
        StrMap *words = &ctx->dict.words;
        for (size_t i = 0; i < words->cap; i++) {
            Word *word = words->keys && words->keys[i] ? (Word *)words->values[i] : NULL;
            if (word && word->definition) {
                word->host_seeded = true;
                word->host_definition = word->definition;
            }
        }
        VEC_FREE(&ctx->vm.stack);
        ctx->vm.stack = saved_stack;
    } else {
        /* The abandoned context is leaked: the jump may have left any part
         * of it inconsistent, so freeing it is not safe. */
        memset(ctx, 0, sizeof(*ctx));
        l2_eval_ctx_init(ctx);
        l2_eval_ctx_prepare_parser(ctx);
        ctx->vm.stack = saved_stack;
        static bool warned = false;
        if (!warned) {
            warned = true;
            fprintf(stderr, "[warn] eval: the host program uses compile-time words the eval runtime "
                            "lacks; eval'd code runs without the host's definitions\n");
        }
    }

    for (size_t i = 0; i < spans.len; i++) {
        free(spans.data[i].path);
    }
    VEC_FREE(&spans);
    strmap_free(&visited);
    free(expanded);
    free(root_dir);
    free(owned_source);
    ctx->embedded_seeded = true;
}

static int l2_eval_inline_with_ctx(L2EvalContext *ctx, const char *source, long source_len);

static char *make_eval_workdir(void) {
    char *tmpl = str_dup("/tmp/l2eval_XXXXXX");
    int fd = mkstemp(tmpl);
    if (fd < 0) {
        fprintf(stderr, "[error] mkstemp failed: %s\n", strerror(errno));
        free(tmpl);
        return NULL;
    }
    close(fd);
    if (unlink(tmpl) != 0) {
        fprintf(stderr, "[error] unlink failed for %s: %s\n", tmpl, strerror(errno));
        free(tmpl);
        return NULL;
    }
    if (mkdir(tmpl, 0700) != 0) {
        fprintf(stderr, "[error] mkdir failed for %s: %s\n", tmpl, strerror(errno));
        free(tmpl);
        return NULL;
    }
    return tmpl;
}

static int remove_tree(const char *path) {
    struct stat st;
    if (lstat(path, &st) != 0) {
        return -1;
    }
    if (S_ISDIR(st.st_mode)) {
        DIR *dir = opendir(path);
        if (!dir) {
            return -1;
        }
        int rc = 0;
        struct dirent *ent = NULL;
        while ((ent = readdir(dir)) != NULL) {
            if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) {
                continue;
            }
            char *child = path_join(path, ent->d_name);
            if (remove_tree(child) != 0) {
                rc = -1;
            }
            free(child);
        }
        closedir(dir);
        if (rmdir(path) != 0) {
            rc = -1;
        }
        return rc;
    }
    return unlink(path) == 0 ? 0 : -1;
}

static void cleanup_eval_workdir(const char *work_dir) {
    if (!work_dir) {
        return;
    }
    if (!str_starts_with(work_dir, "/tmp/l2eval_")) {
        return;
    }
    (void)remove_tree(work_dir);
}

static void run_nasm(const char *asm_path, const char *obj_path, bool debug) {
    char *argv[8];
    int idx = 0;
    argv[idx++] = "nasm";
    argv[idx++] = "-f";
    argv[idx++] = "elf64";
    if (debug) {
        argv[idx++] = "-g";
        argv[idx++] = "-F";
        argv[idx++] = "dwarf";
    }
    argv[idx++] = "-o";
    argv[idx++] = (char *)obj_path;
    argv[idx++] = (char *)asm_path;
    argv[idx++] = NULL;
    run_cmd(argv);
}

static void run_linker(const char *obj_path, const char *exe_path, bool debug, StrVec *libs, bool shared, bool use_libc) {
    const char *linker = NULL;
    if (use_libc) {
        if (access("/usr/bin/cc", X_OK) == 0) {
            linker = "cc";
        } else if (access("/usr/bin/gcc", X_OK) == 0) {
            linker = "gcc";
        } else {
            fprintf(stderr, "[error] no C compiler found for libc linking\n");
            exit(1);
        }
    } else if (access("/usr/bin/ld.lld", X_OK) == 0) {
        linker = "ld.lld";
    } else if (access("/usr/bin/ld", X_OK) == 0) {
        linker = "ld";
    } else {
        fprintf(stderr, "[error] no linker found\n");
        exit(1);
    }
    StrVec argv;
    VEC_INIT(&argv);
    VEC_PUSH(&argv, str_dup((char *)linker));
    if (!use_libc && strstr(linker, "lld")) {
        VEC_PUSH(&argv, str_dup("-m"));
        VEC_PUSH(&argv, str_dup("elf_x86_64"));
    }
    if (shared) {
        VEC_PUSH(&argv, str_dup("-shared"));
    }
    VEC_PUSH(&argv, str_dup("-o"));
    VEC_PUSH(&argv, str_dup((char *)exe_path));
    VEC_PUSH(&argv, str_dup((char *)obj_path));
    if (use_libc) {
        VEC_PUSH(&argv, str_dup("-no-pie"));
    } else if (!shared && (!libs || libs->len == 0)) {
        VEC_PUSH(&argv, str_dup("-nostdlib"));
        VEC_PUSH(&argv, str_dup("-static"));
    } else if (!shared) {
        const char *candidates[] = {
            "/lib64/ld-linux-x86-64.so.2",
            "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
            "/lib/ld-linux-x86-64.so.2",
            "/lib/ld64.so.1"
        };
        const char *interp = NULL;
        for (size_t i = 0; i < ARRAY_LEN(candidates); i++) {
            if (access(candidates[i], R_OK) == 0) {
                interp = candidates[i];
                break;
            }
        }
        if (interp) {
            VEC_PUSH(&argv, str_dup("-dynamic-linker"));
            VEC_PUSH(&argv, str_dup(interp));
        }
    }
    if (libs) {
        for (size_t i = 0; i < libs->len; i++) {
            VEC_PUSH(&argv, str_dup(libs->data[i]));
        }
    }
    if (debug) {
        VEC_PUSH(&argv, str_dup("-g"));
    }
    VEC_PUSH(&argv, NULL);
    run_cmd(argv.data);
}

static char *read_text_file(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) {
        return NULL;
    }
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (size < 0) {
        fclose(f);
        return NULL;
    }
    char *buf = (char *)xmalloc((size_t)size + 1);
    size_t n = fread(buf, 1, (size_t)size, f);
    buf[n] = '\0';
    fclose(f);
    return buf;
}

static bool file_exists(const char *path) {
    return access(path, R_OK) == 0;
}

static char *path_dirname(const char *path) {
    const char *slash = strrchr(path, '/');
    if (!slash) {
        return str_dup(".");
    }
    size_t len = (size_t)(slash - path);
    if (len == 0) {
        return str_dup("/");
    }
    char *out = (char *)xmalloc(len + 1);
    memcpy(out, path, len);
    out[len] = '\0';
    return out;
}

static char *path_basename(const char *path) {
    if (!path) {
        return str_dup("");
    }
    const char *slash = strrchr(path, '/');
    if (!slash || !slash[1]) {
        return str_dup(path);
    }
    return str_dup(slash + 1);
}

static char *path_join(const char *a, const char *b) {
    if (!a || !*a) {
        return str_dup(b);
    }
    if (!b || !*b) {
        return str_dup(a);
    }
    size_t len_a = strlen(a);
    bool has_sep = a[len_a - 1] == '/';
    return str_printf("%s%s%s", a, has_sep ? "" : "/", b);
}

#if defined(__GNUC__)
extern const unsigned char l2_source_embed[] __attribute__((weak));
extern const unsigned long long l2_source_embed_len __attribute__((weak));
#else
extern const unsigned char l2_source_embed[];
extern const unsigned long long l2_source_embed_len;
#endif

static const unsigned char *l2_eval_get_embedded_source(size_t *out_len) {
    if (out_len) {
        *out_len = 0;
    }

    if (g_l2_source_embed_ptr && g_l2_source_embed_len > 0) {
        if (out_len) {
            *out_len = g_l2_source_embed_len;
        }
        return g_l2_source_embed_ptr;
    }

    if (&l2_source_embed_len == NULL || &l2_source_embed == NULL) {
        return NULL;
    }
    size_t len = (size_t)l2_source_embed_len;
    if (len == 0) {
        return NULL;
    }
    if (out_len) {
        *out_len = len;
    }
    return l2_source_embed;
}

static void rstrip_inplace(char *text) {
    if (!text) {
        return;
    }
    size_t len = strlen(text);
    while (len > 0 && isspace((unsigned char)text[len - 1])) {
        text[--len] = '\0';
    }
}

static bool source_token_equals(const char *source, size_t start, size_t end, const char *text) {
    size_t text_len = strlen(text);
    return (end >= start) && (end - start == text_len) && memcmp(source + start, text, text_len) == 0;
}

static bool source_next_token(const char *source, size_t source_len, size_t *cursor, size_t *start_out, size_t *end_out) {
    size_t pos = *cursor;
    while (pos < source_len) {
        unsigned char ch = (unsigned char)source[pos];
        if (isspace(ch)) {
            pos++;
            continue;
        }
        if (ch == '#') {
            while (pos < source_len && source[pos] != '\n') {
                pos++;
            }
            continue;
        }
        break;
    }
    if (pos >= source_len) {
        *cursor = pos;
        return false;
    }

    size_t start = pos;
    char ch = source[pos];
    if (ch == '"') {
        pos++;
        while (pos < source_len) {
            if (source[pos] == '\\' && pos + 1 < source_len) {
                pos += 2;
                continue;
            }
            if (source[pos] == '"') {
                pos++;
                break;
            }
            pos++;
        }
    } else if (ch == '(' || ch == ')' || ch == '{' || ch == '}' || ch == '[' || ch == ']' || ch == ';') {
        pos++;
    } else {
        while (pos < source_len) {
            ch = source[pos];
            if (isspace((unsigned char)ch) || ch == '#' || ch == '(' || ch == ')' || ch == '{' || ch == '}' || ch == '[' || ch == ']' || ch == ';') {
                break;
            }
            pos++;
        }
    }

    *cursor = pos;
    *start_out = start;
    *end_out = pos;
    return true;
}

static char *trim_definition_source_text(char *text) {
    if (!text) {
        return NULL;
    }
    rstrip_inplace(text);
    return text;
}

static char *copy_embedded_source_slice(size_t start, size_t end, long *out_len) {
    if (out_len) {
        *out_len = 0;
    }
    size_t source_len = 0;
    const unsigned char *embed_source = l2_eval_get_embedded_source(&source_len);
    if (!embed_source) {
        return NULL;
    }
    if (source_len == 0 || start >= end || end > source_len) {
        return NULL;
    }
    char *copy = (char *)xmalloc((end - start) + 1);
    memcpy(copy, embed_source + start, end - start);
    copy[end - start] = '\0';
    trim_definition_source_text(copy);
    if (out_len) {
        *out_len = (long)strlen(copy);
    }
    return copy;
}

static const char *find_embedded_definition_source(const char *func_name, long *out_len) {
    if (out_len) {
        *out_len = 0;
    }
    if (!func_name || !*func_name) {
        return NULL;
    }

    size_t source_len = 0;
    const unsigned char *embed_source = l2_eval_get_embedded_source(&source_len);
    if (!embed_source) {
        return NULL;
    }

    const char *source = (const char *)embed_source;
    if (source_len == 0) {
        return NULL;
    }

    char *best_copy = NULL;
    long best_len = 0;

    size_t cursor = 0;
    while (true) {
        size_t start = 0;
        size_t end = 0;
        if (!source_next_token(source, source_len, &cursor, &start, &end)) {
            break;
        }
        if (!source_token_equals(source, start, end, "word")) {
            continue;
        }

        size_t name_start = 0;
        size_t name_end = 0;
        if (!source_next_token(source, source_len, &cursor, &name_start, &name_end)) {
            break;
        }
        if (!source_token_equals(source, name_start, name_end, func_name)) {
            continue;
        }

        size_t body_start = start;
        size_t depth = 0;
        while (true) {
            size_t body_tok_start = 0;
            size_t body_tok_end = 0;
            if (!source_next_token(source, source_len, &cursor, &body_tok_start, &body_tok_end)) {
                break;
            }
            if (source_token_equals(source, body_tok_start, body_tok_end, "if") ||
                source_token_equals(source, body_tok_start, body_tok_end, "for") ||
                source_token_equals(source, body_tok_start, body_tok_end, "while") ||
                source_token_equals(source, body_tok_start, body_tok_end, "with")) {
                depth++;
                continue;
            }
            if (source_token_equals(source, body_tok_start, body_tok_end, "end")) {
                if (depth == 0) {
                    free(best_copy);
                    best_copy = copy_embedded_source_slice(body_start, body_tok_end, &best_len);
                    goto next_definition_word;
                }
                depth--;
            }
        }

next_definition_word:
        continue;
    }

    cursor = 0;
    while (true) {
        size_t start = 0;
        size_t end = 0;
        if (!source_next_token(source, source_len, &cursor, &start, &end)) {
            break;
        }
        if (!source_token_equals(source, start, end, ":asm")) {
            continue;
        }

        size_t name_start = 0;
        size_t name_end = 0;
        if (!source_next_token(source, source_len, &cursor, &name_start, &name_end)) {
            break;
        }
        if (!source_token_equals(source, name_start, name_end, func_name)) {
            continue;
        }

        size_t body_start = start;
        size_t brace_depth = 0;
        while (true) {
            size_t body_tok_start = 0;
            size_t body_tok_end = 0;
            if (!source_next_token(source, source_len, &cursor, &body_tok_start, &body_tok_end)) {
                break;
            }
            if (source_token_equals(source, body_tok_start, body_tok_end, "{")) {
                brace_depth++;
                continue;
            }
            if (source_token_equals(source, body_tok_start, body_tok_end, "}")) {
                if (brace_depth == 0) {
                    free(best_copy);
                    best_copy = copy_embedded_source_slice(body_start, body_tok_end, &best_len);
                    goto next_definition_asm;
                }
                brace_depth--;
            }
        }

next_definition_asm:
        continue;
    }

    if (best_copy && out_len) {
        *out_len = best_len;
    }
    return best_copy;
}

#if defined(__GNUC__)
extern const unsigned long long l2_source_catalog[] __attribute__((weak));
#else
extern const unsigned long long l2_source_catalog[];
#endif

/* Embedded selected-definition sources (--source-embed): exact-name lookup
 * in the compiler's catalog of final selected definitions (a count, then
 * name ptr, name bytes, source ptr, source bytes per record).  Returns NULL
 * and *out_len = 0 when the name has no selected definition with retained
 * source or no catalog was embedded; it never scans the program text. */
const char *l2_source_catalog_lookup(const char *name, long name_len, long *out_len) {
    if (out_len) {
        *out_len = 0;
    }
    if (&l2_source_catalog == NULL || l2_source_catalog == NULL || name == NULL || name_len < 0) {
        return NULL;
    }
    unsigned long long count = l2_source_catalog[0];
    const unsigned long long *record = l2_source_catalog + 1;
    for (unsigned long long i = 0; i < count; i++, record += 4) {
        if ((long)record[1] == name_len && memcmp((const char *)(uintptr_t)record[0], name, (size_t)name_len) == 0) {
            if (out_len) {
                *out_len = (long)record[3];
            }
            return (const char *)(uintptr_t)record[2];
        }
    }
    return NULL;
}

static bool l2_eval_token_candidate(const char *token) {
    if (!token || !*token) {
        return false;
    }
    if (token[0] == '"') {
        return false;
    }
    if (strlen(token) == 1 && strchr("(){}[];:+-*/%<>=!&|^~,", token[0]) != NULL) {
        return false;
    }

    int64_t parsed = 0;
    if (try_parse_int(token, &parsed)) {
        return false;
    }

    static const char *keywords[] = {
        "word", "end", ":asm", "if", "else", "for", "while", "with", "begin",
        "do", "ret", "return", "extern", "priority", "compile-time", "immediate",
        "runtime", "runtime-only", "inline", "struct", "cstruct", "macro",
        "import", "include", "bss", "list", "var", "const"
    };
    for (size_t i = 0; i < sizeof(keywords) / sizeof(keywords[0]); i++) {
        if (strcmp(token, keywords[i]) == 0) {
            return false;
        }
    }

    return true;
}

static bool l2_eval_strvec_contains(const StrVec *vec, const char *value) {
    if (!vec || !value) {
        return false;
    }
    for (size_t i = 0; i < vec->len; i++) {
        if (strcmp(vec->data[i], value) == 0) {
            return true;
        }
    }
    return false;
}

static void l2_eval_preload_word_from_embedded(
    L2EvalContext *ctx,
    const char *word_name,
    StrVec *visited,
    int depth
) {
    if (!ctx || !word_name || !*word_name || depth > 64) {
        return;
    }
    if (l2_eval_strvec_contains(visited, word_name)) {
        return;
    }
    VEC_PUSH(visited, str_dup(word_name));

    /* Already the host's own definition, read when the host program was
     * seeded: parsing it again would make it look like eval's own word, and
     * eval would interpret it instead of calling the host's code. */
    Word *existing = dictionary_lookup(&ctx->dict, word_name);
    if (existing && existing->host_seeded && existing->definition == existing->host_definition) {
        return;
    }

    long def_len = 0;
    const char *def_src = find_embedded_definition_source(word_name, &def_len);
    if (!def_src || def_len <= 0) {
        return;
    }

    size_t cursor = 0;
    size_t source_len = (size_t)def_len;
    while (true) {
        size_t tok_start = 0;
        size_t tok_end = 0;
        if (!source_next_token(def_src, source_len, &cursor, &tok_start, &tok_end)) {
            break;
        }
        size_t tok_len = tok_end - tok_start;
        char *token = (char *)xmalloc(tok_len + 1);
        memcpy(token, def_src + tok_start, tok_len);
        token[tok_len] = '\0';

        if (l2_eval_token_candidate(token) && !dictionary_lookup(&ctx->dict, token)) {
            l2_eval_preload_word_from_embedded(ctx, token, visited, depth + 1);
        }
        free(token);
    }

    parse_tokens(&ctx->parser, def_src);
}

static void l2_eval_preload_eval_dependencies(L2EvalContext *ctx, const char *source) {
    if (!ctx || !source) {
        return;
    }
    size_t embed_len = 0;
    if (!l2_eval_get_embedded_source(&embed_len) || embed_len == 0) {
        return;
    }

    StrVec visited;
    VEC_INIT(&visited);

    size_t cursor = 0;
    size_t source_len = strlen(source);
    while (true) {
        size_t tok_start = 0;
        size_t tok_end = 0;
        if (!source_next_token(source, source_len, &cursor, &tok_start, &tok_end)) {
            break;
        }

        size_t tok_len = tok_end - tok_start;
        char *token = (char *)xmalloc(tok_len + 1);
        memcpy(token, source + tok_start, tok_len);
        token[tok_len] = '\0';

        if (l2_eval_token_candidate(token)) {
            l2_eval_preload_word_from_embedded(ctx, token, &visited, 0);
        }
        free(token);
    }

    for (size_t i = 0; i < visited.len; i++) {
        free(visited.data[i]);
    }
    VEC_FREE(&visited);
}

static char *l2_default_root_dir(void) {
    const char *env_root = getenv("L2_ROOT");
    if (env_root && env_root[0] != '\0') {
        return str_dup(env_root);
    }
#ifdef L2_SOURCE_ROOT
    if (L2_SOURCE_ROOT[0] != '\0') {
        return str_dup(L2_SOURCE_ROOT);
    }
#endif
    if (__FILE__[0] == '/') {
        return path_dirname(__FILE__);
    }
    char cwd[PATH_MAX];
    if (getcwd(cwd, sizeof(cwd)) != NULL) {
        return str_dup(cwd);
    }
    return str_dup(".");
}

static char *resolve_import(const char *base_dir, const char *import_path, StrVec *include_dirs) {
    if (!import_path || !*import_path) {
        return NULL;
    }
    if (import_path[0] == '/') {
        return file_exists(import_path) ? str_dup(import_path) : NULL;
    }
    if (base_dir) {
        char *candidate = path_join(base_dir, import_path);
        if (file_exists(candidate)) {
            return candidate;
        }
        free(candidate);
    }
    if (include_dirs) {
        for (size_t i = 0; i < include_dirs->len; i++) {
            char *candidate = path_join(include_dirs->data[i], import_path);
            if (file_exists(candidate)) {
                return candidate;
            }
            free(candidate);
        }
    }
    return NULL;
}

/* The C compiler supports only the default L2 frontend: a `frontend` header
 * on the first nonblank line is a clear error rather than source syntax. */
static void reject_frontend_header(const char *content, const char *label) {
    const char *p = content;
    while (*p && isspace((unsigned char)*p)) {
        p++;
    }
    if (strncmp(p, "frontend", 8) != 0) {
        return;
    }
    char next = p[8];
    if (next == '\0' || next == '"' || next == '#' || isspace((unsigned char)next)) {
        fprintf(stderr, "[error] [frontend-header-invalid] %s selects a custom frontend; "
                        "frontend headers are not supported by the C compiler\n", label);
        exit(1);
    }
}

static char *expand_imports(const char *path, StrVec *include_dirs, StrMap *visited, FileSpanVec *spans, int *line_counter) {
    if (strmap_has(visited, path)) {
        return str_dup("");
    }
    strmap_set(visited, path, (void *)1);
    char *content = read_text_file(path);
    if (!content) {
        fprintf(stderr, "[error] failed to read %s\n", path);
        exit(1);
    }
    reject_frontend_header(content, path);
    char *base_dir = path_dirname(path);
    StrVec parts;
    VEC_INIT(&parts);
    const char *cursor = content;
    int local_line = 1;
    int span_start = *line_counter;
    int span_local_start = local_line;
    bool span_active = false;
    while (*cursor) {
        const char *line_end = strchr(cursor, '\n');
        size_t len = line_end ? (size_t)(line_end - cursor) : strlen(cursor);
        char *line = (char *)xmalloc(len + 1);
        memcpy(line, cursor, len);
        line[len] = '\0';
        char *trim = line;
        while (*trim && isspace((unsigned char)*trim)) {
            trim++;
        }
        bool is_import = false;
        if (str_starts_with(trim, "import") && (trim[6] == ' ' || trim[6] == '\t')) {
            trim += 6;
            while (*trim && isspace((unsigned char)*trim)) {
                trim++;
            }
            char *end = trim;
            while (*end && !isspace((unsigned char)*end) && *end != '#') {
                end++;
            }
            if (end > trim) {
                char *import_path = (char *)xmalloc((size_t)(end - trim) + 1);
                memcpy(import_path, trim, (size_t)(end - trim));
                import_path[end - trim] = '\0';
                char *resolved = resolve_import(base_dir, import_path, include_dirs);
                if (!resolved) {
                    fprintf(stderr, "[error] import not found: %s\n", import_path);
                    exit(1);
                }
                if (span_active) {
                    FileSpan span = {0};
                    span.path = str_dup(path);
                    span.start_line = span_start;
                    span.end_line = *line_counter;
                    span.local_start_line = span_local_start;
                    VEC_PUSH(spans, span);
                    span_active = false;
                }
                char *expanded = expand_imports(resolved, include_dirs, visited, spans, line_counter);
                if (expanded && *expanded) {
                    VEC_PUSH(&parts, expanded);
                }
                VEC_PUSH(&parts, str_dup("\n"));
                (*line_counter)++;

                const char *remainder = end;
                while (*remainder && isspace((unsigned char)*remainder)) {
                    remainder++;
                }
                if (*remainder && *remainder != '#') {
                    if (!span_active) {
                        span_start = *line_counter;
                        span_local_start = local_line;
                        span_active = true;
                    }
                    VEC_PUSH(&parts, str_dup(remainder));
                    VEC_PUSH(&parts, str_dup("\n"));
                    (*line_counter)++;
                }

                local_line++;
                free(resolved);
                free(import_path);
                is_import = true;
            }
        }
        if (!is_import) {
            if (!span_active) {
                span_start = *line_counter;
                span_local_start = local_line;
                span_active = true;
            }
            VEC_PUSH(&parts, line);
            VEC_PUSH(&parts, str_dup("\n"));
            (*line_counter)++;
            local_line++;
        } else {
            free(line);
        }
        if (!line_end) {
            break;
        }
        cursor = line_end + 1;
    }
    if (span_active) {
        FileSpan span = {0};
        span.path = str_dup(path);
        span.start_line = span_start;
        span.end_line = *line_counter;
        span.local_start_line = span_local_start;
        VEC_PUSH(spans, span);
    }
    char *out = str_join_parts(parts.data, parts.len, NULL, NULL);
    for (size_t i = 0; i < parts.len; i++) {
        free(parts.data[i]);
    }
    VEC_FREE(&parts);
    free(content);
    free(base_dir);
    return out;
}

static char *expand_imports_from_source(const char *content, const char *base_dir, StrVec *include_dirs, StrMap *visited, FileSpanVec *spans, int *line_counter) {
    reject_frontend_header(content, "source");
    const char *key = "<eval_source>";
    if (strmap_has(visited, key)) {
        return str_dup("");
    }
    strmap_set(visited, key, (void *)1);
    StrVec parts;
    VEC_INIT(&parts);
    const char *cursor = content;
    int local_line = 1;
    int span_start = *line_counter;
    int span_local_start = local_line;
    bool span_active = false;
    while (*cursor) {
        const char *line_end = strchr(cursor, '\n');
        size_t len = line_end ? (size_t)(line_end - cursor) : strlen(cursor);
        char *line = (char *)xmalloc(len + 1);
        memcpy(line, cursor, len);
        line[len] = '\0';
        char *trim = line;
        while (*trim && isspace((unsigned char)*trim)) {
            trim++;
        }
        bool is_import = false;
        if (str_starts_with(trim, "import") && (trim[6] == ' ' || trim[6] == '\t')) {
            trim += 6;
            while (*trim && isspace((unsigned char)*trim)) {
                trim++;
            }
            char *end = trim;
            while (*end && !isspace((unsigned char)*end) && *end != '#') {
                end++;
            }
            if (end > trim) {
                char *import_path = (char *)xmalloc((size_t)(end - trim) + 1);
                memcpy(import_path, trim, (size_t)(end - trim));
                import_path[end - trim] = '\0';
                char *resolved = resolve_import(base_dir, import_path, include_dirs);
                if (!resolved) {
                    fprintf(stderr, "[error] import not found: %s\n", import_path);
                    exit(1);
                }
                if (span_active) {
                    FileSpan span = {0};
                    span.path = str_dup("<eval>");
                    span.start_line = span_start;
                    span.end_line = *line_counter;
                    span.local_start_line = span_local_start;
                    VEC_PUSH(spans, span);
                    span_active = false;
                }
                char *expanded = expand_imports(resolved, include_dirs, visited, spans, line_counter);
                if (expanded && *expanded) {
                    VEC_PUSH(&parts, expanded);
                }
                VEC_PUSH(&parts, str_dup("\n"));
                (*line_counter)++;

                const char *remainder = end;
                while (*remainder && isspace((unsigned char)*remainder)) {
                    remainder++;
                }
                if (*remainder && *remainder != '#') {
                    if (!span_active) {
                        span_start = *line_counter;
                        span_local_start = local_line;
                        span_active = true;
                    }
                    VEC_PUSH(&parts, str_dup(remainder));
                    VEC_PUSH(&parts, str_dup("\n"));
                    (*line_counter)++;
                }

                local_line++;
                free(resolved);
                free(import_path);
                is_import = true;
            }
        }
        if (!is_import) {
            if (!span_active) {
                span_start = *line_counter;
                span_local_start = local_line;
                span_active = true;
            }
            VEC_PUSH(&parts, line);
            VEC_PUSH(&parts, str_dup("\n"));
            (*line_counter)++;
            local_line++;
        } else {
            free(line);
        }
        if (!line_end) {
            break;
        }
        cursor = line_end + 1;
    }
    if (span_active) {
        FileSpan span = {0};
        span.path = str_dup("<eval>");
        span.start_line = span_start;
        span.end_line = *line_counter;
        span.local_start_line = span_local_start;
        VEC_PUSH(spans, span);
    }
    char *out = str_join_parts(parts.data, parts.len, NULL, NULL);
    for (size_t i = 0; i < parts.len; i++) {
        free(parts.data[i]);
    }
    VEC_FREE(&parts);
    return out;
}

static bool parse_string_literal(const char *lexeme, char **out) {
    size_t len = strlen(lexeme);
    if (len < 2 || lexeme[0] != '"' || lexeme[len - 1] != '"') {
        return false;
    }
    const char *body = lexeme + 1;
    size_t body_len = len - 2;
    char *buf = (char *)xmalloc(body_len + 1);
    size_t pos = 0;
    for (size_t i = 0; i < body_len; i++) {
        char ch = body[i];
        if (ch != '\\') {
            buf[pos++] = ch;
            continue;
        }
        i++;
        if (i >= body_len) {
            fprintf(stderr, "[error] unterminated escape sequence\n");
            exit(1);
        }
        char esc = body[i];
        if (esc == 'n') {
            buf[pos++] = '\n';
        } else if (esc == 't') {
            buf[pos++] = '\t';
        } else if (esc == 'r') {
            buf[pos++] = '\r';
        } else if (esc == '0') {
            buf[pos++] = '\0';
        } else if (esc == '"') {
            buf[pos++] = '"';
        } else if (esc == '\\') {
            buf[pos++] = '\\';
        } else {
            fprintf(stderr, "[error] unsupported escape sequence \\%c\n", esc);
            exit(1);
        }
    }
    buf[pos] = '\0';
    *out = buf;
    return true;
}

static bool try_parse_int(const char *lexeme, int64_t *out) {
    char *end = NULL;
    errno = 0;
    long long val = strtoll(lexeme, &end, 0);
    if (errno != 0 || !end || *end != '\0') {
        return false;
    }
    *out = (int64_t)val;
    return true;
}

static bool try_parse_float(const char *lexeme, double *out) {
    if (!strchr(lexeme, '.') && !strchr(lexeme, 'e') && !strchr(lexeme, 'E')) {
        return false;
    }
    char *end = NULL;
    errno = 0;
    double val = strtod(lexeme, &end);
    if (errno != 0 || !end || *end != '\0') {
        return false;
    }
    *out = val;
    return true;
}

static void parser_inject_tokens(Parser *parser, TokenVec *injected) {
    if (!injected || injected->len == 0) {
        return;
    }
    if (parser->pos > parser->tokens.len) {
        parser->pos = parser->tokens.len;
    }
    size_t new_len = parser->tokens.len + injected->len;
    if (new_len > parser->tokens.cap) {
        parser->tokens.cap = new_len + 16;
        parser->tokens.data = xrealloc(parser->tokens.data, parser->tokens.cap * sizeof(Token));
    }
    memmove(&parser->tokens.data[parser->pos + injected->len],
            &parser->tokens.data[parser->pos],
            (parser->tokens.len - parser->pos) * sizeof(Token));
    for (size_t i = 0; i < injected->len; i++) {
        parser->tokens.data[parser->pos + i] = injected->data[i];
    }
    parser->tokens.len = new_len;
}

static void parser_start_macro(Parser *parser, const char *name, int param_count) {
    if (parser->macro_recording.active) {
        fprintf(stderr, "[error] nested macro definitions are not supported\n");
        exit(1);
    }
    parser->macro_recording.active = true;
    parser->macro_recording.name = str_dup(name);
    VEC_INIT(&parser->macro_recording.tokens);
    parser->macro_recording.param_count = param_count;
}

static void parser_finish_macro(Parser *parser) {
    if (!parser->macro_recording.active) {
        fprintf(stderr, "[error] unexpected ';' closing a macro\n");
        exit(1);
    }
    Word *word = (Word *)xmalloc(sizeof(Word));
    memset(word, 0, sizeof(Word));
    word->name = str_dup(parser->macro_recording.name);
    word->macro_expansion = (char **)xmalloc((parser->macro_recording.tokens.len + 1) * sizeof(char *));
    word->macro_param_count = parser->macro_recording.param_count;
    for (size_t i = 0; i < parser->macro_recording.tokens.len; i++) {
        word->macro_expansion[i] = str_dup(parser->macro_recording.tokens.data[i]);
    }
    word->macro_expansion[parser->macro_recording.tokens.len] = NULL;
    dictionary_register(parser->dictionary, word);
    parser->macro_recording.active = false;
    memset(&parser->macro_body, 0, sizeof(parser->macro_body));
    parser->pending_quotes = NULL;
    parser->pending_quote_len = 0;
}

static void parser_emit_literal(Parser *parser, LiteralKind kind, int64_t i64, double f64, const char *str) {
    Op op = {0};
    op.kind = OP_LITERAL;
    op.lit_kind = kind;
    if (kind == LIT_INT) {
        op.data.i64 = i64;
    } else if (kind == LIT_FLOAT) {
        op.data.f64 = f64;
    } else {
        op.data.str = str_dup(str);
    }
    parser_emit_op(parser, op);
}

static void parser_handle_token(Parser *parser, Token token);

/* Whether `lex` can be part of the return type and name before the `(` of
 * a C-style extern (`unsigned`, `char*`, `*name`).  Counts, L2 keywords and
 * anything else end the C form: it is then a legacy `extern name` and the
 * parser must not read on into the declarations that follow. */
static bool c_extern_prefix_token(const char *lex) {
    static const char *stop_words[] = {
        "word", "end", ":asm", ":py", "extern", "import", "priority", "macro", "inline",
        "preserve", "ret", "if", "else", "for", "while", "do", "begin", "again", "break",
        "continue", "match",
    };
    if (!lex || !*lex) {
        return false;
    }
    for (size_t i = 0; i < sizeof(stop_words) / sizeof(stop_words[0]); i++) {
        if (strcmp(lex, stop_words[i]) == 0) {
            return false;
        }
    }
    bool has_ident = false;
    for (const char *p = lex; *p; p++) {
        unsigned char ch = (unsigned char)*p;
        if (ch == '*') {
            continue;
        }
        if (isalnum(ch) || ch == '_') {
            if (!has_ident && isdigit(ch)) {
                return false;
            }
            has_ident = true;
            continue;
        }
        return false;
    }
    return true;
}

/* Join C type tokens into one type name: qualifiers dropped, words joined
 * by spaces, pointer stars appended (`const char *` -> "char*"). */
static char *c_extern_type_name(char **parts, size_t count, int extra_stars) {
    size_t cap = 16;
    size_t len = 0;
    int stars = extra_stars;
    char *out = (char *)xmalloc(cap);
    out[0] = '\0';
    for (size_t i = 0; i < count; i++) {
        const char *part = parts[i];
        char word[256];
        size_t wlen = 0;
        for (const char *p = part; *p; p++) {
            if (*p == '*') {
                stars++;
            } else if (wlen + 1 < sizeof(word)) {
                word[wlen++] = *p;
            }
        }
        word[wlen] = '\0';
        if (wlen == 0 || strcmp(word, "const") == 0 || strcmp(word, "volatile") == 0 ||
            strcmp(word, "restrict") == 0 || strcmp(word, "register") == 0) {
            continue;
        }
        size_t need = len + wlen + 2;
        if (need > cap) {
            while (cap < need) cap *= 2;
            out = (char *)xrealloc(out, cap);
        }
        if (len) {
            out[len++] = ' ';
        }
        memcpy(out + len, word, wlen);
        len += wlen;
        out[len] = '\0';
    }
    if (len == 0) {
        free(out);
        out = str_dup("long");
        len = strlen(out);
        cap = len + 1;
    }
    if (stars) {
        out = (char *)xrealloc(out, len + (size_t)stars + 1);
        for (int i = 0; i < stars; i++) {
            out[len++] = '*';
        }
        out[len] = '\0';
    }
    return out;
}

/* `extern <return type> <name>(<params>)`, with multi-token types such as
 * `const char *name(const char *s, long n)`, `long long`, `struct Pair p`
 * and a trailing `...`.  `first` is the token after `extern`.  Returns false,
 * with the input position unchanged, when this is not the C form. */
static bool parser_parse_c_extern(Parser *parser, Token first) {
    size_t saved_pos = parser->pos;
    StrVec prefix;
    VEC_INIT(&prefix);
    if (!c_extern_prefix_token(first.lexeme)) {
        VEC_FREE(&prefix);
        return false;
    }
    VEC_PUSH(&prefix, first.lexeme);
    while (true) {
        Token tok = parser_peek_token(parser);
        if (!tok.lexeme) {
            parser->pos = saved_pos;
            VEC_FREE(&prefix);
            return false;
        }
        if (strcmp(tok.lexeme, "(") == 0) {
            parser_next_token(parser);
            break;
        }
        if (!c_extern_prefix_token(tok.lexeme)) {
            parser->pos = saved_pos;
            VEC_FREE(&prefix);
            return false;
        }
        VEC_PUSH(&prefix, tok.lexeme);
        parser_next_token(parser);
    }
    /* The name is the last token, without its leading pointer stars. */
    const char *name_tok = prefix.data[prefix.len - 1];
    int ret_stars = 0;
    while (*name_tok == '*') {
        name_tok++;
        ret_stars++;
    }
    if (!*name_tok || strchr(name_tok, '*') != NULL) {
        fprintf(stderr, "[error] extern expected a function name before '(' but got '%s'\n",
                prefix.data[prefix.len - 1]);
        exit(1);
    }
    char *name = str_dup(name_tok);
    char *ret_type = c_extern_type_name(prefix.data, prefix.len - 1, ret_stars);
    VEC_FREE(&prefix);

    Word *word = dictionary_lookup(parser->dictionary, name);
    if (!word) {
        word = (Word *)xmalloc(sizeof(Word));
        memset(word, 0, sizeof(Word));
        word->name = str_dup(name);
        dictionary_register(parser->dictionary, word);
    }
    word->is_extern = true;
    word->extern_ret_type = ret_type;
    parser->uses_libc = true;
    bool is_printf = strcmp(name, "printf") == 0;
    if (!is_printf && (strcmp(ret_type, "double") == 0 || strcmp(ret_type, "float") == 0)) {
        parser->uses_libm = true;
    }
    word->extern_arg_types = NULL;
    word->extern_arg_count = 0;
    int cap = 0;
    Token peek = parser_peek_token(parser);
    if (peek.lexeme && strcmp(peek.lexeme, ")") == 0) {
        parser_next_token(parser);
        free(name);
        return true;
    }
    while (true) {
        StrVec parts;
        VEC_INIT(&parts);
        Token sep = {0};
        while (true) {
            Token tok = parser_next_token(parser);
            if (!tok.lexeme) {
                fprintf(stderr, "[error] unterminated extern signature for '%s'\n", name);
                exit(1);
            }
            if (strcmp(tok.lexeme, ",") == 0 || strcmp(tok.lexeme, ")") == 0) {
                sep = tok;
                break;
            }
            VEC_PUSH(&parts, tok.lexeme);
        }
        bool closing = strcmp(sep.lexeme, ")") == 0;
        if (parts.len == 1 && strcmp(parts.data[0], "...") == 0) {
            VEC_FREE(&parts);
            if (!closing) {
                fprintf(stderr, "[error] expected ')' after '...' in extern '%s'\n", name);
                exit(1);
            }
            break;
        }
        if (parts.len == 0) {
            fprintf(stderr, "[error] missing parameter type in extern '%s'\n", name);
            exit(1);
        }
        if (parts.len == 1 && strcmp(parts.data[0], "void") == 0 && word->extern_arg_count == 0 && closing) {
            VEC_FREE(&parts);
            break;
        }
        /* Drop a trailing parameter name (`char *s`: the stars stay with the type). */
        size_t type_count = parts.len;
        int param_stars = 0;
        if (parts.len > 1) {
            const char *last = parts.data[parts.len - 1];
            while (*last == '*') {
                last++;
                param_stars++;
            }
            if (*last && strchr(last, '*') == NULL) {
                type_count--;
            } else {
                param_stars = 0;
            }
        }
        char *type = c_extern_type_name(parts.data, type_count, param_stars);
        VEC_FREE(&parts);
        if (word->extern_arg_count + 1 > cap) {
            cap = cap ? cap * 2 : 4;
            word->extern_arg_types = xrealloc(word->extern_arg_types, (size_t)cap * sizeof(char *));
        }
        word->extern_arg_types[word->extern_arg_count++] = type;
        if (!is_printf && (strcmp(type, "double") == 0 || strcmp(type, "float") == 0)) {
            parser->uses_libm = true;
        }
        if (closing) {
            break;
        }
    }
    free(name);
    return true;
}

/* Parse `source`.  With `continue_state` false this starts a fresh parse
 * (resetting definition, control, label and hook state).  With it true the
 * parser keeps that state, so parsing A and then continuing with B behaves
 * like one parse of A followed by B; the eval warm cache relies on this to
 * parse stdlib once and then only the user code. */
static void parser_finish_macro_body(Parser *parser);
static void parser_parse_quotation(Parser *parser);
static void parser_emit_syntax(Parser *parser, QuoteTemplate *tpl);

static void parse_tokens_impl(Parser *parser, const char *source, bool continue_state) {
    parser->source = str_dup(source);
    tokenizer_init(&parser->tokenizer, parser->reader, source);
    parser->tokenizer_exhausted = false;
    /* Drop tokens buffered from a previous source.  Without this, resetting
     * pos to 0 made a second parse on the same parser replay every token of
     * the earlier source before reaching the new one. */
    parser->tokens.len = 0;
    parser->pos = 0;
    if (!continue_state) {
        parser->current_def = NULL;
        parser->control_len = 0;
        parser->label_counter = 0;
        parser->token_hook = NULL;
        parser->has_last_token = false;
        parser->custom_prelude = NULL;
        parser->custom_data = NULL;
        parser->custom_bss = NULL;
        parser->bss_replaced = false;
        parser->pending_inline_def = false;
        parser->pending_preserve_def = false;
    }

    while (!parser_eof(parser)) {
        Token token = parser_next_token(parser);
        if (!token.lexeme) {
            break;
        }
        if (parser->macro_recording.active) {
            if (strcmp(token.lexeme, ";") == 0) {
                parser_finish_macro(parser);
            } else {
                VEC_PUSH(&parser->macro_recording.tokens, str_dup(token.lexeme));
            }
            continue;
        }
        if (parser->macro_body.active && strcmp(token.lexeme, ";") == 0
                && parser->control_len == parser->macro_body.control_depth) {
            parser_finish_macro_body(parser);
            continue;
        }
        if (parser->current_def && strcmp(token.lexeme, "`") == 0) {
            parser_parse_quotation(parser);
            continue;
        }
        if (strncmp(token.lexeme, L2_QUOTE_MARKER, strlen(L2_QUOTE_MARKER)) == 0) {
            size_t index = (size_t)strtoul(token.lexeme + strlen(L2_QUOTE_MARKER), NULL, 10);
            parser_emit_syntax(parser, parser->pending_quotes[index]);
            continue;
        }
        if (parser->macro_body.active && token.lexeme[0] == '$' && token.lexeme[1]) {
            bool bound = false;
            for (int i = 0; i < parser->macro_body.param_count && !bound; i++) {
                if (strcmp(token.lexeme + 1, parser->macro_body.params[i]) == 0) {
                    Op op = {0};
                    op.kind = OP_BINDING;
                    op.data.word = str_dup(token.lexeme + 1);
                    parser_emit_op(parser, op);
                    bound = true;
                }
            }
            if (bound) continue;
        }
        if (strcmp(token.lexeme, "[") == 0) {
            Op op = {0};
            op.kind = OP_LIST_BEGIN;
            op.data.label = parser_new_label(parser, "list");
            parser_emit_op(parser, op);
            parser_push_control(parser, "list");
            parser->control_stack[parser->control_len - 1].begin_label = op.data.label;
            continue;
        }
        if (strcmp(token.lexeme, "]") == 0) {
            if (!parser->control_len || strcmp(parser->control_stack[parser->control_len - 1].type, "list") != 0) {
                fprintf(stderr, "[error] mismatched ']'\n");
                exit(1);
            }
            char *label = parser->control_stack[parser->control_len - 1].begin_label;
            parser->control_len--;
            Op op = {0};
            op.kind = OP_LIST_END;
            op.data.label = str_dup(label);
            parser_emit_op(parser, op);
            continue;
        }
        if (strcmp(token.lexeme, "word") == 0) {
            Token name_tok = parser_next_token(parser);
            if (!name_tok.lexeme) {
                fprintf(stderr, "[error] definition name missing after 'word'\n");
                exit(1);
            }
            Definition *def = (Definition *)xmalloc(sizeof(Definition));
            memset(def, 0, sizeof(Definition));
            def->name = str_dup(name_tok.lexeme);
            VEC_INIT(&def->body);
            def->terminator = str_dup("end");
            def->inline_def = parser->pending_inline_def;
            def->preserve_def = parser->pending_preserve_def;
            parser->pending_inline_def = false;
            parser->pending_preserve_def = false;
            parser->current_def = def;
            Word *word = dictionary_lookup(parser->dictionary, def->name);
            if (!word) {
                word = (Word *)xmalloc(sizeof(Word));
                memset(word, 0, sizeof(Word));
                word->name = str_dup(def->name);
                dictionary_register(parser->dictionary, word);
            }
            word->prev_definition = word->definition;
            word->prev_asm_def = word->asm_def;
            word->immediate = false;
            word->compile_only = false;
            word->runtime_only = false;
            word->definition = def;
            word->asm_def = NULL;
            word->inline_def = def->inline_def;
            if (parser->definition_stack_len + 1 > parser->definition_stack_cap) {
                parser->definition_stack_cap = parser->definition_stack_cap ? parser->definition_stack_cap * 2 : 8;
                parser->definition_stack = xrealloc(parser->definition_stack, parser->definition_stack_cap * sizeof(Word *));
            }
            parser->definition_stack[parser->definition_stack_len++] = word;
            continue;
        }
        if (strcmp(token.lexeme, "end") == 0) {
            if (parser->control_len && parser->control_stack[parser->control_len - 1].frame) {
                parser_close_ct_frame(parser);
                continue;
            }
            if (parser->control_len) {
                const char *type = parser->control_stack[parser->control_len - 1].type;
                if (strcmp(type, "if") == 0 || strcmp(type, "elif") == 0) {
                    if (parser->control_stack[parser->control_len - 1].false_label) {
                        Op op = {0};
                        op.kind = OP_LABEL;
                        op.data.label = str_dup(parser->control_stack[parser->control_len - 1].false_label);
                        parser_emit_op(parser, op);
                    }
                    if (parser->control_stack[parser->control_len - 1].end_label) {
                        Op op = {0};
                        op.kind = OP_LABEL;
                        op.data.label = str_dup(parser->control_stack[parser->control_len - 1].end_label);
                        parser_emit_op(parser, op);
                    }
                    parser->control_len--;
                    continue;
                }
                if (strcmp(type, "else") == 0) {
                    Op op = {0};
                    op.kind = OP_LABEL;
                    op.data.label = str_dup(parser->control_stack[parser->control_len - 1].end_label);
                    parser_emit_op(parser, op);
                    parser->control_len--;
                    continue;
                }
                if (strcmp(type, "begin") == 0) {
                    Op op = {0};
                    op.kind = OP_JUMP;
                    op.data.label = str_dup(parser->control_stack[parser->control_len - 1].begin_label);
                    parser_emit_op(parser, op);
                    op.kind = OP_LABEL;
                    op.data.label = str_dup(parser->control_stack[parser->control_len - 1].end_label);
                    parser_emit_op(parser, op);
                    parser->control_len--;
                    continue;
                }
                if (strcmp(type, "for") == 0) {
                    Op op = {0};
                    op.kind = OP_FOR_END;
                    op.data.loop.loop = str_dup(parser->control_stack[parser->control_len - 1].loop_label);
                    op.data.loop.end = str_dup(parser->control_stack[parser->control_len - 1].end_label);
                    parser_emit_op(parser, op);
                    parser->control_len--;
                    continue;
                }
                if (strcmp(type, "with") == 0) {
                    StrVec *with_names = &parser->control_stack[parser->control_len - 1].with_names;
                    for (size_t i = 0; i < with_names->len; i++) {
                        const char *name = with_names->data[i];
                        strmap_set(&parser->variable_words, name, NULL);
                        free(with_names->data[i]);
                    }
                    VEC_FREE(with_names);
                    parser->control_len--;
                    continue;
                }
            }
            if (parser->current_def) {
                Definition *def = parser->current_def;
                Word *word = parser->definition_stack[parser->definition_stack_len - 1];
                def->immediate = word->immediate;
                def->compile_only = word->compile_only;
                def->runtime_only = word->runtime_only;
                def->inline_def = word->inline_def;
                Form form = {0};
                form.kind = FORM_DEF;
                form.ptr = def;
                VEC_PUSH(&parser->module.forms, form);
                parser->current_def = NULL;
                parser->definition_stack_len--;
                parser->last_defined = word;
                continue;
            }
            fprintf(stderr, "[error] unexpected 'end'\n");
            exit(1);
        }
        if (strcmp(token.lexeme, ":asm") == 0) {
            bool inline_def = parser->pending_inline_def;
            bool preserve_def = parser->pending_preserve_def;
            parser->pending_inline_def = false;
            parser->pending_preserve_def = false;
            Token name_tok = parser_next_token(parser);
            if (!name_tok.lexeme) {
                fprintf(stderr, "[error] definition name missing after ':asm'\n");
                exit(1);
            }
            bool effect_string_io = false;
            Token brace = parser_next_token(parser);
            if (brace.lexeme && strcmp(brace.lexeme, "(") == 0) {
                while (!parser_eof(parser)) {
                    Token meta = parser_next_token(parser);
                    if (!meta.lexeme) {
                        break;
                    }
                    if (strcmp(meta.lexeme, ")") == 0) {
                        break;
                    }
                    if (strcmp(meta.lexeme, "string-io") == 0) {
                        effect_string_io = true;
                    }
                }
                brace = parser_next_token(parser);
            }
            if (!brace.lexeme || strcmp(brace.lexeme, "{") != 0) {
                fprintf(stderr, "[error] expected '{' after asm name, got '%s'\n", brace.lexeme ? brace.lexeme : "<eof>");
                exit(1);
            }
            size_t body_start = (size_t)brace.end;
            size_t body_end = body_start;
            /* Collect the body tokens; used as fallback when the tokens
             * were injected (via ct-parser-inject) and don't carry valid
             * source positions we can slice from parser->source. */
            StrVec asm_body_toks;
            VEC_INIT(&asm_body_toks);
            while (!parser_eof(parser)) {
                Token next = parser_next_token(parser);
                if (next.lexeme && strcmp(next.lexeme, "}") == 0) {
                    body_end = (size_t)next.start;
                    break;
                }
                if (next.lexeme) {
                    VEC_PUSH(&asm_body_toks, str_dup(next.lexeme));
                }
            }
            char *body = NULL;
            if (body_end > body_start && parser->source) {
                size_t body_len = body_end - body_start;
                body = (char *)xmalloc(body_len + 1);
                memcpy(body, parser->source + body_start, body_len);
                body[body_len] = '\0';
            } else if (asm_body_toks.len > 0) {
                /* Reconstruct body from tokens (injected via ct-parser-inject). */
                body = str_join_parts(asm_body_toks.data, asm_body_toks.len, " ", NULL);
            } else {
                fprintf(stderr, "[error] missing '}' to terminate asm body\n");
                exit(1);
            }
            for (size_t i = 0; i < asm_body_toks.len; i++) {
                free(asm_body_toks.data[i]);
            }
            VEC_FREE(&asm_body_toks);
            AsmDefinition *def = (AsmDefinition *)xmalloc(sizeof(AsmDefinition));
            memset(def, 0, sizeof(AsmDefinition));
            def->name = str_dup(name_tok.lexeme);
            def->body = body;
            def->effect_string_io = effect_string_io;
            def->inline_def = inline_def;
            def->preserve_def = preserve_def;
            Token term = parser_next_token(parser);
            if (!term.lexeme || strcmp(term.lexeme, ";") != 0) {
                fprintf(stderr, "[error] expected ';' after asm definition\n");
                exit(1);
            }
            Word *word = dictionary_lookup(parser->dictionary, def->name);
            if (!word) {
                word = (Word *)xmalloc(sizeof(Word));
                memset(word, 0, sizeof(Word));
                word->name = str_dup(def->name);
                dictionary_register(parser->dictionary, word);
            }
            word->prev_definition = word->definition;
            word->prev_asm_def = word->asm_def;
            word->immediate = false;
            word->compile_only = false;
            word->runtime_only = false;
            word->asm_def = def;
            word->definition = NULL;
            word->inline_def = def->inline_def;
            Form form = {0};
            form.kind = FORM_ASM;
            form.ptr = def;
            VEC_PUSH(&parser->module.forms, form);
            parser->last_defined = word;
            continue;
        }
        if (strcmp(token.lexeme, "extern") == 0) {
            Token tok1 = parser_next_token(parser);
            if (!tok1.lexeme) {
                fprintf(stderr, "[error] extern missing name or return type\n");
                exit(1);
            }
            Token peek = parser_peek_token(parser);
            if (peek.lexeme && isdigit((unsigned char)peek.lexeme[0])) {
                Word *word = dictionary_lookup(parser->dictionary, tok1.lexeme);
                if (!word) {
                    word = (Word *)xmalloc(sizeof(Word));
                    memset(word, 0, sizeof(Word));
                    word->name = str_dup(tok1.lexeme);
                    dictionary_register(parser->dictionary, word);
                }
                word->is_extern = true;
                parser_next_token(parser);
                word->extern_inputs = atoi(peek.lexeme);
                Token next = parser_peek_token(parser);
                if (next.lexeme && isdigit((unsigned char)next.lexeme[0])) {
                    parser_next_token(parser);
                    word->extern_outputs = atoi(next.lexeme);
                } else {
                    word->extern_outputs = 0;
                }
                continue;
            }
            if (parser_parse_c_extern(parser, tok1)) {
                continue;
            }
            /* Legacy `extern name` without counts: nothing else to read. */
            Word *word = dictionary_lookup(parser->dictionary, tok1.lexeme);
            if (!word) {
                word = (Word *)xmalloc(sizeof(Word));
                memset(word, 0, sizeof(Word));
                word->name = str_dup(tok1.lexeme);
                dictionary_register(parser->dictionary, word);
            }
            word->is_extern = true;
            continue;
        }
        if (parser_control_overridden(parser, token.lexeme)) {
            parser_handle_token(parser, token);
            continue;
        }
        if (strcmp(token.lexeme, "if") == 0) {
            char *false_label = parser_new_label(parser, "if_false");
            Op op = {0};
            op.kind = OP_BRANCH_ZERO;
            op.data.label = str_dup(false_label);
            parser_emit_op(parser, op);
            parser_push_control(parser, "if");
            parser->control_stack[parser->control_len - 1].false_label = false_label;
            continue;
        }
        if (strcmp(token.lexeme, "else") == 0) {
            if (!parser->control_len || (strcmp(parser->control_stack[parser->control_len - 1].type, "if") != 0 && strcmp(parser->control_stack[parser->control_len - 1].type, "elif") != 0)) {
                fprintf(stderr, "[error] 'else' without matching if\n");
                exit(1);
            }
            char *end_label = parser->control_stack[parser->control_len - 1].end_label;
            if (!end_label) {
                end_label = parser_new_label(parser, "if_end");
            }
            Op jump = {0};
            jump.kind = OP_JUMP;
            jump.data.label = str_dup(end_label);
            parser_emit_op(parser, jump);
            Op label = {0};
            label.kind = OP_LABEL;
            label.data.label = str_dup(parser->control_stack[parser->control_len - 1].false_label);
            parser_emit_op(parser, label);
            Token next = parser_peek_token(parser);
            if (next.lexeme && next.line == token.line && strcmp(next.lexeme, "if") != 0) {
                TokenVec cond_tokens;
                VEC_INIT(&cond_tokens);
                bool shorthand = false;
                while (!parser_eof(parser)) {
                    Token cond = parser_next_token(parser);
                    if (!cond.lexeme) {
                        break;
                    }
                    if (cond.line != token.line) {
                        VEC_PUSH(&cond_tokens, cond);
                        break;
                    }
                    if (strcmp(cond.lexeme, "if") == 0) {
                        shorthand = true;
                        break;
                    }
                    VEC_PUSH(&cond_tokens, cond);
                }
                if (shorthand) {
                    for (size_t i = 0; i < cond_tokens.len; i++) {
                        parser_handle_token(parser, cond_tokens.data[i]);
                    }
                    char *false_label = parser_new_label(parser, "if_false");
                    Op br = {0};
                    br.kind = OP_BRANCH_ZERO;
                    br.data.label = str_dup(false_label);
                    parser_emit_op(parser, br);
                    parser->control_stack[parser->control_len - 1].type = str_dup("elif");
                    parser->control_stack[parser->control_len - 1].false_label = false_label;
                    parser->control_stack[parser->control_len - 1].end_label = end_label;
                } else {
                    /* Not a chain: read the tokens again as the else body.
                     * They were consumed in order, so step back over them;
                     * injecting them would leave a second copy sharing the
                     * same lexemes in the token vector. */
                    parser->pos -= cond_tokens.len;
                    parser->last_token = token;
                    parser->has_last_token = true;
                    VEC_FREE(&cond_tokens);
                    parser->control_stack[parser->control_len - 1].type = str_dup("else");
                    parser->control_stack[parser->control_len - 1].end_label = end_label;
                }
            } else {
                parser->control_stack[parser->control_len - 1].type = str_dup("else");
                parser->control_stack[parser->control_len - 1].end_label = end_label;
            }
            continue;
        }
        if (strcmp(token.lexeme, "for") == 0) {
            char *loop_label = parser_new_label(parser, "for_loop");
            char *end_label = parser_new_label(parser, "for_end");
            Op op = {0};
            op.kind = OP_FOR_BEGIN;
            op.data.loop.loop = str_dup(loop_label);
            op.data.loop.end = str_dup(end_label);
            parser_emit_op(parser, op);
            parser_push_control(parser, "for");
            parser->control_stack[parser->control_len - 1].loop_label = loop_label;
            parser->control_stack[parser->control_len - 1].end_label = end_label;
            continue;
        }
        if (strcmp(token.lexeme, "while") == 0) {
            char *begin_label = parser_new_label(parser, "begin");
            char *end_label = parser_new_label(parser, "end");
            Op label = {0};
            label.kind = OP_LABEL;
            label.data.label = str_dup(begin_label);
            parser_emit_op(parser, label);
            parser_push_control(parser, "begin");
            parser->control_stack[parser->control_len - 1].begin_label = begin_label;
            parser->control_stack[parser->control_len - 1].end_label = end_label;
            continue;
        }
        if (strcmp(token.lexeme, "do") == 0) {
            if (!parser->control_len || strcmp(parser->control_stack[parser->control_len - 1].type, "begin") != 0) {
                fprintf(stderr, "[error] 'do' without matching while\n");
                exit(1);
            }
            Op op = {0};
            op.kind = OP_BRANCH_ZERO;
            op.data.label = str_dup(parser->control_stack[parser->control_len - 1].end_label);
            parser_emit_op(parser, op);
            continue;
        }
        parser_handle_token(parser, token);
    }
    if (parser->macro_recording.active) {
        fprintf(stderr, "[error] unterminated macro definition\n");
        exit(1);
    }
    if (parser->control_len) {
        fprintf(stderr, "[error] unclosed control structure\n");
        exit(1);
    }
    if (parser->current_def) {
        fprintf(stderr, "[error] unclosed definition at EOF\n");
        exit(1);
    }
}

static void parse_tokens(Parser *parser, const char *source) {
    parse_tokens_impl(parser, source, false);
}

static void parse_tokens_continue(Parser *parser, const char *source) {
    parse_tokens_impl(parser, source, true);
}


static bool quote_is_separator(const char *lexeme) {
    static const char *seps[] = {"`", "(", ")", "{", "}", "[", "]", ";", ",", NULL};
    for (int i = 0; seps[i]; i++) {
        if (strcmp(lexeme, seps[i]) == 0) return true;
    }
    return false;
}

static bool quote_opens_unquote(TokenVec *tokens, size_t index) {
    const char *lex = tokens->data[index].lexeme;
    size_t len = strlen(lex);
    if (!len || lex[len - 1] != '$' || lex[0] == '\\' || index + 1 >= tokens->len) return false;
    Token *next = &tokens->data[index + 1];
    return strcmp(next->lexeme, "(") == 0 && next->start == tokens->data[index].end && next->end > 0;
}

static bool quote_splice_name(const char *lexeme) {
    if (lexeme[0] != '$' || !lexeme[1]) return false;
    return l2_is_identifier(lexeme + 1);
}

static void parser_emit_syntax(Parser *parser, QuoteTemplate *tpl) {
    Op op = {0};
    op.kind = OP_SYNTAX;
    op.data.ptr = tpl;
    parser_emit_op(parser, op);
}

/* A backtick quotation in a definition or macro body (the opening backtick
 * is consumed).  `$( )` expressions are parsed as ordinary code before the
 * quotation op, each between the unquote begin/end words, as in the Python
 * compiler. */
static void parser_parse_quotation(Parser *parser) {
    TokenVec inner;
    VEC_INIT(&inner);
    int depth = 0;
    for (;;) {
        Token tok = parser_next_token(parser);
        if (!tok.lexeme) {
            fprintf(stderr, "[error] unterminated quotation\n");
            exit(1);
        }
        if (depth == 0 && strcmp(tok.lexeme, "`") == 0) break;
        VEC_PUSH(&inner, tok);
        if (depth > 0) {
            if (strcmp(tok.lexeme, "(") == 0) depth++;
            else if (strcmp(tok.lexeme, ")") == 0) depth--;
            continue;
        }
        size_t len = strlen(tok.lexeme);
        if (len && tok.lexeme[len - 1] == '$' && tok.lexeme[0] != '\\') {
            Token next = parser_peek_token(parser);
            if (next.lexeme && strcmp(next.lexeme, "(") == 0 && next.start == tok.end && next.end > 0) {
                VEC_PUSH(&inner, parser_next_token(parser));
                depth = 1;
            }
        }
    }
    QuoteTemplate *tpl = (QuoteTemplate *)xmalloc(sizeof(QuoteTemplate));
    tpl->parts = (QuotePart *)xmalloc((inner.len + 1) * sizeof(QuotePart));
    tpl->len = 0;
    tpl->unquotes = 0;
    TokenVec injected;
    VEC_INIT(&injected);
    for (size_t i = 0; i < inner.len;) {
        Token *tok = &inner.data[i];
        QuotePart *part = &tpl->parts[tpl->len++];
        memset(part, 0, sizeof(*part));
        part->line = tok->line;
        part->column = tok->column;
        if (quote_opens_unquote(&inner, i)) {
            size_t close = i + 1;
            int d = 0;
            for (; close < inner.len; close++) {
                if (strcmp(inner.data[close].lexeme, "(") == 0) d++;
                else if (strcmp(inner.data[close].lexeme, ")") == 0 && --d == 0) break;
            }
            size_t len = strlen(tok->lexeme);
            part->kind = 2;
            part->prefix = (char *)xmalloc(len);
            memcpy(part->prefix, tok->lexeme, len - 1);
            part->prefix[len - 1] = '\0';
            size_t after = close + 1;
            if (after < inner.len && inner.data[after].start == inner.data[close].end && inner.data[after].end > 0
                    && !quote_is_separator(inner.data[after].lexeme) && !quote_opens_unquote(&inner, after)) {
                part->suffix = str_dup(inner.data[after].lexeme);
                after++;
            }
            Token begin = token_copy_with(tok, L2_UNQUOTE_BEGIN);
            VEC_PUSH(&injected, begin);
            for (size_t k = i + 2; k < close; k++) VEC_PUSH(&injected, inner.data[k]);
            VEC_PUSH(&injected, token_copy_with(tok, L2_UNQUOTE_END));
            tpl->unquotes++;
            i = after;
            continue;
        }
        const char *lex = tok->lexeme;
        if (strcmp(lex, "\\`") == 0) {
            part->lexeme = str_dup("`");
        } else if (strcmp(lex, "\\\\") == 0) {
            part->lexeme = str_dup("\\");
        } else if (lex[0] == '\\' && lex[1] == '$') {
            part->lexeme = str_dup(lex + 1);
        } else if (quote_splice_name(lex)) {
            part->kind = 1;
            part->lexeme = str_dup(lex + 1);
        } else {
            part->lexeme = str_dup(lex);
        }
        i++;
    }
    if (!tpl->unquotes) {
        parser_emit_syntax(parser, tpl);
        return;
    }
    parser->pending_quotes = (QuoteTemplate **)xrealloc(parser->pending_quotes,
                                                        (parser->pending_quote_len + 1) * sizeof(QuoteTemplate *));
    size_t index = parser->pending_quote_len++;
    parser->pending_quotes[index] = tpl;
    Token marker = {0};
    marker.lexeme = str_printf("%s%zu", L2_QUOTE_MARKER, index);
    VEC_PUSH(&injected, marker);
    parser_inject_tokens(parser, &injected);
}

static void parser_finish_macro_body(Parser *parser) {
    Word *word = dictionary_lookup(parser->dictionary, parser->macro_body.name);
    if (!word) {
        word = (Word *)xmalloc(sizeof(Word));
        memset(word, 0, sizeof(Word));
        word->name = str_dup(parser->macro_body.name);
        dictionary_register(parser->dictionary, word);
    }
    word->macro_body = parser->macro_body.def;
    word->macro_params = parser->macro_body.params;
    word->macro_param_count = parser->macro_body.param_count;
    word->macro_expansion = NULL;
    word->definition = NULL;
    word->asm_def = NULL;
    word->immediate = false;
    parser->current_def = NULL;
    parser->macro_body.active = false;
}

/* One macro argument: a single token, or a whole backtick quotation. */
static TokenVec parser_macro_argument(Parser *parser, const char *macro_name, bool *is_quotation) {
    TokenVec arg;
    VEC_INIT(&arg);
    *is_quotation = false;
    Token tok = parser_next_token(parser);
    if (!tok.lexeme) {
        fprintf(stderr, "[error] not enough macro parameters for '%s'\n", macro_name);
        exit(1);
    }
    if (strcmp(tok.lexeme, "`") != 0) {
        VEC_PUSH(&arg, tok);
        return arg;
    }
    *is_quotation = true;
    for (;;) {
        Token inner = parser_next_token(parser);
        if (!inner.lexeme) {
            fprintf(stderr, "[error] unterminated quotation argument for macro '%s'\n", macro_name);
            exit(1);
        }
        if (strcmp(inner.lexeme, "`") == 0) break;
        VEC_PUSH(&arg, inner);
    }
    return arg;
}

/* Expand `word` at the parser's input, appending its output to `out`. */
static void parser_macro_expand_into(Parser *parser, Word *word, TokenVec *out) {
    int param_count = word->macro_param_count;
    TokenVec *args = param_count > 0 ? (TokenVec *)xmalloc((size_t)param_count * sizeof(TokenVec)) : NULL;
    bool *quoted = param_count > 0 ? (bool *)xmalloc((size_t)param_count * sizeof(bool)) : NULL;
    for (int i = 0; i < param_count; i++) {
        args[i] = parser_macro_argument(parser, word->name, &quoted[i]);
    }
    if (word->macro_body) {
        CompileTimeVM *vm = parser->ct_vm;
        CtValue *values = param_count > 0 ? (CtValue *)xmalloc((size_t)param_count * sizeof(CtValue)) : NULL;
        for (int i = 0; i < param_count; i++) {
            if (quoted[i]) {
                CtList *list = ct_list_new();
                for (size_t k = 0; k < args[i].len; k++) ct_list_push_token(list, args[i].data[k]);
                values[i] = ct_make_syntax(list);
            } else {
                values[i] = ct_make_token(args[i].data[0]);
            }
        }
        /* The body runs on its own data stack with its parameters bound. */
        CtValueVec saved_stack = vm->stack;
        char **saved_names = vm->binding_names;
        CtValue *saved_values = vm->binding_values;
        size_t saved_len = vm->binding_len;
        TokenVec *saved_out = vm->emit_out;
        VEC_INIT(&vm->stack);
        vm->binding_names = word->macro_params;
        vm->binding_values = values;
        vm->binding_len = (size_t)param_count;
        vm->emit_out = out;
        VEC_PUSH(&vm->call_stack, word->name);
        ct_execute_nodes(vm, &word->macro_body->body);
        vm->call_stack.len--;
        VEC_FREE(&vm->stack);
        vm->stack = saved_stack;
        vm->binding_names = saved_names;
        vm->binding_values = saved_values;
        vm->binding_len = saved_len;
        vm->emit_out = saved_out;
        free(values);
    } else {
        for (size_t i = 0; word->macro_expansion && word->macro_expansion[i]; i++) {
            const char *item = word->macro_expansion[i];
            if (item && item[0] == '$' && isdigit((unsigned char)item[1])) {
                int idx = atoi(item + 1) - 1;
                if (idx >= 0 && idx < param_count) {
                    for (size_t k = 0; k < args[idx].len; k++) {
                        VEC_PUSH(out, token_copy_with(&args[idx].data[k], args[idx].data[k].lexeme));
                    }
                    continue;
                }
            }
            Token tok = {0};
            tok.lexeme = str_dup(item);
            VEC_PUSH(out, tok);
        }
    }
    free(args);
    free(quoted);
}

static void parser_expand_macro(Parser *parser, Word *word) {
    TokenVec injected;
    VEC_INIT(&injected);
    parser_macro_expand_into(parser, word, &injected);
    parser_inject_tokens(parser, &injected);
}

static void parser_handle_struct(Parser *parser) {
    Token name_tok = parser_next_token(parser);
    if (!name_tok.lexeme) {
        fprintf(stderr, "[error] struct missing name\n");
        exit(1);
    }
    typedef struct {
        char *name;
        int64_t size;
        int64_t offset;
    } Field;
    Field *fields = NULL;
    size_t field_len = 0;
    size_t field_cap = 0;
    int64_t offset = 0;
    while (!parser_eof(parser)) {
        Token tok = parser_next_token(parser);
        if (!tok.lexeme) {
            break;
        }
        if (strcmp(tok.lexeme, "end") == 0) {
            break;
        }
        if (strcmp(tok.lexeme, "field") != 0) {
            fprintf(stderr, "[error] unexpected token '%s' in struct\n", tok.lexeme);
            exit(1);
        }
        Token field_name = parser_next_token(parser);
        Token field_size = parser_next_token(parser);
        if (!field_name.lexeme || !field_size.lexeme) {
            fprintf(stderr, "[error] malformed struct field\n");
            exit(1);
        }
        int64_t size = 0;
        if (!try_parse_int(field_size.lexeme, &size)) {
            fprintf(stderr, "[error] invalid struct field size '%s'\n", field_size.lexeme);
            exit(1);
        }
        if (field_len + 1 > field_cap) {
            field_cap = field_cap ? field_cap * 2 : 8;
            fields = xrealloc(fields, field_cap * sizeof(Field));
        }
        fields[field_len++] = (Field){str_dup(field_name.lexeme), size, offset};
        offset += size;
    }
    TokenVec injected;
    VEC_INIT(&injected);
    Token tok = {0};
    tok.lexeme = str_dup("word");
    VEC_PUSH(&injected, tok);
    tok.lexeme = str_printf("%s.size", name_tok.lexeme);
    VEC_PUSH(&injected, tok);
    tok.lexeme = str_printf("%lld", (long long)offset);
    VEC_PUSH(&injected, tok);
    tok.lexeme = str_dup("end");
    VEC_PUSH(&injected, tok);
    for (size_t i = 0; i < field_len; i++) {
        Field f = fields[i];
        Token t = {0};
        t.lexeme = str_dup("word");
        VEC_PUSH(&injected, t);
        t.lexeme = str_printf("%s.%s.size", name_tok.lexeme, f.name);
        VEC_PUSH(&injected, t);
        t.lexeme = str_printf("%lld", (long long)f.size);
        VEC_PUSH(&injected, t);
        t.lexeme = str_dup("end");
        VEC_PUSH(&injected, t);

        t.lexeme = str_dup("word");
        VEC_PUSH(&injected, t);
        t.lexeme = str_printf("%s.%s.offset", name_tok.lexeme, f.name);
        VEC_PUSH(&injected, t);
        t.lexeme = str_printf("%lld", (long long)f.offset);
        VEC_PUSH(&injected, t);
        t.lexeme = str_dup("end");
        VEC_PUSH(&injected, t);

        t.lexeme = str_dup("word");
        VEC_PUSH(&injected, t);
        t.lexeme = str_printf("%s.%s@", name_tok.lexeme, f.name);
        VEC_PUSH(&injected, t);
        t.lexeme = str_printf("%s.%s.offset", name_tok.lexeme, f.name);
        VEC_PUSH(&injected, t);
        t.lexeme = str_dup("+");
        VEC_PUSH(&injected, t);
        t.lexeme = str_dup("@");
        VEC_PUSH(&injected, t);
        t.lexeme = str_dup("end");
        VEC_PUSH(&injected, t);

        t.lexeme = str_dup("word");
        VEC_PUSH(&injected, t);
        t.lexeme = str_printf("%s.%s!", name_tok.lexeme, f.name);
        VEC_PUSH(&injected, t);
        t.lexeme = str_dup("swap");
        VEC_PUSH(&injected, t);
        t.lexeme = str_printf("%s.%s.offset", name_tok.lexeme, f.name);
        VEC_PUSH(&injected, t);
        t.lexeme = str_dup("+");
        VEC_PUSH(&injected, t);
        t.lexeme = str_dup("swap");
        VEC_PUSH(&injected, t);
        t.lexeme = str_dup("!");
        VEC_PUSH(&injected, t);
        t.lexeme = str_dup("end");
        VEC_PUSH(&injected, t);
        free(f.name);
    }
    free(fields);
    parser_inject_tokens(parser, &injected);
}

static void parser_handle_with(Parser *parser) {
    StrVec names;
    VEC_INIT(&names);
    while (!parser_eof(parser)) {
        Token tok = parser_next_token(parser);
        if (!tok.lexeme) {
            fprintf(stderr, "[error] unterminated with block\n");
            exit(1);
        }
        if (strcmp(tok.lexeme, "in") == 0) {
            break;
        }
        VEC_PUSH(&names, str_dup(tok.lexeme));
    }
    for (size_t i = 0; i < names.len; i++) {
        const char *name = names.data[i];
        int id = parser->label_counter++;
        char *cell_label = str_printf("__with_%s_%d_cell", name, id);
        char *word_name = str_printf("__with_%s_%d", name, id);
        strmap_set(&parser->variable_labels, name, cell_label);
        strmap_set(&parser->variable_words, name, str_dup(word_name));

        AsmDefinition *def = (AsmDefinition *)xmalloc(sizeof(AsmDefinition));
        memset(def, 0, sizeof(AsmDefinition));
        def->name = str_dup(word_name);
        def->body = str_printf("    lea rax, [rel %s]\n    sub r12, 8\n    mov [r12], rax\n", cell_label);
        Word *word = dictionary_lookup(parser->dictionary, word_name);
        if (!word) {
            word = (Word *)xmalloc(sizeof(Word));
            memset(word, 0, sizeof(Word));
            word->name = str_dup(word_name);
            dictionary_register(parser->dictionary, word);
        }
        word->asm_def = def;
        Form form = {0};
        form.kind = FORM_ASM;
        form.ptr = def;
        VEC_PUSH(&parser->module.forms, form);
    }

    parser_push_control(parser, "with");
    parser->control_stack[parser->control_len - 1].with_names = names;
    TokenVec injected;
    VEC_INIT(&injected);
    for (size_t i = names.len; i-- > 0;) {
        Token t = {0};
        char *label = (char *)strmap_get(&parser->variable_words, names.data[i]);
        t.lexeme = str_dup(label);
        VEC_PUSH(&injected, t);
        t.lexeme = str_dup("swap");
        VEC_PUSH(&injected, t);
        t.lexeme = str_dup("!");
        VEC_PUSH(&injected, t);
    }
    parser_inject_tokens(parser, &injected);
    names.data = NULL;
    names.len = 0;
    names.cap = 0;
}

static void parser_handle_token(Parser *parser, Token token) {
    if (parser->token_hook) {
        Word *hook = dictionary_lookup(parser->dictionary, parser->token_hook);
        if (!hook) {
            fprintf(stderr, "[error] unknown token hook '%s'\n", parser->token_hook);
            exit(1);
        }
        ct_stack_push(&parser->ct_vm->stack, ct_make_token(token));
        ct_word_call(parser->ct_vm, hook);
        CtValue handled = ct_stack_pop(&parser->ct_vm->stack);
        if (ct_truthy(handled)) {
            return;
        }
    }

    if (strcmp(token.lexeme, "const") == 0) {
        /* `const NAME value` expands NAME to the single token `value`
         * (the Python compiler creates the macro lazily; here it is eager). */
        Token name = parser_next_token(parser);
        Token value = parser_next_token(parser);
        if (!name.lexeme || !value.lexeme) {
            fprintf(stderr, "[error] 'const' needs a name and a value: const NAME value\n");
            exit(1);
        }
        parser_start_macro(parser, name.lexeme, 0);
        VEC_PUSH(&parser->macro_recording.tokens, str_dup(value.lexeme));
        parser_finish_macro(parser);
        return;
    }

    if (strcmp(token.lexeme, "macro") == 0) {
        /* `macro name [(params)] body ;`: the body is compile-time code with
         * the parameters bound as $name; it ends at `;` at its own top level. */
        Token name = parser_next_token(parser);
        if (!name.lexeme) {
            fprintf(stderr, "[error] macro missing name\n");
            exit(1);
        }
        if (parser->current_def || parser->macro_body.active) {
            fprintf(stderr, "[error] macro definitions must be top-level forms\n");
            exit(1);
        }
        char **params = NULL;
        int param_count = 0;
        int param_cap = 0;
        Token peek = parser_peek_token(parser);
        if (peek.lexeme && strcmp(peek.lexeme, "(") == 0) {
            parser_next_token(parser);
            for (;;) {
                Token param = parser_next_token(parser);
                if (!param.lexeme) {
                    fprintf(stderr, "[error] unterminated parameter list for macro '%s'\n", name.lexeme);
                    exit(1);
                }
                if (strcmp(param.lexeme, ")") == 0) break;
                if (param_count + 1 > param_cap) {
                    param_cap = param_cap ? param_cap * 2 : 4;
                    params = (char **)xrealloc(params, (size_t)param_cap * sizeof(char *));
                }
                params[param_count++] = str_dup(param.lexeme);
            }
        }
        Definition *def = (Definition *)xmalloc(sizeof(Definition));
        memset(def, 0, sizeof(Definition));
        def->name = str_dup(name.lexeme);
        VEC_INIT(&def->body);
        def->compile_only = true;
        def->terminator = str_dup(";");
        parser->current_def = def;
        parser->macro_body.active = true;
        parser->macro_body.name = str_dup(name.lexeme);
        parser->macro_body.params = params;
        parser->macro_body.param_count = param_count;
        parser->macro_body.control_depth = parser->control_len;
        parser->macro_body.def = def;
        return;
    }

    if (strcmp(token.lexeme, "inline") == 0) {
        parser->pending_inline_def = true;
        return;
    }
    if (strcmp(token.lexeme, "preserve") == 0) {
        parser->pending_preserve_def = true;
        return;
    }

    if (strcmp(token.lexeme, "immediate") == 0) {
        if (!parser->last_defined) {
            fprintf(stderr, "[error] immediate used without a preceding definition\n");
            exit(1);
        }
        if (parser->last_defined->runtime_only) {
            fprintf(stderr, "[error] word '%s' is runtime-only and cannot be immediate\n", parser->last_defined->name);
            exit(1);
        }
        parser->last_defined->immediate = true;
        if (parser->last_defined->definition) {
            parser->last_defined->definition->immediate = true;
        }
        if (parser->last_defined->asm_def) {
            parser->last_defined->asm_def->immediate = true;
        }
        return;
    }

    if (strcmp(token.lexeme, "compile-only") == 0) {
        if (!parser->last_defined) {
            fprintf(stderr, "[error] compile-only used without a preceding definition\n");
            exit(1);
        }
        if (parser->last_defined->runtime_only) {
            fprintf(stderr, "[error] word '%s' is runtime-only and cannot be compile-only\n", parser->last_defined->name);
            exit(1);
        }
        parser->last_defined->compile_only = true;
        if (parser->last_defined->definition) {
            parser->last_defined->definition->compile_only = true;
        }
        if (parser->last_defined->asm_def) {
            parser->last_defined->asm_def->compile_only = true;
        }
        if (parser->last_defined->prev_definition) {
            parser->last_defined->ct_definition = parser->last_defined->definition;
            parser->last_defined->definition = parser->last_defined->prev_definition;
            parser->last_defined->prev_definition = NULL;
        }
        if (parser->last_defined->prev_asm_def) {
            parser->last_defined->ct_asm_def = parser->last_defined->asm_def;
            parser->last_defined->asm_def = parser->last_defined->prev_asm_def;
            parser->last_defined->prev_asm_def = NULL;
        }
        return;
    }

    if (strcmp(token.lexeme, "runtime") == 0 || strcmp(token.lexeme, "runtime-only") == 0) {
        if (!parser->last_defined) {
            fprintf(stderr, "[error] runtime used without a preceding definition\n");
            exit(1);
        }
        if (parser->last_defined->immediate) {
            fprintf(stderr, "[error] word '%s' is immediate and cannot be runtime-only\n", parser->last_defined->name);
            exit(1);
        }
        if (parser->last_defined->compile_only) {
            fprintf(stderr, "[error] word '%s' is compile-only and cannot be runtime-only\n", parser->last_defined->name);
            exit(1);
        }
        parser->last_defined->runtime_only = true;
        if (parser->last_defined->definition) {
            parser->last_defined->definition->runtime_only = true;
        }
        if (parser->last_defined->asm_def) {
            parser->last_defined->asm_def->runtime_only = true;
        }
        return;
    }

    if (strcmp(token.lexeme, "compile-time") == 0) {
        Token name = parser_next_token(parser);
        if (!name.lexeme) {
            fprintf(stderr, "[error] compile-time missing word name\n");
            exit(1);
        }
        Word *word = dictionary_lookup(parser->dictionary, name.lexeme);
        if (!word) {
            fprintf(stderr, "[error] unknown word '%s' for compile-time\n", name.lexeme);
            exit(1);
        }
        if (word->runtime_only) {
            fprintf(stderr, "[error] word '%s' is runtime-only\n", name.lexeme);
            exit(1);
        }
        ct_word_call(parser->ct_vm, word);
        if (parser->current_def) {
            Op op = {0};
            op.kind = OP_WORD;
            op.data.word = str_dup(name.lexeme);
            parser_emit_op(parser, op);
        }
        return;
    }

    if (strcmp(token.lexeme, "here") == 0) {
        SourceLocation *loc = location_for_token(parser, token);
        char *text = str_printf("%s:%d:%d", loc->path, loc->line, loc->column);
        parser_emit_literal(parser, LIT_STRING, 0, 0.0, text);
        free(text);
        return;
    }

    if (strcmp(token.lexeme, "label") == 0) {
        Token name = parser_next_token(parser);
        if (!name.lexeme) {
            fprintf(stderr, "[error] label missing name\n");
            exit(1);
        }
        Op op = {0};
        op.kind = OP_LABEL;
        op.data.label = str_dup(name.lexeme);
        parser_emit_op(parser, op);
        return;
    }

    if (strcmp(token.lexeme, "goto") == 0) {
        Token name = parser_next_token(parser);
        if (!name.lexeme) {
            fprintf(stderr, "[error] goto missing label\n");
            exit(1);
        }
        Op op = {0};
        op.kind = OP_JUMP;
        op.data.label = str_dup(name.lexeme);
        parser_emit_op(parser, op);
        return;
    }

    if (strcmp(token.lexeme, "ret") == 0) {
        Op op = {0};
        op.kind = OP_RET;
        parser_emit_op(parser, op);
        return;
    }

    if (strcmp(token.lexeme, "struct") == 0) {
        parser_handle_struct(parser);
        return;
    }

    if (strcmp(token.lexeme, "with") == 0) {
        parser_handle_with(parser);
        return;
    }

    char *str_lit = NULL;
    if (parse_string_literal(token.lexeme, &str_lit)) {
        parser_emit_literal(parser, LIT_STRING, 0, 0.0, str_lit);
        free(str_lit);
        return;
    }
    int64_t int_val = 0;
    if (try_parse_int(token.lexeme, &int_val)) {
        parser_emit_literal(parser, LIT_INT, int_val, 0.0, NULL);
        return;
    }
    double float_val = 0.0;
    if (try_parse_float(token.lexeme, &float_val)) {
        parser_emit_literal(parser, LIT_FLOAT, 0, float_val, NULL);
        return;
    }

    if (token.lexeme[0] == '&') {
        if (token.lexeme[1] == '\0') {
            fprintf(stderr, "[error] missing word name after '&' at %d:%d\n", token.line, token.column + 1);
            exit(1);
        }
        Op op = {0};
        op.kind = OP_WORD_PTR;
        op.data.word = str_dup(token.lexeme + 1);
        parser_emit_op(parser, op);
        return;
    }

    const char *var_label = (const char *)strmap_get(&parser->variable_words, token.lexeme);
    if (var_label) {
        Token peek = parser_peek_token(parser);
        Op op = {0};
        op.kind = OP_WORD;
        op.data.word = str_dup(var_label);
        parser_emit_op(parser, op);
        if (!peek.lexeme || strcmp(peek.lexeme, "!") != 0) {
            op.data.word = str_dup("@");
            parser_emit_op(parser, op);
        }
        return;
    }

    Word *word = dictionary_lookup(parser->dictionary, token.lexeme);
    if (word && (word->macro_expansion || word->macro_body)) {
        if (parser->macro_body.active) {
            /* Inside a macro body a macro name is a direct call. */
            Op op = {0};
            op.kind = OP_WORD;
            op.data.word = str_dup(word->name);
            parser_emit_op(parser, op);
            return;
        }
        parser_expand_macro(parser, word);
        return;
    }
    if (word && word->immediate) {
        /* What an immediate word emits is read right after it, in order. */
        CompileTimeVM *vm = parser->ct_vm;
        TokenVec emitted;
        VEC_INIT(&emitted);
        TokenVec *saved_out = vm->emit_out;
        vm->emit_out = &emitted;
        ct_word_call(parser->ct_vm, word);
        vm->emit_out = saved_out;
        parser_inject_tokens(parser, &emitted);
        if (parser->current_def && !word->compile_only) {
            Op op = {0};
            op.kind = OP_WORD;
            op.data.word = str_dup(word->name);
            parser_emit_op(parser, op);
        }
        return;
    }
    if (word && word->compile_only && parser->current_def && parser->definition_stack_len) {
        Word *current = parser->definition_stack[parser->definition_stack_len - 1];
        current->compile_only = true;
        if (current->definition) {
            current->definition->compile_only = true;
        }
    }

    if (!word) {
        word = (Word *)xmalloc(sizeof(Word));
        memset(word, 0, sizeof(Word));
        word->name = str_dup(token.lexeme);
        dictionary_register(parser->dictionary, word);
    }
    Op op = {0};
    op.kind = OP_WORD;
    op.data.word = str_dup(token.lexeme);
    parser_emit_op(parser, op);
}

int l2_cli(int argc, char **argv) {
    StrVec inputs;
    StrVec include_dirs;
    StrVec libs;
    VEC_INIT(&inputs);
    VEC_INIT(&include_dirs);
    VEC_INIT(&libs);
    const char *output = "a.out";
    const char *temp_dir = "build";
    bool emit_asm = false;
    bool debug = false;
    bool ct_run_main = false;
    bool no_artifact = false;

    for (int i = 1; i < argc; i++) {
        const char *arg = argv[i];
        if (strcmp(arg, "-o") == 0 && i + 1 < argc) {
            output = argv[++i];
            continue;
        }
        if (strcmp(arg, "--emit-asm") == 0) {
            emit_asm = true;
            continue;
        }
        if (strcmp(arg, "--dbg") == 0) {
            debug = true;
            continue;
        }
        if (strcmp(arg, "--ct-run-main") == 0) {
            ct_run_main = true;
            continue;
        }
        if (strcmp(arg, "--no-artifact") == 0) {
            no_artifact = true;
            continue;
        }
        if (strcmp(arg, "--script") == 0) {
            ct_run_main = true;
            no_artifact = true;
            continue;
        }
        if ((strcmp(arg, "-I") == 0 || strcmp(arg, "--include") == 0) && i + 1 < argc) {
            VEC_PUSH(&include_dirs, str_dup(argv[++i]));
            continue;
        }
        if (strncmp(arg, "-I", 2) == 0 && strlen(arg) > 2) {
            VEC_PUSH(&include_dirs, str_dup(arg + 2));
            continue;
        }
        if ((strcmp(arg, "-l") == 0) && i + 1 < argc) {
            const char *lib = argv[++i];
            if (strchr(lib, '/') || strstr(lib, ".so") || strstr(lib, ".a")) {
                VEC_PUSH(&libs, str_printf("-l:%s", lib));
            } else {
                VEC_PUSH(&libs, str_printf("-l%s", lib));
            }
            continue;
        }
        if (strncmp(arg, "-l", 2) == 0 && strlen(arg) > 2) {
            VEC_PUSH(&libs, str_dup(arg));
            continue;
        }
        if (strcmp(arg, "--temp-dir") == 0 && i + 1 < argc) {
            temp_dir = argv[++i];
            continue;
        }
        if (arg[0] == '-') {
            fprintf(stderr, "[error] unknown option: %s\n", arg);
            return 1;
        }
        VEC_PUSH(&inputs, str_dup(arg));
    }

    if (inputs.len == 0) {
        fprintf(stderr, "usage: %s <source.sl> [-o output] [--emit-asm] [--ct-run-main] [--no-artifact] [--script]\n", argv[0]);
        return 1;
    }

    VEC_PUSH(&include_dirs, str_dup("."));
    VEC_PUSH(&include_dirs, str_dup("./stdlib"));

    StrMap visited;
    strmap_init(&visited);
    StrVec sources;
    VEC_INIT(&sources);
    FileSpanVec file_spans;
    VEC_INIT(&file_spans);
    int line_counter = 1;
    for (size_t i = 0; i < inputs.len; i++) {
        char *expanded = expand_imports(inputs.data[i], &include_dirs, &visited, &file_spans, &line_counter);
        VEC_PUSH(&sources, expanded);
    }
    char *combined = str_join_parts(sources.data, sources.len, NULL, NULL);

    Dictionary dict;
    dictionary_init(&dict);
    Reader reader;
    reader_init(&reader);
    Parser parser;
    parser_init(&parser, &dict, &reader);
    parser.file_spans = file_spans;
    parser.primary_path = inputs.len ? str_dup(inputs.data[0]) : NULL;
    CompileTimeVM vm;
    ct_vm_init(&vm, &parser);
    parser.ct_vm = &vm;
    bootstrap_dictionary(&dict, &parser, &vm);
    register_builtin_syscall(&parser);

    parse_tokens(&parser, combined);

    if (ct_run_main) {
        Word *entry = dictionary_lookup(&dict, "main");
        if (!entry) {
            fprintf(stderr, "[error] --ct-run-main requested but 'main' is not defined\n");
            return 1;
        }
        ct_vm_reset(&vm);
        ct_word_call(&vm, entry);
    }

    if (no_artifact) {
        printf("[info] skipped artifact generation (--no-artifact)\n");
        return 0;
    }

    if (parser.uses_libc && !strvec_contains(&libs, "-lc")) {
        VEC_PUSH(&libs, str_dup("-lc"));
    }
    if (parser.uses_libm && !strvec_contains(&libs, "-lm")) {
        VEC_PUSH(&libs, str_dup("-lm"));
    }

    Emission emission = emit_module(&parser, &dict, debug);
    char *asm_text = emission_snapshot(&emission);

    char *asm_path = NULL;
    char *obj_path = NULL;
    if (emit_asm) {
        asm_path = str_dup(output);
    } else {
        mkdir(temp_dir, 0755);
        const char *base = strrchr(output, '/');
        base = base ? base + 1 : output;
        asm_path = str_printf("%s/%s.asm", temp_dir, base);
        obj_path = str_printf("%s/%s.o", temp_dir, base);
    }

    write_file(asm_path, asm_text);
    if (emit_asm) {
        return 0;
    }
    run_nasm(asm_path, obj_path, debug);
    run_linker(obj_path, output, debug, &libs, false, parser.uses_libc);
    return 0;
}

static int eval_program_in_process(const char *source, long source_len) {
    int result = -1;
    char *owned_source = NULL;
    char *root_dir = NULL;
    char *stdlib_dir = NULL;
    char *combined = NULL;
    StrMap visited;
    FileSpanVec spans;
    L2EvalContext ctx = {0};

    if (!source) {
        fprintf(stderr, "[error] l2_eval received null source\n");
        return -1;
    }
    if (source_len < 0) {
        fprintf(stderr, "[error] l2_eval received negative source length\n");
        return -1;
    }

    size_t src_len = (size_t)source_len;
    owned_source = (char *)xmalloc(src_len + 1);
    memcpy(owned_source, source, src_len);
    owned_source[src_len] = '\0';

    root_dir = l2_default_root_dir();
    stdlib_dir = path_join(root_dir, "stdlib");

    l2_eval_ctx_init(&ctx);
    VEC_PUSH(&ctx.include_dirs, str_dup(root_dir));
    VEC_PUSH(&ctx.include_dirs, str_dup(stdlib_dir));

    strmap_init(&visited);
    VEC_INIT(&spans);
    int line_counter = 1;
    combined = expand_imports_from_source(owned_source, root_dir, &ctx.include_dirs, &visited, &spans, &line_counter);

    l2_eval_ctx_prepare_parser(&ctx);
    ctx.parser.file_spans = spans;
    ctx.parser.primary_path = str_dup("<eval>");
    char *program = str_printf("%s\nmain\n", combined);
    result = l2_eval_inline_with_ctx(&ctx, program, (long)strlen(program));
    free(program);

cleanup:
    if (owned_source) {
        free(owned_source);
    }
    free(combined);
    free(root_dir);
    free(stdlib_dir);
    if (ctx.parser.primary_path) {
        free(ctx.parser.primary_path);
    }
    return result;
}

/* ---------------------------------------------------------------------------
 * eval_program warm cache: parse stdlib once per process.
 *
 * Every eval_program call forks a child that, on the cold path, expands and
 * parses the whole program including stdlib.  When a program starts with
 * imports that all resolve inside the stdlib directory, the parent keeps a
 * context with exactly that stdlib prefix already parsed; each forked child
 * inherits it and only parses the user body.  The child's text is padded with
 * newlines so diagnostics keep the line numbers of the cold-path text.
 *
 * The cache is bypassed (cold path, unchanged behavior) when the host program
 * has embedded source, when any top-level import resolves outside stdlib or
 * appears after user code, or when the warm parse fails in a probe child.
 * ------------------------------------------------------------------------- */

typedef struct {
    char *key;           /* newline-joined resolved top-level import paths */
    L2EvalContext *ctx;  /* parsed stdlib prefix, or NULL if the warm parse failed */
    int prefix_lines;    /* lines of the expanded prefix in the cold-path text */
} L2EvalWarmEntry;

#define L2_EVAL_WARM_MAX 4
static L2EvalWarmEntry g_eval_warm[L2_EVAL_WARM_MAX];
static size_t g_eval_warm_len = 0;

/* Same import-directive test as the import expanders.  On success returns
 * true and sets the path range and the remainder after the path (leading
 * whitespace skipped). */
static bool l2_eval_line_import(const char *line, const char **path_start, const char **path_end, const char **remainder) {
    const char *trim = line;
    while (*trim && isspace((unsigned char)*trim)) {
        trim++;
    }
    if (!(str_starts_with(trim, "import") && (trim[6] == ' ' || trim[6] == '\t'))) {
        return false;
    }
    trim += 6;
    while (*trim && isspace((unsigned char)*trim)) {
        trim++;
    }
    const char *end = trim;
    while (*end && !isspace((unsigned char)*end) && *end != '#') {
        end++;
    }
    if (end == trim) {
        return false;
    }
    const char *rest = end;
    while (*rest && isspace((unsigned char)*rest)) {
        rest++;
    }
    *path_start = trim;
    *path_end = end;
    *remainder = rest;
    return true;
}

static int l2_eval_count_newlines(const char *text) {
    int count = 0;
    for (const char *p = text; *p; p++) {
        if (*p == '\n') {
            count++;
        }
    }
    return count;
}

/* Split `source` into a stdlib-only import prefix and the user body.
 * Returns false when the program does not qualify for the warm cache. */
static bool l2_eval_split_stdlib_prefix(const char *source, char **out_prefix, char **out_body, char **out_key) {
    char *root_dir = l2_default_root_dir();
    char *stdlib_dir = path_join(root_dir, "stdlib");
    char stdlib_real[PATH_MAX];
    if (!realpath(stdlib_dir, stdlib_real)) {
        free(stdlib_dir);
        free(root_dir);
        return false;
    }
    char *stdlib_prefix = str_printf("%s/", stdlib_real);
    StrVec include_dirs;
    VEC_INIT(&include_dirs);
    VEC_PUSH(&include_dirs, str_dup(root_dir));
    VEC_PUSH(&include_dirs, str_dup(stdlib_dir));

    StrVec prefix_lines;
    StrVec keys;
    VEC_INIT(&prefix_lines);
    VEC_INIT(&keys);
    char *body = NULL;
    bool ok = true;

    const char *cursor = source;
    while (*cursor) {
        const char *line_end = strchr(cursor, '\n');
        size_t len = line_end ? (size_t)(line_end - cursor) : strlen(cursor);
        char *line = (char *)xmalloc(len + 1);
        memcpy(line, cursor, len);
        line[len] = '\0';
        const char *next = line_end ? line_end + 1 : cursor + len;

        const char *trim = line;
        while (*trim && isspace((unsigned char)*trim)) {
            trim++;
        }
        const char *path_start = NULL;
        const char *path_end = NULL;
        const char *remainder = NULL;
        if (*trim == '\0' || *trim == '#') {
            VEC_PUSH(&prefix_lines, line);
        } else if (l2_eval_line_import(line, &path_start, &path_end, &remainder)) {
            size_t path_len = (size_t)(path_end - path_start);
            char *import_path = (char *)xmalloc(path_len + 1);
            memcpy(import_path, path_start, path_len);
            import_path[path_len] = '\0';
            char *resolved = resolve_import(root_dir, import_path, &include_dirs);
            free(import_path);
            /* Compare canonical paths so `../` cannot escape the stdlib
             * directory while still matching its textual prefix. */
            char resolved_real[PATH_MAX];
            if (!resolved || !realpath(resolved, resolved_real) ||
                !str_starts_with(resolved_real, stdlib_prefix)) {
                free(resolved);
                free(line);
                ok = false;
                break;
            }
            VEC_PUSH(&keys, resolved);
            size_t head_len = (size_t)(path_end - line);
            char *head = (char *)xmalloc(head_len + 1);
            memcpy(head, line, head_len);
            head[head_len] = '\0';
            VEC_PUSH(&prefix_lines, head);
            if (*remainder && *remainder != '#') {
                body = str_printf("%s\n%s", remainder, next);
                free(line);
                break;
            }
            free(line);
        } else {
            body = str_printf("%s\n%s", line, next);
            free(line);
            break;
        }
        if (!line_end) {
            break;
        }
        cursor = line_end + 1;
    }

    if (ok && keys.len == 0) {
        ok = false;
    }
    if (ok && body) {
        /* A later import would change the visited set mid-body; keep those
         * programs on the cold path. */
        const char *scan = body;
        while (*scan) {
            const char *line_end = strchr(scan, '\n');
            size_t len = line_end ? (size_t)(line_end - scan) : strlen(scan);
            char *line = (char *)xmalloc(len + 1);
            memcpy(line, scan, len);
            line[len] = '\0';
            const char *a = NULL, *b = NULL, *c = NULL;
            bool is_import = l2_eval_line_import(line, &a, &b, &c);
            free(line);
            if (is_import) {
                ok = false;
                break;
            }
            if (!line_end) {
                break;
            }
            scan = line_end + 1;
        }
    }

    if (ok) {
        *out_prefix = str_join_parts(prefix_lines.data, prefix_lines.len, NULL, "\n");
        *out_body = body ? body : str_dup("");
        *out_key = str_join_parts(keys.data, keys.len, NULL, "\n");
        body = NULL;
    }

    free(body);
    for (size_t i = 0; i < prefix_lines.len; i++) {
        free(prefix_lines.data[i]);
    }
    VEC_FREE(&prefix_lines);
    for (size_t i = 0; i < keys.len; i++) {
        free(keys.data[i]);
    }
    VEC_FREE(&keys);
    for (size_t i = 0; i < include_dirs.len; i++) {
        free(include_dirs.data[i]);
    }
    VEC_FREE(&include_dirs);
    free(stdlib_prefix);
    free(stdlib_dir);
    free(root_dir);
    return ok;
}

/* Expand text that contains no imports the way the cold path's second
 * expansion pass does, returning the text and its file spans. */
static char *l2_eval_expand_plain(L2EvalContext *ctx, const char *text, FileSpanVec *spans) {
    StrMap visited;
    strmap_init(&visited);
    VEC_INIT(spans);
    int line_counter = 1;
    char *out = expand_imports_from_source(text, ctx->root_dir, &ctx->include_dirs, &visited, spans, &line_counter);
    strmap_free(&visited);
    return out;
}

/* Build a context with the stdlib prefix parsed, mirroring the cold path's
 * setup.  May exit() on a parse error, so the parent only calls it after a
 * probe child has succeeded. */
static L2EvalContext *l2_eval_warm_build(const char *prefix_source, int *out_prefix_lines) {
    L2EvalContext *ctx = (L2EvalContext *)xmalloc(sizeof(L2EvalContext));
    memset(ctx, 0, sizeof(L2EvalContext));
    l2_eval_ctx_init(ctx);

    StrMap visited;
    strmap_init(&visited);
    FileSpanVec first_spans;
    VEC_INIT(&first_spans);
    int line_counter = 1;
    char *expanded = expand_imports_from_source(prefix_source, ctx->root_dir, &ctx->include_dirs, &visited, &first_spans, &line_counter);
    strmap_free(&visited);

    FileSpanVec spans;
    char *text = l2_eval_expand_plain(ctx, expanded, &spans);
    l2_eval_ctx_prepare_parser(ctx);
    l2_eval_seed_embedded_program(ctx);
    ctx->parser.file_spans = spans;
    ctx->parser.primary_path = str_dup("<eval>");
    parse_tokens(&ctx->parser, text);

    *out_prefix_lines = l2_eval_count_newlines(text);
    for (size_t i = 0; i < first_spans.len; i++) {
        free(first_spans.data[i].path);
    }
    VEC_FREE(&first_spans);
    free(expanded);
    free(text);
    return ctx;
}

/* Find or create the warm entry for `key`.  Returns NULL when the cache is
 * full; an entry whose ctx is NULL means "use the cold path". */
static L2EvalWarmEntry *l2_eval_warm_get(const char *key, const char *prefix_source) {
    for (size_t i = 0; i < g_eval_warm_len; i++) {
        if (str_equals(g_eval_warm[i].key, key)) {
            return &g_eval_warm[i];
        }
    }
    if (g_eval_warm_len >= L2_EVAL_WARM_MAX) {
        return NULL;
    }
    L2EvalWarmEntry *entry = &g_eval_warm[g_eval_warm_len++];
    entry->key = str_dup(key);
    entry->ctx = NULL;
    entry->prefix_lines = 0;

    /* Probe: a parse error exits the process, so prove the warm parse
     * succeeds in a throwaway child before running it in this process. */
    fflush(stdout);
    fflush(stderr);
    pid_t probe = fork();
    if (probe < 0) {
        return entry;
    }
    if (probe == 0) {
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
            dup2(devnull, STDERR_FILENO);
            close(devnull);
        }
        int lines = 0;
        (void)l2_eval_warm_build(prefix_source, &lines);
        _exit(0);
    }
    int status = 0;
    pid_t waited;
    do {
        waited = waitpid(probe, &status, 0);
    } while (waited < 0 && errno == EINTR);
    if (waited == probe && WIFEXITED(status) && WEXITSTATUS(status) == 0) {
        entry->ctx = l2_eval_warm_build(prefix_source, &entry->prefix_lines);
    }
    return entry;
}

/* Child side: continue the inherited warm parse with only the user body,
 * then run the top-level forms exactly as l2_eval_inline_with_ctx does. */
static int l2_eval_run_warm(L2EvalWarmEntry *entry, const char *body) {
    L2EvalContext *ctx = entry->ctx;
    FileSpanVec body_spans;
    char *body_text = l2_eval_expand_plain(ctx, body, &body_spans);
    for (size_t i = 0; i < body_spans.len; i++) {
        free(body_spans.data[i].path);
    }
    VEC_FREE(&body_spans);

    size_t pad = (size_t)entry->prefix_lines;
    size_t body_len = strlen(body_text);
    const char *tail = "\nmain\n";
    size_t tail_len = strlen(tail);
    char *program = (char *)xmalloc(pad + body_len + tail_len + 1);
    memset(program, '\n', pad);
    memcpy(program + pad, body_text, body_len);
    memcpy(program + pad + body_len, tail, tail_len + 1);
    free(body_text);

    FileSpanVec spans;
    char *text = l2_eval_expand_plain(ctx, program, &spans);
    free(program);
    ctx->parser.file_spans = spans;
    parse_tokens_continue(&ctx->parser, text);
    free(text);

    for (size_t i = 0; i < ctx->parser.module.forms.len; i++) {
        Form form = ctx->parser.module.forms.data[i];
        if (form.kind != FORM_DEF) {
            continue;
        }
        Definition *def = (Definition *)form.ptr;
        if (def && str_equals(def->name, "<top>")) {
            ct_execute_nodes(&ctx->vm, &def->body);
        }
    }
    int result = 0;
    if (ctx->vm.stack.len > 0) {
        result = (int)ct_value_to_int(ct_stack_peek(&ctx->vm.stack));
    }
    return result;
}

int eval_program(const char *source, long source_len) {
    if (!source || source_len < 0) {
        return eval_program_in_process(source, source_len);
    }

    L2EvalWarmEntry *warm = NULL;
    char *warm_body = NULL;
    size_t embed_len = 0;
    if (getenv("L2_EVAL_NO_WARM") == NULL &&
        (!l2_eval_get_embedded_source(&embed_len) || embed_len == 0)) {
        char *owned = (char *)xmalloc((size_t)source_len + 1);
        memcpy(owned, source, (size_t)source_len);
        owned[source_len] = '\0';
        char *prefix = NULL;
        char *key = NULL;
        if (l2_eval_split_stdlib_prefix(owned, &prefix, &warm_body, &key)) {
            warm = l2_eval_warm_get(key, prefix);
            if (!warm || !warm->ctx) {
                warm = NULL;
            }
            free(prefix);
            free(key);
        }
        free(owned);
    }

    int result_pipe[2];
    if (pipe(result_pipe) != 0) {
        fprintf(stderr, "[error] eval_program pipe failed: %s\n", strerror(errno));
        free(warm_body);
        return -1;
    }
    fflush(stdout);
    fflush(stderr);

    pid_t child_pid = fork();
    if (child_pid < 0) {
        int saved_errno = errno;
        close(result_pipe[0]);
        close(result_pipe[1]);
        fprintf(stderr, "[error] eval_program fork failed: %s\n", strerror(saved_errno));
        free(warm_body);
        return -1;
    }
    if (child_pid == 0) {
        close(result_pipe[0]);
        int result = warm ? l2_eval_run_warm(warm, warm_body)
                          : eval_program_in_process(source, source_len);
        fflush(stdout);
        fflush(stderr);
        ssize_t written;
        do {
            written = write(result_pipe[1], &result, sizeof(result));
        } while (written < 0 && errno == EINTR);
        close(result_pipe[1]);
        _exit(written == (ssize_t)sizeof(result) ? 0 : 1);
    }

    free(warm_body);
    close(result_pipe[1]);
    int result = -1;
    size_t received = 0;
    while (received < sizeof(result)) {
        ssize_t count = read(result_pipe[0], (char *)&result + received, sizeof(result) - received);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            break;
        }
        received += (size_t)count;
    }
    close(result_pipe[0]);

    int wait_status = 0;
    pid_t waited;
    do {
        waited = waitpid(child_pid, &wait_status, 0);
    } while (waited < 0 && errno == EINTR);
    if (waited != child_pid || !WIFEXITED(wait_status) || WEXITSTATUS(wait_status) != 0 || received != sizeof(result)) {
        return -1;
    }
    return result;
}

static int l2_eval_inline_run(L2EvalContext *ctx, const char *source, long source_len) {
    char *owned_source = NULL;
    char *work_dir = NULL;
    char *source_path = NULL;
    char *root_dir = NULL;
    char *stdlib_dir = NULL;
    char *combined = NULL;
    int result = -1;

    if (!source) {
        fprintf(stderr, "[error] l2_eval_inline received null source\n");
        return -1;
    }
    if (source_len < 0) {
        fprintf(stderr, "[error] l2_eval_inline received negative source length\n");
        return -1;
    }

    size_t src_len = (size_t)source_len;
    owned_source = (char *)xmalloc(src_len + 1);
    memcpy(owned_source, source, src_len);
    owned_source[src_len] = '\0';

    root_dir = l2_default_root_dir();
    stdlib_dir = path_join(root_dir, "stdlib");
    (void)stdlib_dir;

    StrMap visited;
    strmap_init(&visited);
    FileSpanVec spans;
    VEC_INIT(&spans);
    int line_counter = 1;
    combined = expand_imports_from_source(owned_source, root_dir, &ctx->include_dirs, &visited, &spans, &line_counter);

    l2_eval_ctx_prepare_parser(ctx);
    l2_eval_seed_embedded_program(ctx);
    ctx->parser.file_spans = spans;
    ctx->parser.primary_path = str_dup("<eval>");
    l2_eval_preload_eval_dependencies(ctx, combined);
    parse_tokens(&ctx->parser, combined);

    for (size_t i = 0; i < ctx->parser.module.forms.len; i++) {
        Form form = ctx->parser.module.forms.data[i];
        if (form.kind != FORM_DEF) {
            continue;
        }
        Definition *def = (Definition *)form.ptr;
        if (def && str_equals(def->name, "<top>")) {
            ct_execute_nodes(&ctx->vm, &def->body);
        }
    }
    result = 0;
    if (ctx->vm.stack.len > 0) {
        result = (int)ct_value_to_int(ct_stack_peek(&ctx->vm.stack));
    }

cleanup:
    free(owned_source);
    free(combined);
    free(root_dir);
    free(stdlib_dir);
    return result;
}

/* Whether the last eval run stopped on an error; see l2_eval_last_failed. */
static bool g_l2_eval_failed = false;
/* Run eval'd source under an exit guard: an error in it (an unknown word,
 * a stack underflow, ...) is reported and ends this eval only, leaving the
 * context's stacks empty and returning -1, instead of ending the process.
 * Definitions the source made before the error stay in the context. */
static int l2_eval_inline_with_ctx(L2EvalContext *ctx, const char *source, long source_len) {
    jmp_buf guard;
    jmp_buf *outer_guard = g_l2_exit_guard;
    volatile int result = -1;
    g_l2_eval_depth++;
    if (setjmp(guard) == 0) {
        g_l2_exit_guard = &guard;
        result = l2_eval_inline_run(ctx, source, source_len);
        g_l2_eval_failed = false;
    } else {
        ctx->vm.stack.len = 0;
        ctx->vm.rstack.len = 0;
        ctx->vm.call_stack.len = 0;
        g_l2_eval_failed = true;
        result = -1;
    }
    g_l2_exit_guard = outer_guard;
    g_l2_eval_depth--;
    return result;
}

/* 1 when the last eval, eval_env or eval_scalar call stopped on an error
 * (its message is on stderr), else 0.  A failed eval_env leaves the data
 * stack as it was before the call, minus eval_env's own arguments. */
int l2_eval_last_failed(void) {
    return g_l2_eval_failed ? 1 : 0;
}

static bool ct_value_to_runtime_cells(const CtValue *value, uint64_t cells[2], size_t *cell_count) {
    if (!value || !cells || !cell_count) {
        return false;
    }
    switch (value->kind) {
        case CT_INT:
            cells[0] = (uint64_t)value->as.i64;
            *cell_count = 1;
            return true;
        case CT_STR: {
            const char *str = value->as.str ? value->as.str : "";
            cells[0] = (uint64_t)(uintptr_t)str;
            cells[1] = (uint64_t)strlen(str);
            *cell_count = 2;
            return true;
        }
        case CT_TOKEN: {
            const char *str = value->as.token.lexeme ? value->as.token.lexeme : "";
            cells[0] = (uint64_t)(uintptr_t)str;
            cells[1] = (uint64_t)strlen(str);
            *cell_count = 2;
            return true;
        }
        case CT_NIL:
            cells[0] = 0;
            *cell_count = 1;
            return true;
        default:
            return false;
    }
}

uintptr_t l2_eval_env_compute(const char *source, long source_len, long stack_top_addr) {
    /* If eval_env's normal source argument is unavailable, retry against the
     * embedded host program instead of returning an invalid stack pointer. */
    if (!source) {
        size_t embed_len = 0;
        const unsigned char *embed_source = l2_eval_get_embedded_source(&embed_len);
        if (embed_source && embed_len > 0) {
            source = (const char *)embed_source;
            source_len = (long)embed_len;
        } else {
            fprintf(stderr, "[error] eval_env received null source\n");
            return (uintptr_t)stack_top_addr + (uintptr_t)(2 * 8);
        }
    }
    if (source_len <= 0) {
        size_t embed_len = 0;
        const unsigned char *embed_source = l2_eval_get_embedded_source(&embed_len);
        if (source_len == 0 && embed_source && embed_len > 0) {
            source = (const char *)embed_source;
            source_len = (long)embed_len;
        } else if (source_len < 0) {
            fprintf(stderr, "[error] eval_env received negative source length\n");
            return (uintptr_t)stack_top_addr + (uintptr_t)(2 * 8);
        }
    }

    l2_eval_ctx_init(&g_eval_env_ctx);
    g_eval_env_ctx.vm.stack.len = 0;
    g_eval_env_ctx.vm.rstack.len = 0;

    uintptr_t context_sp = (uintptr_t)stack_top_addr + (uintptr_t)(2 * 8);
    uintptr_t dstack_top_addr = l2_eval_stack_top_addr();
    if (context_sp > dstack_top_addr) {
        fprintf(stderr, "[error] eval_env stack pointer is above dstack_top\n");
        return context_sp;
    }
    uintptr_t depth_bytes = dstack_top_addr - context_sp;
    if ((depth_bytes & 7) != 0) {
        fprintf(stderr, "[error] eval_env stack pointer is misaligned\n");
        return context_sp;
    }
    size_t depth = (size_t)(depth_bytes / 8);
    for (size_t idx = depth; idx-- > 0;) {
        uint64_t cell = *(uint64_t *)(context_sp + (uintptr_t)(idx * 8));
        ct_stack_push(&g_eval_env_ctx.vm.stack, ct_make_int((int64_t)cell));
    }

    int rc = l2_eval_inline_with_ctx(&g_eval_env_ctx, source, source_len);
    // rc is the top stack value, not a status code; negative results are valid.
    (void)rc;
    if (g_l2_eval_failed) {
        return context_sp;
    }

    for (size_t i = 0; i < g_eval_env_ctx.vm.stack.len; i++) {
        uint64_t cells[2];
        size_t cell_count = 0;
        if (!ct_value_to_runtime_cells(&g_eval_env_ctx.vm.stack.data[i], cells, &cell_count)) {
            fprintf(stderr, "[error] eval_env cannot export compile-time value kind %d\n", g_eval_env_ctx.vm.stack.data[i].kind);
            return context_sp;
        }
    }

    uintptr_t new_sp = dstack_top_addr;
    for (size_t i = 0; i < g_eval_env_ctx.vm.stack.len; i++) {
        uint64_t cells[2];
        size_t cell_count = 0;
        if (!ct_value_to_runtime_cells(&g_eval_env_ctx.vm.stack.data[i], cells, &cell_count)) {
            fprintf(stderr, "[error] eval_env cannot export compile-time value kind %d\n", g_eval_env_ctx.vm.stack.data[i].kind);
            return context_sp;
        }
        for (size_t c = 0; c < cell_count; c++) {
            new_sp -= 8;
            *(uint64_t *)new_sp = cells[c];
        }
    }
    return new_sp;
}

#if defined(__x86_64__) && (defined(__GNUC__) || defined(__clang__))
__attribute__((naked))
void eval_env(const char *source, long source_len, long stack_top_addr) {
    __asm__ volatile(
        "sub $8, %rsp\n"
        "call l2_eval_env_compute\n"
        "add $8, %rsp\n"
        "mov %rax, %r12\n"
        "ret\n"
    );
}
#else
void eval_env(const char *source, long source_len, long stack_top_addr) {
    (void)source;
    (void)source_len;
    (void)stack_top_addr;
    fprintf(stderr, "[error] eval_env is only supported on x86_64 with GCC/Clang\n");
}
#endif

uintptr_t l2_eval_compute(const char *source, long source_len, long stack_top_addr) {
    if (!source) {
        fprintf(stderr, "[error] eval received null source\n");
        return (uintptr_t)stack_top_addr;
    }
    if (source_len < 0) {
        fprintf(stderr, "[error] eval received negative source length\n");
        return (uintptr_t)stack_top_addr;
    }

    l2_eval_ctx_init(&g_eval_ctx);
    g_eval_ctx.vm.stack.len = 0;
    g_eval_ctx.vm.rstack.len = 0;

    int rc = l2_eval_inline_with_ctx(&g_eval_ctx, source, source_len);
    // A negative result can equal the error sentinel; an empty CT stack
    // distinguishes setup failure from a valid negative result.
    if (g_l2_eval_failed || (rc < 0 && g_eval_ctx.vm.stack.len == 0)) {
        return (uintptr_t)stack_top_addr;
    }

    uintptr_t new_sp = (uintptr_t)stack_top_addr;
    if (new_sp == 0) {
        new_sp = l2_eval_stack_top_addr();
    }
    for (size_t i = 0; i < g_eval_ctx.vm.stack.len; i++) {
        uint64_t cells[2];
        size_t cell_count = 0;
        if (!ct_value_to_runtime_cells(&g_eval_ctx.vm.stack.data[i], cells, &cell_count)) {
            fprintf(stderr, "[error] eval cannot export compile-time value kind %d\n", g_eval_ctx.vm.stack.data[i].kind);
            return (uintptr_t)stack_top_addr;
        }
        for (size_t c = 0; c < cell_count; c++) {
            new_sp -= 8;
            *(uint64_t *)new_sp = cells[c];
        }
    }
    return new_sp;
}

#if defined(__x86_64__) && (defined(__GNUC__) || defined(__clang__))
__attribute__((naked))
void eval(const char *source, long source_len) {
    __asm__ volatile(
        "mov %r12, %rdx\n"
        "sub $8, %rsp\n"
        "call l2_eval_compute\n"
        "add $8, %rsp\n"
        "mov %rax, %r12\n"
        "ret\n"
    );
}
#else
void eval(const char *source, long source_len) {
    (void)source; (void)source_len;
    fprintf(stderr, "[error] eval (stack-returning) is only supported on x86_64 with GCC/Clang\n");
}
#endif

int eval_scalar(const char *source, long source_len) {
    if (!source) {
        return -1;
    }
    uintptr_t sp = l2_eval_compute(source, source_len, 0);
    if (sp == 0) {
        return -1;
    }
    if (sp >= l2_eval_stack_top_addr()) {
        return 0;
    }
    int64_t result = *(int64_t *)sp;
    return (int)result;
}

l2_eval_status_t l2_eval_ex(const char *source, long source_len, int64_t *out_result) {
    if (!source || source_len < 0 || !out_result) {
        return L2_EVAL_STATUS_ERROR;
    }

    l2_eval_ctx_init(&g_eval_ctx);
    g_eval_ctx.vm.stack.len = 0;
    g_eval_ctx.vm.rstack.len = 0;

    int result = l2_eval_inline_with_ctx(&g_eval_ctx, source, source_len);
    if (g_l2_eval_failed || (result < 0 && g_eval_ctx.vm.stack.len == 0)) {
        return L2_EVAL_STATUS_ERROR;
    }
    if (g_eval_ctx.vm.stack.len == 0) {
        *out_result = 0;
        return L2_EVAL_STATUS_OK;
    }

    CtValue top = ct_stack_peek(&g_eval_ctx.vm.stack);
    if (top.kind != CT_INT) {
        return L2_EVAL_STATUS_ERROR;
    }
    *out_result = top.as.i64;
    return L2_EVAL_STATUS_OK;
}

int eval_cstr(const char *source) {
    if (!source) {
        return -1;
    }
    return eval_scalar(source, (long)strlen(source));
}

int eval_program_cstr(const char *source) {
    if (!source) {
        return -1;
    }
    return eval_program(source, (long)strlen(source));
}

// Legacy aliases for backward compatibility
int l2_eval(const char *source, long source_len) {
    return eval_scalar(source, source_len);
}

int l2_eval_cstr(const char *source) {
    return eval_cstr(source);
}

int l2_eval_program(const char *source, long source_len) {
    return eval_program(source, source_len);
}

int l2_eval_inline(const char *source, long source_len) {
    return eval_scalar(source, source_len);
}

int l2_eval_inline_cstr(const char *source) {
    return eval_cstr(source);
}

// l2_compile / l2_release implementation. The SysV trampoline and source share
// an executable mapping, with the source after the reserved trampoline slot.

#if defined(__x86_64__)

typedef struct l2_compiled_entry {
    void *page;
    size_t size;
    struct l2_compiled_entry *next;
} l2_compiled_entry_t;

static l2_compiled_entry_t *g_l2_compiled_head = NULL;

#define L2_COMPILE_TRAMPOLINE_SLOT 64

// Compile snippets to native code with Keystone when they use supported
// r12-based operations; all other snippets use the eval trampoline.

static L2EvalContext g_l2_ct_jit_ctx;
static bool g_l2_ct_jit_ctx_ready = false;
static long g_l2_ct_jit_seq = 0;

// Convert NASM size prefixes and strip comments for Keystone's Intel syntax.
// Returns a new string and frees the input.
static char *l2_ct_asm_intelize(char *asm_text) {
    if (!asm_text) return NULL;
    size_t in_len = strlen(asm_text);
    size_t cap = in_len * 2 + 64;
    char *out = (char *)xmalloc(cap);
    size_t o = 0;
    static const char *sizes[] = { "qword", "dword", "word", "byte", "tword", NULL };
    for (size_t i = 0; i < in_len;) {
        char c = asm_text[i];
        // Skip ';' comments up to end of line.
        if (c == ';') {
            while (i < in_len && asm_text[i] != '\n') i++;
            continue;
        }
        bool matched = false;
        for (int s = 0; sizes[s]; s++) {
            size_t slen = strlen(sizes[s]);
            if (i + slen + 1 > in_len) continue;
            if (memcmp(asm_text + i, sizes[s], slen) != 0) continue;
            if (i > 0) {
                char prev = asm_text[i - 1];
                if (prev != ' ' && prev != '\t' && prev != '\n') continue;
            }
            size_t j = i + slen;
            if (j >= in_len || asm_text[j] != ' ') continue;
            size_t k = j + 1;
            while (k < in_len && asm_text[k] == ' ') k++;
            if (k >= in_len || asm_text[k] != '[') continue;
            memcpy(out + o, sizes[s], slen); o += slen;
            memcpy(out + o, " ptr", 4); o += 4;
            i = j;
            matched = true;
            break;
        }
        if (!matched) {
            out[o++] = asm_text[i++];
        }
        if (o + 8 >= cap) {
            cap = cap * 2;
            out = (char *)realloc(out, cap);
        }
    }
    out[o] = '\0';
    free(asm_text);
    return out;
}

static void l2_ct_jit_ctx_init_once(void) {
    if (g_l2_ct_jit_ctx_ready) {
        return;
    }
    l2_eval_ctx_init(&g_l2_ct_jit_ctx);
    l2_eval_seed_embedded_program(&g_l2_ct_jit_ctx);

    // Prime the context with stdlib :asm words, loading stdlib.sl from disk
    // when this library was built without embedded source.
    char *primer = str_dup("import stdlib/stdlib.sl\n");
    StrMap visited;
    strmap_init(&visited);
    FileSpanVec spans;
    VEC_INIT(&spans);
    int line_counter = 1;
    char *expanded = expand_imports_from_source(primer,
                                                 g_l2_ct_jit_ctx.root_dir,
                                                 &g_l2_ct_jit_ctx.include_dirs,
                                                 &visited, &spans, &line_counter);
    if (expanded && *expanded) {
        parse_tokens(&g_l2_ct_jit_ctx.parser, expanded);
    }
    for (size_t i = 0; i < spans.len; i++) free(spans.data[i].path);
    VEC_FREE(&spans);
    strmap_free(&visited);
    free(expanded);
    free(primer);

    g_l2_ct_jit_ctx_ready = true;
}

// Bare JIT pages initialize only r12, so reject runtime symbols, syscalls,
// externs, and r13 references that require the normal runtime environment.
static bool l2_ct_asm_is_jit_safe(const char *asm_text) {
    if (!asm_text) return false;
    static const char *banned[] = {
        "[rel ",
        "syscall",
        "extern ",
        "section ",
        "[abs ",
        // r13 is not initialized in a bare JIT page; use the trampoline instead.
        "r13",
        NULL,
    };
    for (int i = 0; banned[i]; i++) {
        if (strstr(asm_text, banned[i])) {
            return false;
        }
    }
    return true;
}

// Render a flat snippet with its entry point first, followed by JIT-safe
// stdlib :asm definitions needed to resolve calls.
static char *l2_ct_render_snippet_asm(const char *source, long source_len) {
    l2_ct_jit_ctx_init_once();
    L2EvalContext *ctx = &g_l2_ct_jit_ctx;

    long uid = ++g_l2_ct_jit_seq;
    char snippet_name[64];
    snprintf(snippet_name, sizeof(snippet_name), "__l2_ct_snip_%ld", uid);

    char *wrapped = str_printf("word %s\n%.*s\nend\n",
                                snippet_name, (int)source_len, source);
    if (!wrapped) return NULL;

    size_t forms_before = ctx->parser.module.forms.len;
    // parse_tokens reuses its token vector, so discard the primer tokens.
    for (size_t i = 0; i < ctx->parser.tokens.len; i++) {
        free(ctx->parser.tokens.data[i].lexeme);
    }
    ctx->parser.tokens.len = 0;
    parse_tokens(&ctx->parser, wrapped);
    free(wrapped);

    Definition *snippet_def = NULL;
    for (size_t i = ctx->parser.module.forms.len; i > forms_before; i--) {
        Form form = ctx->parser.module.forms.data[i - 1];
        if (form.kind == FORM_DEF) {
            Definition *d = (Definition *)form.ptr;
            if (d && str_equals(d->name, snippet_name)) {
                snippet_def = d;
                break;
            }
        }
    }
    if (!snippet_def) return NULL;

    Emission emission;
    emission_init(&emission);
    EmitContext ectx;
    ectx.emission = &emission;
    ectx.dictionary = &ctx->dict;
    strmap_init(&ectx.string_labels);
    strmap_init(&ectx.externs);
    strmap_init(&ectx.label_cache);
    ectx.unique_id = 0;
    ectx.debug = false;

    // Keep the entry point at the start of the assembled blob.
    VEC_PUSH(&emission.text, str_dup("l2_ct_entry:"));
    FunctionEmitter builder;
    emitter_init(&builder, &emission.text, false);
    StrVec inline_stack;
    VEC_INIT(&inline_stack);
    emit_ops(&ectx, &builder, &snippet_def->body, &inline_stack);
    emit_line(&builder, "    ret");
    VEC_FREE(&inline_stack);

    // Use the dictionary to avoid duplicate forms; unsafe bodies are skipped
    // and cause JIT fallback if referenced.
    StrMap *dwords = &ctx->dict.words;
    for (size_t i = 0; i < dwords->cap; i++) {
        if (!dwords->keys || !dwords->keys[i]) continue;
        Word *word = (Word *)dwords->values[i];
        if (!word || !word->asm_def) continue;
        AsmDefinition *def = word->asm_def;
        if (def->compile_only || !def->body) continue;
        if (!l2_ct_asm_is_jit_safe(def->body)) continue;
        emit_asm_definition(&ectx, def);
    }

    // Keystone consumes flat instruction text, not section directives.
    char *asm_text = str_join_parts(emission.text.data, emission.text.len, NULL, "\n");

    for (size_t i = 0; i < emission.text.len; i++) free(emission.text.data[i]);
    for (size_t i = 0; i < emission.data.len; i++) free(emission.data.data[i]);
    for (size_t i = 0; i < emission.bss.len; i++) free(emission.bss.data[i]);
    VEC_FREE(&emission.text);
    VEC_FREE(&emission.data);
    VEC_FREE(&emission.bss);
    strmap_free(&ectx.string_labels);
    strmap_free(&ectx.externs);
    strmap_free(&ectx.label_cache);

    return asm_text;
}

// Return an executable page on success, or -1 so the caller can use the
// eval trampoline.
static int l2_compile_try_jit(const char *source, long source_len,
                               void **out_page, size_t *out_size) {
    if (!source || source_len < 0) return -1;
    if (!out_page || !out_size) return -1;

    char *asm_text = l2_ct_render_snippet_asm(source, source_len);
    if (!asm_text) return -1;

    if (!l2_ct_asm_is_jit_safe(asm_text)) {
        free(asm_text);
        return -1;
    }

    // Keystone uses Intel syntax and rejects NASM size qualifiers/comments.
    asm_text = l2_ct_asm_intelize(asm_text);
    if (!asm_text) return -1;

    uint8_t *bytes = NULL;
    size_t len = 0;
    int rc = l2_ks_assemble(asm_text, &bytes, &len);
    free(asm_text);
    if (rc != 0 || !bytes || len == 0) {
        free(bytes);
        return -1;
    }

    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0) page_size = 4096;
    size_t alloc = ((len + (size_t)page_size - 1) / (size_t)page_size) * (size_t)page_size;

    void *page = mmap(NULL, alloc, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (page == MAP_FAILED) {
        free(bytes);
        return -1;
    }
    memcpy(page, bytes, len);
    free(bytes);
    if (mprotect(page, alloc, PROT_READ | PROT_EXEC) != 0) {
        munmap(page, alloc);
        return -1;
    }
    __builtin___clear_cache((char *)page, (char *)page + len);

    *out_page = page;
    *out_size = alloc;
    return 0;
}

// Per-word JIT cache; entries live as long as the primed CT context.
typedef struct AsmJitCacheEntry {
    char *word_name;
    void *entry_pt;
    size_t page_size;
    struct AsmJitCacheEntry *next;
} AsmJitCacheEntry;

static AsmJitCacheEntry *g_asm_jit_word_cache = NULL;

static void *asm_jit_cache_lookup(const char *name) {
    if (!name) return NULL;
    for (AsmJitCacheEntry *e = g_asm_jit_word_cache; e; e = e->next) {
        if (strcmp(e->word_name, name) == 0) return e->entry_pt;
    }
    return NULL;
}

static void asm_jit_cache_insert(const char *name, void *entry_pt, size_t page_size) {
    AsmJitCacheEntry *e = (AsmJitCacheEntry *)xmalloc(sizeof(*e));
    e->word_name = str_dup(name);
    e->entry_pt = entry_pt;
    e->page_size = page_size;
    e->next = g_asm_jit_word_cache;
    g_asm_jit_word_cache = e;
}

// JIT a CT :asm call when safe; otherwise preserve its normal handling.
static bool ct_try_asm_jit(CompileTimeVM *vm, Word *word, AsmDefinition *asm_def) {
    if (!vm || !word || !word->name) return false;
    if (!asm_def || !asm_def->body) return false;
    // Runtime cells do not preserve CT value kinds, so only integers can be
    // marshalled safely.
    for (size_t i = 0; i < vm->stack.len; i++) {
        if (vm->stack.data[i].kind != CT_INT) return false;
    }
    if (!l2_ct_asm_is_jit_safe(asm_def->body)) return false;

    // Cached by body text: the program's own :asm words (and redefinitions
    // of stdlib ones) get their own code.
    void *fn = asm_jit_cache_lookup(asm_def->body);
    if (!fn) {
        // Assemble the body itself as a function (the trampoline sets r12
        // and calls it).  A body that calls other words cannot be assembled
        // alone; then compile a call in the primed stdlib context instead.
        char *text = l2_ct_asm_intelize(str_printf("%s\nret\n", asm_def->body));
        uint8_t *bytes = NULL;
        size_t len = 0;
        if (text && l2_ks_assemble_quiet(text, &bytes, &len) == 0 && bytes && len) {
            fn = l2_jit_install(bytes, len);
        }
        free(bytes);
        free(text);
        if (!fn) {
            void *page = NULL;
            size_t sz = 0;
            if (l2_compile_try_jit(word->name, (long)strlen(word->name),
                                    &page, &sz) != 0) {
                return false;
            }
            fn = page;
        }
        asm_jit_cache_insert(asm_def->body, fn, 0);
    }

    // Match the L2 stack layout and reserve space for values pushed by the word.
    size_t headroom = 64;
    size_t cap = vm->stack.len + headroom;
    uint64_t *scratch = (uint64_t *)xmalloc(cap * sizeof(uint64_t));
    memset(scratch, 0, cap * sizeof(uint64_t));
    uintptr_t base = (uintptr_t)(scratch + cap);
    uintptr_t sp = base;
    // Push bottom first so TOS ends up at [sp].
    for (size_t i = 0; i < vm->stack.len; i++) {
        sp -= 8;
        *(uint64_t *)sp = (uint64_t)vm->stack.data[i].as.i64;
    }

    uintptr_t new_sp = (uintptr_t)l2_invoke_trampoline(fn, (uint64_t)sp);
    if (new_sp > base || new_sp < (uintptr_t)scratch) {
        // Reject an invalid stack pointer without altering the CT stack.
        free(scratch);
        return false;
    }

    size_t new_len = (base - new_sp) / 8;
    vm->stack.len = 0;
    // Push bottom-first into the CT VM stack so TOS remains the same.
    for (ssize_t i = (ssize_t)new_len - 1; i >= 0; i--) {
        uint64_t cell = *(uint64_t *)(new_sp + (uintptr_t)(i * 8));
        ct_stack_push(&vm->stack, ct_make_int((int64_t)cell));
    }
    free(scratch);
    return true;
}

void *l2_compile(const char *source, long source_len) {
    if (!source || source_len < 0) {
        fprintf(stderr, "[error] l2_compile received invalid source\n");
        return NULL;
    }

    // Try native JIT first.  On any failure fall back to the eval
    // trampoline that dispatches through l2_eval_compute at call time.
    void *jit_page = NULL;
    size_t jit_size = 0;
    if (l2_compile_try_jit(source, source_len, &jit_page, &jit_size) == 0) {
        l2_compiled_entry_t *entry = (l2_compiled_entry_t *)malloc(sizeof(*entry));
        if (entry) {
            entry->page = jit_page;
            entry->size = jit_size;
            entry->next = g_l2_compiled_head;
            g_l2_compiled_head = entry;
        }
        return jit_page;
    }

    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0) {
        page_size = 4096;
    }

    size_t needed = (size_t)L2_COMPILE_TRAMPOLINE_SLOT + (size_t)source_len + 1;
    size_t alloc = ((needed + (size_t)page_size - 1) / (size_t)page_size) * (size_t)page_size;

    void *page = mmap(NULL, alloc, PROT_READ | PROT_WRITE | PROT_EXEC,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (page == MAP_FAILED) {
        fprintf(stderr, "[error] l2_compile mmap failed: %s\n", strerror(errno));
        return NULL;
    }

    uint8_t *code = (uint8_t *)page;
    char *src_slot = (char *)page + L2_COMPILE_TRAMPOLINE_SLOT;
    memcpy(src_slot, source, (size_t)source_len);
    src_slot[source_len] = '\0';

    size_t off = 0;
    // Build the SysV trampoline and pass the live L2 data stack to the VM.
    code[off++] = 0x55;
    code[off++] = 0x48; code[off++] = 0x89; code[off++] = 0xE5;
    code[off++] = 0x48; code[off++] = 0x83; code[off++] = 0xE4; code[off++] = 0xF0;
    code[off++] = 0x48; code[off++] = 0xBF;
    void *src_ptr_val = src_slot;
    memcpy(code + off, &src_ptr_val, sizeof(void *)); off += 8;
    code[off++] = 0x48; code[off++] = 0xBE;
    int64_t slen_val = (int64_t)source_len;
    memcpy(code + off, &slen_val, 8); off += 8;
    code[off++] = 0x4C; code[off++] = 0x89; code[off++] = 0xE2;
    code[off++] = 0x48; code[off++] = 0xB8;
    void *fn_ptr = (void *)&l2_eval_compute;
    memcpy(code + off, &fn_ptr, sizeof(void *)); off += 8;
    code[off++] = 0xFF; code[off++] = 0xD0;
    code[off++] = 0x49; code[off++] = 0x89; code[off++] = 0xC4;
    code[off++] = 0x48; code[off++] = 0x89; code[off++] = 0xEC;
    code[off++] = 0x5D;
    code[off++] = 0xC3;

    if (off > (size_t)L2_COMPILE_TRAMPOLINE_SLOT) {
        // Keep the generated code within its reserved slot.
        fprintf(stderr, "[error] l2_compile trampoline overflow (%zu > %d)\n",
                off, L2_COMPILE_TRAMPOLINE_SLOT);
        munmap(page, alloc);
        return NULL;
    }

    l2_compiled_entry_t *entry = (l2_compiled_entry_t *)malloc(sizeof(*entry));
    if (entry) {
        entry->page = page;
        entry->size = alloc;
        entry->next = g_l2_compiled_head;
        g_l2_compiled_head = entry;
    }
    // Flush the instruction cache after writing executable bytes.
    __builtin___clear_cache((char *)page, (char *)page + off);
    return page;
}

void *l2_compile_cstr(const char *source) {
    if (!source) {
        return NULL;
    }
    return l2_compile(source, (long)strlen(source));
}

void l2_release(void *fn_ptr) {
    if (!fn_ptr) {
        return;
    }
    l2_compiled_entry_t **prev = &g_l2_compiled_head;
    while (*prev) {
        if ((*prev)->page == fn_ptr) {
            l2_compiled_entry_t *victim = *prev;
            *prev = victim->next;
            if (munmap(victim->page, victim->size) != 0) {
                fprintf(stderr, "[warn] l2_release munmap failed: %s\n", strerror(errno));
            }
            free(victim);
            return;
        }
        prev = &(*prev)->next;
    }
    fprintf(stderr, "[warn] l2_release: pointer %p not tracked\n", fn_ptr);
}

/* SysV bridge that invokes an L2 function with a caller-supplied r12 stack.
 * It goes through l2_native_call, which saves every register L2 code may
 * change (rbx, rbp, r12-r15) and calls with an aligned stack.  The callee
 * also gets a return stack of its own in r13: a slice of a per-thread
 * buffer, one slice per nesting level (L2 code can call back into eval,
 * which can come back here), or a fresh one past the preallocated levels. */
#if defined(L2_HAVE_NATIVE_CALL)
#define L2_TRAMPOLINE_RSTACK_CELLS 4096
#define L2_TRAMPOLINE_LEVELS 8
static __thread uint64_t *g_l2_trampoline_rstacks = NULL;
static __thread size_t g_l2_trampoline_depth = 0;

uint64_t l2_invoke_trampoline(void *fn, uint64_t r12_in) {
    if (!g_l2_trampoline_rstacks) {
        g_l2_trampoline_rstacks = (uint64_t *)xmalloc(
            (size_t)L2_TRAMPOLINE_LEVELS * L2_TRAMPOLINE_RSTACK_CELLS * sizeof(uint64_t));
    }
    uint64_t *owned = NULL;
    uint64_t *base;
    if (g_l2_trampoline_depth < L2_TRAMPOLINE_LEVELS) {
        base = g_l2_trampoline_rstacks + g_l2_trampoline_depth * L2_TRAMPOLINE_RSTACK_CELLS;
    } else {
        owned = (uint64_t *)xmalloc(L2_TRAMPOLINE_RSTACK_CELLS * sizeof(uint64_t));
        base = owned;
    }
    g_l2_trampoline_depth++;
    uint64_t r12_out = l2_native_call(fn, r12_in, (uint64_t)(uintptr_t)(base + L2_TRAMPOLINE_RSTACK_CELLS));
    g_l2_trampoline_depth--;
    free(owned);
    return r12_out;
}
#else
uint64_t l2_invoke_trampoline(void *fn, uint64_t r12_in) {
    (void)fn; (void)r12_in;
    fprintf(stderr, "[error] l2_invoke_trampoline unimplemented on this arch\n");
    return r12_in;
}
#endif

// -----------------------------------------------------------------------------
// Keystone-backed JIT implementation.
// -----------------------------------------------------------------------------

// Declare Keystone's stable C ABI directly so development headers are optional.
typedef int l2_ks_arch_t;
struct l2_ks_engine_opaque;
typedef struct l2_ks_engine_opaque l2_ks_engine_t;

#define L2_KS_ARCH_X86  4  /* KS_ARCH_X86  == 4 in keystone.h */
#define L2_KS_MODE_64   8  /* KS_MODE_64   == 1 << 3         */
#define L2_KS_OPT_SYNTAX        1  /* KS_OPT_SYNTAX          */
#define L2_KS_OPT_SYNTAX_NASM   2  /* KS_OPT_SYNTAX_NASM     */

typedef int (*l2_ks_open_fn)(l2_ks_arch_t arch, int mode, l2_ks_engine_t **ks);
typedef int (*l2_ks_close_fn)(l2_ks_engine_t *ks);
typedef int (*l2_ks_asm_fn)(l2_ks_engine_t *ks, const char *string,
                            uint64_t address, unsigned char **encoding,
                            size_t *encoding_size, size_t *stat_count);
typedef void (*l2_ks_free_fn)(unsigned char *p);
typedef int (*l2_ks_errno_fn)(l2_ks_engine_t *ks);
typedef const char *(*l2_ks_strerror_fn)(int err);
typedef int (*l2_ks_option_fn)(l2_ks_engine_t *ks, int type, size_t value);

typedef struct {
    void *handle;
    l2_ks_engine_t *engine;
    l2_ks_open_fn open_fn;
    l2_ks_close_fn close_fn;
    l2_ks_asm_fn asm_fn;
    l2_ks_free_fn free_fn;
    l2_ks_errno_fn errno_fn;
    l2_ks_strerror_fn strerror_fn;
    l2_ks_option_fn option_fn;
    int available;   // 1 iff engine successfully opened
    int probed;      // 1 iff we've attempted to load
} l2_ks_state_t;

static l2_ks_state_t g_ks_state = {0};

static const char *l2_ks_lib_candidates[] = {
    /* system fallbacks: use whatever the loader finds on default paths */
    "libkeystone.so.0",
    "libkeystone.so",
    "/usr/lib/x86_64-linux-gnu/libkeystone.so.0",
    "/usr/lib/x86_64-linux-gnu/libkeystone.so",
    "/usr/local/lib/libkeystone.so.0",
    "/usr/local/lib/libkeystone.so",
    NULL,
};

/* Compile-time vendored paths.  build_l2_lib.sh defines
 * L2_SOURCE_ROOT so we can find tools/vendor/libkeystone.so shipped
 * with the repository (see tools/fetch_keystone.sh). */
static const char *l2_ks_vendored_paths[] = {
#ifdef L2_SOURCE_ROOT
    L2_SOURCE_ROOT "/tools/vendor/libkeystone.so",
    L2_SOURCE_ROOT "/build/libkeystone.so",
#endif
    NULL,
};

/* Try to locate a vendored libkeystone.so sitting next to the loaded
 * libl2.so on disk.  Only relevant for the shared build. */
static void *l2_ks_try_sibling_of_libl2(void) {
#ifdef L2_AS_LIBRARY
    Dl_info info;
    /* pass any symbol defined in this TU; use ourselves */
    if (!dladdr((void *)&l2_ks_try_sibling_of_libl2, &info) || !info.dli_fname) {
        return NULL;
    }
    const char *fname = info.dli_fname;
    const char *slash = strrchr(fname, '/');
    if (!slash) return NULL;
    size_t dirlen = (size_t)(slash - fname);
    /* candidate patterns to try relative to libl2's directory */
    static const char *rels[] = {
        "/libkeystone.so",
        "/libkeystone.so.0",
        "/../tools/vendor/libkeystone.so",
        NULL,
    };
    char buf[4096];
    for (const char **r = rels; *r; ++r) {
        if (dirlen + strlen(*r) + 1 >= sizeof(buf)) continue;
        memcpy(buf, fname, dirlen);
        strcpy(buf + dirlen, *r);
        void *h = dlopen(buf, RTLD_NOW | RTLD_LOCAL);
        if (h) return h;
    }
#endif
    return NULL;
}

static int l2_ks_init(void) {
    if (g_ks_state.probed) {
        return g_ks_state.available;
    }
    g_ks_state.probed = 1;

    const char *env_path = getenv("L2_KEYSTONE_PATH");
    void *h = NULL;
    if (env_path && *env_path) {
        h = dlopen(env_path, RTLD_NOW | RTLD_LOCAL);
        if (!h) {
            fprintf(stderr, "[warn] l2_jit: dlopen(%s) failed: %s\n",
                    env_path, dlerror());
        }
    }
    /* Prefer the vendored copy that ships with the repo before
     * consulting system paths, so libl2 stays self-contained. */
    for (const char **p = l2_ks_vendored_paths; !h && *p; ++p) {
        h = dlopen(*p, RTLD_NOW | RTLD_LOCAL);
    }
    if (!h) {
        h = l2_ks_try_sibling_of_libl2();
    }
    for (const char **p = l2_ks_lib_candidates; !h && *p; ++p) {
        h = dlopen(*p, RTLD_NOW | RTLD_LOCAL);
    }
    if (!h) {
        fprintf(stderr, "[warn] l2_jit: libkeystone not found; "
                        "run tools/fetch_keystone.sh or set "
                        "L2_KEYSTONE_PATH to override\n");
        return 0;
    }
    g_ks_state.handle      = h;
    g_ks_state.open_fn     = (l2_ks_open_fn)     dlsym(h, "ks_open");
    g_ks_state.close_fn    = (l2_ks_close_fn)    dlsym(h, "ks_close");
    g_ks_state.asm_fn      = (l2_ks_asm_fn)      dlsym(h, "ks_asm");
    g_ks_state.free_fn     = (l2_ks_free_fn)     dlsym(h, "ks_free");
    g_ks_state.errno_fn    = (l2_ks_errno_fn)    dlsym(h, "ks_errno");
    g_ks_state.strerror_fn = (l2_ks_strerror_fn) dlsym(h, "ks_strerror");
    g_ks_state.option_fn   = (l2_ks_option_fn)   dlsym(h, "ks_option");

    if (!g_ks_state.open_fn || !g_ks_state.close_fn ||
        !g_ks_state.asm_fn  || !g_ks_state.free_fn) {
        fprintf(stderr, "[warn] l2_jit: libkeystone missing required symbols\n");
        dlclose(h);
        memset(&g_ks_state, 0, sizeof(g_ks_state));
        g_ks_state.probed = 1;
        return 0;
    }

    int rc = g_ks_state.open_fn(L2_KS_ARCH_X86, L2_KS_MODE_64, &g_ks_state.engine);
    if (rc != 0 || !g_ks_state.engine) {
        fprintf(stderr, "[warn] l2_jit: ks_open failed (rc=%d)\n", rc);
        dlclose(h);
        memset(&g_ks_state, 0, sizeof(g_ks_state));
        g_ks_state.probed = 1;
        return 0;
    }
    // Keystone rejects x86-64 NASM syntax; emitted code is converted to Intel syntax.
    g_ks_state.available = 1;
    return 1;
}

int l2_ks_available(void) {
    return l2_ks_init();
}

int l2_ks_assemble(const char *asm_src, uint8_t **out_buf, size_t *out_len) {
    if (!asm_src || !out_buf || !out_len) {
        return -1;
    }
    *out_buf = NULL;
    *out_len = 0;
    if (!l2_ks_init()) {
        return -1;
    }

    unsigned char *encoding = NULL;
    size_t size = 0;
    size_t stat_count = 0;
    int rc = g_ks_state.asm_fn(g_ks_state.engine, asm_src, 0,
                               &encoding, &size, &stat_count);
    if (rc != 0) {
        int err = g_ks_state.errno_fn ? g_ks_state.errno_fn(g_ks_state.engine) : -1;
        const char *msg = (g_ks_state.strerror_fn && err >= 0)
                          ? g_ks_state.strerror_fn(err) : "unknown";
        if (!g_ks_quiet) {
            fprintf(stderr, "[error] l2_ks_assemble: keystone error %d: %s\n", err, msg);
        }
        if (encoding) {
            g_ks_state.free_fn(encoding);
        }
        return -1;
    }
    if (!encoding || size == 0) {
        if (encoding) {
            g_ks_state.free_fn(encoding);
        }
        fprintf(stderr, "[error] l2_ks_assemble: keystone produced no bytes\n");
        return -1;
    }
    uint8_t *out = (uint8_t *)malloc(size);
    if (!out) {
        g_ks_state.free_fn(encoding);
        return -1;
    }
    memcpy(out, encoding, size);
    g_ks_state.free_fn(encoding);
    *out_buf = out;
    *out_len = size;
    return 0;
}

// Executable-memory manager (W^X): mmap RW, copy, mprotect RX.  Pages
// are tracked in an intrusive list so l2_jit_release can find their
// size for munmap without a lookup table.
typedef struct l2_jit_entry {
    void *page;
    size_t size;
    struct l2_jit_entry *next;
} l2_jit_entry_t;

static l2_jit_entry_t *g_l2_jit_head = NULL;

static void *l2_jit_install(const uint8_t *bytes, size_t len) {
    if (!bytes || len == 0) {
        return NULL;
    }
    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0) {
        page_size = 4096;
    }
    size_t alloc = ((len + (size_t)page_size - 1) / (size_t)page_size) * (size_t)page_size;

    void *page = mmap(NULL, alloc, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (page == MAP_FAILED) {
        fprintf(stderr, "[error] l2_jit_install: mmap failed: %s\n", strerror(errno));
        return NULL;
    }
    memcpy(page, bytes, len);
    if (mprotect(page, alloc, PROT_READ | PROT_EXEC) != 0) {
        fprintf(stderr, "[error] l2_jit_install: mprotect failed: %s\n", strerror(errno));
        munmap(page, alloc);
        return NULL;
    }
    __builtin___clear_cache((char *)page, (char *)page + len);

    l2_jit_entry_t *entry = (l2_jit_entry_t *)malloc(sizeof(*entry));
    if (entry) {
        entry->page = page;
        entry->size = alloc;
        entry->next = g_l2_jit_head;
        g_l2_jit_head = entry;
    }
    return page;
}

void *l2_jit_from_asm(const char *asm_src, long src_len) {
    if (!asm_src || src_len < 0) {
        return NULL;
    }
    // Keystone reads asm_src until NUL, so ensure a NUL-terminated copy
    // when the caller passed a length.  L2 string literals are already
    // NUL-terminated but we don't want to rely on that.
    char *nul_src = (char *)malloc((size_t)src_len + 1);
    if (!nul_src) {
        return NULL;
    }
    memcpy(nul_src, asm_src, (size_t)src_len);
    nul_src[src_len] = '\0';

    uint8_t *bytes = NULL;
    size_t len = 0;
    int rc = l2_ks_assemble(nul_src, &bytes, &len);
    free(nul_src);
    if (rc != 0) {
        return NULL;
    }
    void *fn = l2_jit_install(bytes, len);
    free(bytes);
    return fn;
}

void *l2_jit_from_asm_cstr(const char *asm_src) {
    if (!asm_src) {
        return NULL;
    }
    return l2_jit_from_asm(asm_src, (long)strlen(asm_src));
}

void l2_jit_release(void *fn_ptr) {
    if (!fn_ptr) {
        return;
    }
    l2_jit_entry_t **prev = &g_l2_jit_head;
    while (*prev) {
        if ((*prev)->page == fn_ptr) {
            l2_jit_entry_t *victim = *prev;
            *prev = victim->next;
            if (munmap(victim->page, victim->size) != 0) {
                fprintf(stderr, "[warn] l2_jit_release: munmap failed: %s\n", strerror(errno));
            }
            free(victim);
            return;
        }
        prev = &(*prev)->next;
    }
    fprintf(stderr, "[warn] l2_jit_release: pointer %p not tracked\n", fn_ptr);
}

#else  /* !__x86_64__ */

void *l2_compile(const char *source, long source_len) {
    (void)source; (void)source_len;
    fprintf(stderr, "[error] l2_compile is only supported on x86_64\n");
    return NULL;
}

void *l2_compile_cstr(const char *source) {
    (void)source;
    return NULL;
}

void l2_release(void *fn_ptr) {
    (void)fn_ptr;
}

int l2_ks_available(void) { return 0; }

int l2_ks_assemble(const char *asm_src, uint8_t **out_buf, size_t *out_len) {
    (void)asm_src;
    if (out_buf) *out_buf = NULL;
    if (out_len) *out_len = 0;
    fprintf(stderr, "[error] l2_ks_assemble is only supported on x86_64\n");
    return -1;
}

void *l2_jit_from_asm(const char *asm_src, long src_len) {
    (void)asm_src; (void)src_len;
    fprintf(stderr, "[error] l2_jit_from_asm is only supported on x86_64\n");
    return NULL;
}

void *l2_jit_from_asm_cstr(const char *asm_src) {
    (void)asm_src;
    return NULL;
}

void l2_jit_release(void *fn_ptr) {
    (void)fn_ptr;
}

#endif  /* __x86_64__ */

#ifndef L2_AS_LIBRARY
int main(int argc, char **argv) {
    return l2_cli(argc, argv);
}
#endif

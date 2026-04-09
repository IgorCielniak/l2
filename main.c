#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <ctype.h>
#include <stdarg.h>
#include <errno.h>
#include <limits.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <dirent.h>

#define ARRAY_LEN(x) (sizeof(x) / sizeof((x)[0]))

int l2_cli(int argc, char **argv);
int l2_eval(const char *source, long source_len);
int l2_eval_cstr(const char *source);

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
    OP_RET
} OpKind;

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
} Definition;

typedef struct {
    char *name;
    char *body;
    bool immediate;
    bool compile_only;
    bool runtime_only;
    bool effect_string_io;
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

static void reader_resort(Reader *reader) {
    for (size_t i = 0; i < reader->token_order.len; i++) {
        for (size_t j = i + 1; j < reader->token_order.len; j++) {
            if (strlen(reader->token_order.data[j]) > strlen(reader->token_order.data[i])) {
                char *tmp = reader->token_order.data[i];
                reader->token_order.data[i] = reader->token_order.data[j];
                reader->token_order.data[j] = tmp;
            }
        }
    }
}

static void reader_add_tokens(Reader *reader, const char *tok) {
    if (!tok || !*tok) {
        return;
    }
    for (size_t i = 0; i < reader->custom_tokens.len; i++) {
        if (strcmp(reader->custom_tokens.data[i], tok) == 0) {
            return;
        }
    }
    VEC_PUSH(&reader->custom_tokens, str_dup(tok));
    VEC_PUSH(&reader->token_order, reader->custom_tokens.data[reader->custom_tokens.len - 1]);
    reader_resort(reader);
}

static void reader_add_token_chars(Reader *reader, const char *chars) {
    if (!chars) {
        return;
    }
    char buf[2] = {0, 0};
    for (const char *p = chars; *p; p++) {
        buf[0] = *p;
        reader_add_tokens(reader, buf);
    }
}

typedef struct {
    const char *source;
    size_t length;
    size_t index;
    int line;
    int column;
    Reader *reader;
} Tokenizer;

static void tokenizer_init(Tokenizer *tokenizer, Reader *reader, const char *source) {
    tokenizer->source = source;
    tokenizer->length = strlen(source);
    tokenizer->index = 0;
    tokenizer->line = 1;
    tokenizer->column = 0;
    tokenizer->reader = reader;
}

static bool tokenizer_next(Tokenizer *tokenizer, Token *out) {
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
            while (idx < len && src[idx] != '\n') {
                idx++;
            }
            continue;
        }
        if (ch == ';' && idx + 1 < len && isalpha((unsigned char)src[idx + 1])) {
            size_t start = idx;
            int token_line = line;
            int token_col = col;
            idx++;
            col++;
            size_t tok_len = idx - start;
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
            bool is_sep = isspace((unsigned char)c) || c == '"' || c == '#';
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
        char *type;
        char *false_label;
        char *end_label;
        char *begin_label;
        char *loop_label;
        int line;
        int column;
        StrVec with_names;
    } *control_stack;
    size_t control_len;
    size_t control_cap;
    int label_counter;
    char *token_hook;
    Token last_token;
    bool has_last_token;
    StrMap variable_labels;
    StrMap variable_words;
    CompileTimeVM *ct_vm;
    StrVec *custom_prelude;
    StrVec *custom_bss;
    bool pending_inline_def;
    bool uses_libc;
    bool uses_libm;
    char *primary_path;
};

typedef enum {
    CT_NIL,
    CT_INT,
    CT_STR,
    CT_TOKEN,
    CT_LIST,
    CT_MAP,
    CT_LEXER
} CtValueKind;

typedef struct CtValue CtValue;

VEC_DECL(CtValueVec, CtValue);

typedef struct {
    CtValueVec items;
} CtList;

typedef struct {
    char **keys;
    CtValue *values;
    size_t cap;
    size_t len;
} CtMap;

typedef struct {
    Parser *parser;
    bool separators[256];
    TokenVec buffer;
} SplitLexer;

struct CtValue {
    CtValueKind kind;
    union {
        int64_t i64;
        char *str;
        Token token;
        CtList *list;
        CtMap *map;
        SplitLexer *lexer;
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
};

static void ct_value_free(CtValue *value);

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

static CtValue ct_make_lexer(SplitLexer *lexer) {
    CtValue v = {0};
    v.kind = CT_LEXER;
    v.as.lexer = lexer;
    return v;
}

static void ct_value_free(CtValue *value) {
    if (!value) {
        return;
    }
    if (value->kind == CT_STR) {
        free(value->as.str);
    }
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
    return map;
}

static void ct_map_grow(CtMap *map) {
    size_t new_cap = map->cap ? map->cap * 2 : 64;
    char **new_keys = (char **)xmalloc(new_cap * sizeof(char *));
    CtValue *new_vals = (CtValue *)xmalloc(new_cap * sizeof(CtValue));
    for (size_t i = 0; i < new_cap; i++) {
        new_keys[i] = NULL;
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

static void ct_map_set(CtMap *map, const char *key, CtValue value) {
    if (!map->cap || (map->len + 1) * 3 >= map->cap * 2) {
        ct_map_grow(map);
    }
    uint64_t hash = hash_str(key);
    size_t idx = (size_t)(hash & (map->cap - 1));
    while (map->keys[idx]) {
        if (strcmp(map->keys[idx], key) == 0) {
            ct_value_free(&map->values[idx]);
            map->values[idx] = value;
            return;
        }
        idx = (idx + 1) & (map->cap - 1);
    }
    map->keys[idx] = str_dup(key);
    map->values[idx] = value;
    map->len++;
}

static bool ct_map_get(CtMap *map, const char *key, CtValue *out) {
    if (!map->cap) {
        return false;
    }
    uint64_t hash = hash_str(key);
    size_t idx = (size_t)(hash & (map->cap - 1));
    size_t start = idx;
    while (map->keys[idx]) {
        if (strcmp(map->keys[idx], key) == 0) {
            *out = map->values[idx];
            return true;
        }
        idx = (idx + 1) & (map->cap - 1);
        if (idx == start) {
            break;
        }
    }
    return false;
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

static bool is_identifier(const char *text) {
    if (!text || !*text) {
        return false;
    }
    if (!(isalpha((unsigned char)text[0]) || text[0] == '_')) {
        return false;
    }
    for (const char *p = text + 1; *p; p++) {
        if (!(isalnum((unsigned char)*p) || *p == '_')) {
            return false;
        }
    }
    return true;
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
    parser->control_stack = NULL;
    parser->control_len = 0;
    parser->control_cap = 0;
    parser->label_counter = 0;
    parser->token_hook = NULL;
    parser->has_last_token = false;
    strmap_init(&parser->variable_labels);
    strmap_init(&parser->variable_words);
    parser->ct_vm = NULL;
    parser->custom_prelude = NULL;
    parser->custom_bss = NULL;
    parser->pending_inline_def = false;
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

static int64_t ct_pop_int(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->stack);
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
        } else if (v.kind == CT_LEXER) {
            kind = "lexer";
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
    CtValue v = ct_stack_pop(&vm->stack);
    if (v.kind == CT_TOKEN) {
        return str_dup(v.as.token.lexeme);
    }
    if (v.kind != CT_STR) {
        ct_trace_error(vm, "expected string on compile-time stack");
    }
    return str_dup(v.as.str);
}

static CtList *ct_pop_list(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->stack);
    if (v.kind != CT_LIST) {
        ct_trace_error(vm, "expected list on compile-time stack");
    }
    return v.as.list;
}

static CtMap *ct_pop_map(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->stack);
    if (v.kind != CT_MAP) {
        ct_trace_error(vm, "expected map on compile-time stack");
    }
    return v.as.map;
}

static Token ct_pop_token(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->stack);
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

static bool ct_try_asm_io(CompileTimeVM *vm, Word *word, AsmDefinition *asm_def) {
    if (asm_def && asm_def->effect_string_io) {
        CtValue v = ct_stack_pop(&vm->stack);
        if (v.kind == CT_STR) {
            FILE *out = stdout;
            if (strcmp(word->name, "ewrite_buf") == 0) {
                out = stderr;
            }
            fputs(v.as.str ? v.as.str : "", out);
        } else {
            ct_stack_pop(&vm->stack);
        }
        return true;
    }
    if (strcmp(word->name, "putc") == 0) {
        CtValue v = ct_stack_pop(&vm->stack);
        int ch = 0;
        if (v.kind == CT_INT) {
            ch = (int)v.as.i64;
        } else if (v.kind == CT_STR && v.as.str && v.as.str[0]) {
            ch = (unsigned char)v.as.str[0];
        }
        fputc(ch, stdout);
        return true;
    }
    return false;
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
                ct_stack_push(&vm->stack, ct_make_int((int64_t)node.data.f64));
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
        if (node.kind == OP_BRANCH_ZERO) {
            CtValue v = ct_stack_pop(&vm->stack);
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

static void ct_word_call(CompileTimeVM *vm, Word *word) {
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
        if (word->asm_def || word->ct_asm_def) {
            AsmDefinition *asm_def = word->ct_asm_def ? word->ct_asm_def : word->asm_def;
            ct_try_asm_io(vm, word, asm_def);
            vm->call_stack.len--;
            return;
        }
        if (word->is_extern) {
            int pops = word->extern_arg_count > 0 ? word->extern_arg_count : word->extern_inputs;
            for (int i = 0; i < pops; i++) {
                ct_stack_pop(&vm->stack);
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
        fprintf(stderr, "[error] word '%s' has no compile-time definition\n", word->name);
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
    ct_stack_pop(&vm->stack);
}

static void ct_intrinsic_swap(CompileTimeVM *vm) {
    CtValue a = ct_stack_pop(&vm->stack);
    CtValue b = ct_stack_pop(&vm->stack);
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
    CtValue v = ct_stack_pop(&vm->stack);
    ct_stack_push(&vm->rstack, v);
}

static void ct_intrinsic_from_r(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->rstack);
    ct_stack_push(&vm->stack, v);
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
    CtValue b = ct_stack_pop(&vm->stack);
    CtValue a = ct_stack_pop(&vm->stack);
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
    CtValue b = ct_stack_pop(&vm->stack);
    CtValue a = ct_stack_pop(&vm->stack);
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
    CtValue v = ct_stack_pop(&vm->stack);
    ct_stack_push(&vm->stack, ct_make_int(!ct_truthy(v)));
}

static void ct_intrinsic_nil(CompileTimeVM *vm) {
    ct_stack_push(&vm->stack, ct_make_nil());
}

static void ct_intrinsic_nilp(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->stack);
    ct_stack_push(&vm->stack, ct_make_int(v.kind == CT_NIL));
}

static void ct_intrinsic_string_eq(CompileTimeVM *vm) {
    char *b = ct_pop_str(vm);
    char *a = ct_pop_str(vm);
    bool eq = strcmp(a, b) == 0;
    free(a);
    free(b);
    ct_stack_push(&vm->stack, ct_make_int(eq));
}

static void ct_intrinsic_string_length(CompileTimeVM *vm) {
    char *s = ct_pop_str(vm);
    ct_stack_push(&vm->stack, ct_make_int((int64_t)strlen(s)));
    free(s);
}

static void ct_intrinsic_string_append(CompileTimeVM *vm) {
    char *b = ct_pop_str(vm);
    char *a = ct_pop_str(vm);
    char *out = str_printf("%s%s", a, b);
    free(a);
    free(b);
    ct_stack_push(&vm->stack, ct_make_str(out));
    free(out);
}

static void ct_intrinsic_string_to_number(CompileTimeVM *vm) {
    char *s = ct_pop_str(vm);
    int64_t out = 0;
    bool ok = try_parse_int(s, &out);
    ct_stack_push(&vm->stack, ct_make_int(out));
    ct_stack_push(&vm->stack, ct_make_int(ok ? 1 : 0));
    free(s);
}

static void ct_intrinsic_int_to_string(CompileTimeVM *vm) {
    int64_t v = ct_pop_int(vm);
    char *out = str_printf("%lld", (long long)v);
    ct_stack_push(&vm->stack, ct_make_str(out));
    free(out);
}

static void ct_intrinsic_identifierp(CompileTimeVM *vm) {
    char *s = ct_pop_str(vm);
    ct_stack_push(&vm->stack, ct_make_int(is_identifier(s)));
    free(s);
}

static void ct_intrinsic_list_new(CompileTimeVM *vm) {
    CtList *list = ct_list_new();
    ct_stack_push(&vm->stack, ct_make_list(list));
}

static void ct_intrinsic_list_append(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->stack);
    CtList *list = ct_pop_list(vm);
    VEC_PUSH(&list->items, v);
    ct_stack_push(&vm->stack, ct_make_list(list));
}

static void ct_intrinsic_list_pop(CompileTimeVM *vm) {
    CtList *list = ct_pop_list(vm);
    if (!list->items.len) {
        ct_stack_push(&vm->stack, ct_make_list(list));
        ct_stack_push(&vm->stack, ct_make_nil());
        return;
    }
    CtValue v = VEC_POP(&list->items);
    ct_stack_push(&vm->stack, ct_make_list(list));
    ct_stack_push(&vm->stack, v);
}

static void ct_intrinsic_list_pop_front(CompileTimeVM *vm) {
    CtList *list = ct_pop_list(vm);
    if (!list->items.len) {
        ct_stack_push(&vm->stack, ct_make_list(list));
        ct_stack_push(&vm->stack, ct_make_nil());
        return;
    }
    CtValue v = list->items.data[0];
    memmove(&list->items.data[0], &list->items.data[1], (list->items.len - 1) * sizeof(CtValue));
    list->items.len--;
    ct_stack_push(&vm->stack, ct_make_list(list));
    ct_stack_push(&vm->stack, v);
}

static void ct_intrinsic_list_length(CompileTimeVM *vm) {
    CtList *list = ct_pop_list(vm);
    ct_stack_push(&vm->stack, ct_make_int((int64_t)list->items.len));
}

static void ct_intrinsic_list_empty(CompileTimeVM *vm) {
    CtList *list = ct_pop_list(vm);
    ct_stack_push(&vm->stack, ct_make_int(list->items.len == 0));
}

static void ct_intrinsic_list_get(CompileTimeVM *vm) {
    int64_t idx = ct_pop_int(vm);
    CtList *list = ct_pop_list(vm);
    CtValue v = ct_make_nil();
    if (idx >= 0 && (size_t)idx < list->items.len) {
        v = list->items.data[idx];
    }
    ct_stack_push(&vm->stack, v);
}

static void ct_intrinsic_list_set(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->stack);
    int64_t idx = ct_pop_int(vm);
    CtList *list = ct_pop_list(vm);
    if (idx < 0 || (size_t)idx >= list->items.len) {
        fprintf(stderr, "[error] list-set index out of range\n");
        exit(1);
    }
    list->items.data[idx] = v;
    ct_stack_push(&vm->stack, ct_make_list(list));
}

static void ct_intrinsic_list_extend(CompileTimeVM *vm) {
    CtList *list2 = ct_pop_list(vm);
    CtList *list1 = ct_pop_list(vm);
    for (size_t i = 0; i < list2->items.len; i++) {
        VEC_PUSH(&list1->items, list2->items.data[i]);
    }
    ct_stack_push(&vm->stack, ct_make_list(list1));
}

static void ct_intrinsic_list_last(CompileTimeVM *vm) {
    CtList *list = ct_pop_list(vm);
    CtValue v = ct_make_nil();
    if (list->items.len) {
        v = list->items.data[list->items.len - 1];
    }
    ct_stack_push(&vm->stack, v);
}

static void ct_intrinsic_list_clone(CompileTimeVM *vm) {
    CtList *list = ct_pop_list(vm);
    CtList *out = ct_list_new();
    for (size_t i = 0; i < list->items.len; i++) {
        VEC_PUSH(&out->items, list->items.data[i]);
    }
    ct_stack_push(&vm->stack, ct_make_list(list));
    ct_stack_push(&vm->stack, ct_make_list(out));
}

static void ct_intrinsic_map_new(CompileTimeVM *vm) {
    CtMap *map = ct_map_new();
    ct_stack_push(&vm->stack, ct_make_map(map));
}

static void ct_intrinsic_map_set(CompileTimeVM *vm) {
    CtValue val = ct_stack_pop(&vm->stack);
    char *key = ct_pop_str(vm);
    CtMap *map = ct_pop_map(vm);
    ct_map_set(map, key, val);
    free(key);
    ct_stack_push(&vm->stack, ct_make_map(map));
}

static void ct_intrinsic_map_get(CompileTimeVM *vm) {
    char *key = ct_pop_str(vm);
    CtMap *map = ct_pop_map(vm);
    CtValue out = ct_make_nil();
    bool ok = ct_map_get(map, key, &out);
    ct_stack_push(&vm->stack, ct_make_map(map));
    ct_stack_push(&vm->stack, out);
    ct_stack_push(&vm->stack, ct_make_int(ok));
    free(key);
}

static void ct_intrinsic_map_has(CompileTimeVM *vm) {
    char *key = ct_pop_str(vm);
    CtMap *map = ct_pop_map(vm);
    CtValue out = ct_make_nil();
    bool ok = ct_map_get(map, key, &out);
    ct_stack_push(&vm->stack, ct_make_map(map));
    ct_stack_push(&vm->stack, ct_make_int(ok));
    free(key);
}

static void ct_intrinsic_token_lexeme(CompileTimeVM *vm) {
    Token tok = ct_pop_token(vm);
    ct_stack_push(&vm->stack, ct_make_str(tok.lexeme));
}

static void ct_intrinsic_token_from_lexeme(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->stack);
    if (v.kind == CT_NIL) {
        v = ct_stack_pop(&vm->stack);
    }
    char *lex = NULL;
    if (v.kind == CT_STR) {
        lex = str_dup(v.as.str);
    } else if (v.kind == CT_TOKEN) {
        lex = str_dup(v.as.token.lexeme);
    } else {
        ct_trace_error(vm, "expected string for token-from-lexeme");
    }
    Token tok = {0};
    tok.lexeme = lex;
    tok.line = 0;
    tok.column = 0;
    tok.start = 0;
    tok.end = 0;
    ct_stack_push(&vm->stack, ct_make_token(tok));
}

static void ct_intrinsic_next_token(CompileTimeVM *vm) {
    Token tok = parser_next_token(vm->parser);
    if (!tok.lexeme) {
        ct_stack_push(&vm->stack, ct_make_nil());
        return;
    }
    ct_stack_push(&vm->stack, ct_make_token(tok));
}

static void ct_intrinsic_peek_token(CompileTimeVM *vm) {
    Token tok = parser_peek_token(vm->parser);
    if (!tok.lexeme) {
        ct_stack_push(&vm->stack, ct_make_nil());
        return;
    }
    ct_stack_push(&vm->stack, ct_make_token(tok));
}

static void ct_intrinsic_inject_tokens(CompileTimeVM *vm) {
    CtList *list = ct_pop_list(vm);
    TokenVec injected;
    VEC_INIT(&injected);
    for (size_t i = 0; i < list->items.len; i++) {
        CtValue v = list->items.data[i];
        Token tok = {0};
        if (v.kind == CT_TOKEN) {
            tok = v.as.token;
        } else if (v.kind == CT_STR) {
            tok.lexeme = str_dup(v.as.str);
        } else {
            tok.lexeme = ct_string_from_value(v);
        }
        VEC_PUSH(&injected, tok);
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

static void ct_intrinsic_add_token(CompileTimeVM *vm) {
    char *tok = ct_pop_str(vm);
    reader_add_tokens(vm->parser->reader, tok);
    free(tok);
}

static void ct_intrinsic_add_token_chars(CompileTimeVM *vm) {
    char *chars = ct_pop_str(vm);
    reader_add_token_chars(vm->parser->reader, chars);
    free(chars);
}

static void ct_intrinsic_prelude_clear(CompileTimeVM *vm) {
    if (!vm->parser->custom_prelude) {
        vm->parser->custom_prelude = (StrVec *)xmalloc(sizeof(StrVec));
        VEC_INIT(vm->parser->custom_prelude);
    }
    vm->parser->custom_prelude->len = 0;
}

static void ct_intrinsic_prelude_append(CompileTimeVM *vm) {
    char *line = ct_pop_str(vm);
    if (!vm->parser->custom_prelude) {
        vm->parser->custom_prelude = (StrVec *)xmalloc(sizeof(StrVec));
        VEC_INIT(vm->parser->custom_prelude);
    }
    VEC_PUSH(vm->parser->custom_prelude, line);
}

static void ct_intrinsic_bss_clear(CompileTimeVM *vm) {
    if (!vm->parser->custom_bss) {
        vm->parser->custom_bss = (StrVec *)xmalloc(sizeof(StrVec));
        VEC_INIT(vm->parser->custom_bss);
    }
    vm->parser->custom_bss->len = 0;
}

static void ct_intrinsic_bss_append(CompileTimeVM *vm) {
    char *line = ct_pop_str(vm);
    if (!vm->parser->custom_bss) {
        vm->parser->custom_bss = (StrVec *)xmalloc(sizeof(StrVec));
        VEC_INIT(vm->parser->custom_bss);
    }
    VEC_PUSH(vm->parser->custom_bss, line);
}

static void ct_intrinsic_use_l2_ct(CompileTimeVM *vm) {
    char *name = ct_pop_str(vm);
    Word *word = dictionary_lookup(vm->dictionary, name);
    if (!word) {
        word = (Word *)xmalloc(sizeof(Word));
        memset(word, 0, sizeof(Word));
        word->name = str_dup(name);
        dictionary_register(vm->dictionary, word);
    }
    if (word->runtime_only) {
        fprintf(stderr, "[error] word '%s' is runtime-only and cannot be executed at compile time\n", word->name);
        exit(1);
    }
    word->compile_time_override = true;
    free(name);
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

static void ct_intrinsic_shunt(CompileTimeVM *vm) {
    CtList *list = ct_pop_list(vm);
    CtList *output = ct_list_new();
    CtList *ops = ct_list_new();
    for (size_t i = 0; i < list->items.len; i++) {
        CtValue tok = list->items.data[i];
        char *lex = ct_string_from_value(tok);
        if (strcmp(lex, "(") == 0) {
            VEC_PUSH(&ops->items, ct_make_str(lex));
            free(lex);
            continue;
        }
        if (strcmp(lex, ")") == 0) {
            while (ops->items.len) {
                CtValue top = ops->items.data[ops->items.len - 1];
                char *top_lex = ct_string_from_value(top);
                if (strcmp(top_lex, "(") == 0) {
                    ops->items.len--;
                    free(top_lex);
                    break;
                }
                VEC_PUSH(&output->items, top);
                ops->items.len--;
                free(top_lex);
            }
            free(lex);
            continue;
        }
        int prec = 0;
        if (strcmp(lex, "+") == 0 || strcmp(lex, "-") == 0) {
            prec = 1;
        } else if (strcmp(lex, "*") == 0 || strcmp(lex, "/") == 0 || strcmp(lex, "%") == 0) {
            prec = 2;
        }
        if (prec > 0) {
            while (ops->items.len) {
                CtValue top = ops->items.data[ops->items.len - 1];
                char *top_lex = ct_string_from_value(top);
                int top_prec = 0;
                if (strcmp(top_lex, "+") == 0 || strcmp(top_lex, "-") == 0) {
                    top_prec = 1;
                } else if (strcmp(top_lex, "*") == 0 || strcmp(top_lex, "/") == 0 || strcmp(top_lex, "%") == 0) {
                    top_prec = 2;
                }
                if (top_prec >= prec) {
                    VEC_PUSH(&output->items, top);
                    ops->items.len--;
                } else {
                    free(top_lex);
                    break;
                }
                free(top_lex);
            }
            VEC_PUSH(&ops->items, ct_make_str(lex));
            free(lex);
            continue;
        }
        VEC_PUSH(&output->items, ct_make_str(lex));
        free(lex);
    }
    while (ops->items.len) {
        CtValue top = VEC_POP(&ops->items);
        VEC_PUSH(&output->items, top);
    }
    ct_stack_push(&vm->stack, ct_make_list(output));
}

static SplitLexer *split_lexer_new(Parser *parser, const char *seps) {
    SplitLexer *lexer = (SplitLexer *)xmalloc(sizeof(SplitLexer));
    lexer->parser = parser;
    memset(lexer->separators, 0, sizeof(lexer->separators));
    for (const char *p = seps; p && *p; p++) {
        lexer->separators[(unsigned char)*p] = true;
    }
    VEC_INIT(&lexer->buffer);
    return lexer;
}

static void split_lexer_buffer_token(SplitLexer *lexer, Token tok) {
    if (!tok.lexeme) {
        return;
    }
    size_t len = strlen(tok.lexeme);
    if (len == 0 || tok.lexeme[0] == '"') {
        VEC_PUSH(&lexer->buffer, tok);
        return;
    }
    size_t start = 0;
    for (size_t i = 0; i <= len; i++) {
        bool is_sep = (i < len) && lexer->separators[(unsigned char)tok.lexeme[i]];
        bool at_end = (i == len);
        if (is_sep || at_end) {
            if (i > start) {
                size_t tok_len = i - start;
                char *lex = (char *)xmalloc(tok_len + 1);
                memcpy(lex, tok.lexeme + start, tok_len);
                lex[tok_len] = '\0';
                Token out = tok;
                out.lexeme = lex;
                VEC_PUSH(&lexer->buffer, out);
            }
            if (is_sep) {
                char sep[2] = {tok.lexeme[i], '\0'};
                Token out = tok;
                out.lexeme = str_dup(sep);
                VEC_PUSH(&lexer->buffer, out);
            }
            start = i + 1;
        }
    }
}

static Token split_lexer_pop(SplitLexer *lexer) {
    if (lexer->buffer.len == 0) {
        Token tok = parser_next_token(lexer->parser);
        if (!tok.lexeme) {
            Token empty = {0};
            empty.lexeme = NULL;
            return empty;
        }
        split_lexer_buffer_token(lexer, tok);
    }
    if (lexer->buffer.len == 0) {
        Token empty = {0};
        empty.lexeme = NULL;
        return empty;
    }
    Token out = lexer->buffer.data[0];
    memmove(&lexer->buffer.data[0], &lexer->buffer.data[1], (lexer->buffer.len - 1) * sizeof(Token));
    lexer->buffer.len--;
    return out;
}

static Token split_lexer_peek(SplitLexer *lexer) {
    if (lexer->buffer.len == 0) {
        Token tok = parser_next_token(lexer->parser);
        if (!tok.lexeme) {
            Token empty = {0};
            empty.lexeme = NULL;
            return empty;
        }
        split_lexer_buffer_token(lexer, tok);
    }
    if (lexer->buffer.len == 0) {
        Token empty = {0};
        empty.lexeme = NULL;
        return empty;
    }
    return lexer->buffer.data[0];
}

static void split_lexer_push_back(SplitLexer *lexer, Token tok) {
    if (lexer->buffer.len + 1 > lexer->buffer.cap) {
        lexer->buffer.cap = lexer->buffer.cap ? lexer->buffer.cap * 2 : 8;
        lexer->buffer.data = xrealloc(lexer->buffer.data, lexer->buffer.cap * sizeof(Token));
    }
    memmove(&lexer->buffer.data[1], &lexer->buffer.data[0], lexer->buffer.len * sizeof(Token));
    lexer->buffer.data[0] = tok;
    lexer->buffer.len++;
}

static void ct_intrinsic_lexer_new(CompileTimeVM *vm) {
    char *seps = ct_pop_str(vm);
    SplitLexer *lexer = split_lexer_new(vm->parser, seps);
    free(seps);
    ct_stack_push(&vm->stack, ct_make_lexer(lexer));
}

static void ct_intrinsic_lexer_pop(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->stack);
    if (v.kind != CT_LEXER) {
        fprintf(stderr, "[error] lexer-pop expects lexer\n");
        exit(1);
    }
    Token tok = split_lexer_pop(v.as.lexer);
    ct_stack_push(&vm->stack, ct_make_lexer(v.as.lexer));
    if (!tok.lexeme) {
        ct_stack_push(&vm->stack, ct_make_nil());
    } else {
        ct_stack_push(&vm->stack, ct_make_token(tok));
    }
}

static void ct_intrinsic_lexer_peek(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->stack);
    if (v.kind != CT_LEXER) {
        fprintf(stderr, "[error] lexer-peek expects lexer\n");
        exit(1);
    }
    Token tok = split_lexer_peek(v.as.lexer);
    ct_stack_push(&vm->stack, ct_make_lexer(v.as.lexer));
    if (!tok.lexeme) {
        ct_stack_push(&vm->stack, ct_make_nil());
    } else {
        ct_stack_push(&vm->stack, ct_make_token(tok));
    }
}

static void ct_intrinsic_lexer_expect(CompileTimeVM *vm) {
    char *expected = ct_pop_str(vm);
    CtValue v = ct_stack_pop(&vm->stack);
    if (v.kind != CT_LEXER) {
        fprintf(stderr, "[error] lexer-expect expects lexer\n");
        exit(1);
    }
    Token tok = split_lexer_pop(v.as.lexer);
    if (!tok.lexeme || strcmp(tok.lexeme, expected) != 0) {
        fprintf(stderr, "[error] lexer-expect expected '%s'\n", expected);
        exit(1);
    }
    ct_stack_push(&vm->stack, ct_make_lexer(v.as.lexer));
    ct_stack_push(&vm->stack, ct_make_token(tok));
    free(expected);
}

static void ct_intrinsic_lexer_collect_brace(CompileTimeVM *vm) {
    CtValue v = ct_stack_pop(&vm->stack);
    if (v.kind != CT_LEXER) {
        fprintf(stderr, "[error] lexer-collect-brace expects lexer\n");
        exit(1);
    }
    int depth = 1;
    CtList *list = ct_list_new();
    while (depth > 0) {
        Token tok = split_lexer_pop(v.as.lexer);
        if (!tok.lexeme) {
            fprintf(stderr, "[error] unterminated brace in lexer\n");
            exit(1);
        }
        if (strcmp(tok.lexeme, "{") == 0) {
            depth++;
        } else if (strcmp(tok.lexeme, "}") == 0) {
            depth--;
            if (depth == 0) {
                break;
            }
        }
        VEC_PUSH(&list->items, ct_make_token(tok));
    }
    ct_stack_push(&vm->stack, ct_make_lexer(v.as.lexer));
    ct_stack_push(&vm->stack, ct_make_list(list));
}

static void ct_intrinsic_lexer_push_back(CompileTimeVM *vm) {
    Token tok = ct_pop_token(vm);
    CtValue v = ct_stack_pop(&vm->stack);
    if (v.kind != CT_LEXER) {
        fprintf(stderr, "[error] lexer-push-back expects lexer\n");
        exit(1);
    }
    split_lexer_push_back(v.as.lexer, tok);
    ct_stack_push(&vm->stack, ct_make_lexer(v.as.lexer));
}

static void ct_intrinsic_emit_definition(CompileTimeVM *vm) {
    CtList *body = ct_pop_list(vm);
    Token name = ct_pop_token(vm);
    TokenVec injected;
    VEC_INIT(&injected);
    Token tok = {0};
    tok.lexeme = str_dup("word");
    VEC_PUSH(&injected, tok);
    VEC_PUSH(&injected, name);
    for (size_t i = 0; i < body->items.len; i++) {
        CtValue item = body->items.data[i];
        Token t = {0};
        if (item.kind == CT_TOKEN) {
            t = item.as.token;
        } else if (item.kind == CT_STR) {
            t.lexeme = str_dup(item.as.str);
        } else if (item.kind == CT_INT) {
            t.lexeme = str_printf("%lld", (long long)item.as.i64);
        } else {
            t.lexeme = ct_string_from_value(item);
        }
        VEC_PUSH(&injected, t);
    }
    tok.lexeme = str_dup("end");
    VEC_PUSH(&injected, tok);
    parser_inject_tokens(vm->parser, &injected);
}

static void ct_intrinsic_prelude_set(CompileTimeVM *vm) {
    ct_intrinsic_prelude_clear(vm);
    ct_intrinsic_prelude_append(vm);
}

static void ct_intrinsic_bss_set(CompileTimeVM *vm) {
    ct_intrinsic_bss_clear(vm);
    ct_intrinsic_bss_append(vm);
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

static void bootstrap_dictionary(Dictionary *dict, Parser *parser, CompileTimeVM *vm) {
    (void)parser;
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
    register_ct_intrinsic(dict, "string=", ct_intrinsic_string_eq);
    register_ct_intrinsic(dict, "string-length", ct_intrinsic_string_length);
    register_ct_intrinsic(dict, "string-append", ct_intrinsic_string_append);
    register_ct_intrinsic(dict, "string>number", ct_intrinsic_string_to_number);
    register_ct_intrinsic(dict, "int>string", ct_intrinsic_int_to_string);
    register_ct_intrinsic(dict, "identifier?", ct_intrinsic_identifierp);
    register_ct_intrinsic(dict, "list-new", ct_intrinsic_list_new);
    register_ct_intrinsic(dict, "list-append", ct_intrinsic_list_append);
    register_ct_intrinsic(dict, "list-pop", ct_intrinsic_list_pop);
    register_ct_intrinsic(dict, "list-pop-front", ct_intrinsic_list_pop_front);
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
    register_ct_intrinsic(dict, "token-lexeme", ct_intrinsic_token_lexeme);
    register_ct_intrinsic(dict, "token-from-lexeme", ct_intrinsic_token_from_lexeme);
    register_ct_intrinsic(dict, "next-token", ct_intrinsic_next_token);
    register_ct_intrinsic(dict, "peek-token", ct_intrinsic_peek_token);
    register_ct_intrinsic(dict, "inject-tokens", ct_intrinsic_inject_tokens);
    register_ct_intrinsic(dict, "set-token-hook", ct_intrinsic_set_token_hook);
    register_ct_intrinsic(dict, "clear-token-hook", ct_intrinsic_clear_token_hook);
    register_ct_intrinsic(dict, "parse-error", ct_intrinsic_parse_error);
    register_ct_intrinsic(dict, "add-token", ct_intrinsic_add_token);
    register_ct_intrinsic(dict, "add-token-chars", ct_intrinsic_add_token_chars);
    register_ct_intrinsic(dict, "prelude-clear", ct_intrinsic_prelude_clear);
    register_ct_intrinsic(dict, "prelude-append", ct_intrinsic_prelude_append);
    register_ct_intrinsic(dict, "prelude-set", ct_intrinsic_prelude_set);
    register_ct_intrinsic(dict, "bss-clear", ct_intrinsic_bss_clear);
    register_ct_intrinsic(dict, "bss-append", ct_intrinsic_bss_append);
    register_ct_intrinsic(dict, "bss-set", ct_intrinsic_bss_set);
    register_ct_intrinsic(dict, "use-l2-ct", ct_intrinsic_use_l2_ct);
    register_ct_intrinsic(dict, "shunt", ct_intrinsic_shunt);
    register_ct_intrinsic(dict, "emit-definition", ct_intrinsic_emit_definition);
    register_ct_intrinsic(dict, "lexer-new", ct_intrinsic_lexer_new);
    register_ct_intrinsic(dict, "lexer-pop", ct_intrinsic_lexer_pop);
    register_ct_intrinsic(dict, "lexer-peek", ct_intrinsic_lexer_peek);
    register_ct_intrinsic(dict, "lexer-expect", ct_intrinsic_lexer_expect);
    register_ct_intrinsic(dict, "lexer-collect-brace", ct_intrinsic_lexer_collect_brace);
    register_ct_intrinsic(dict, "lexer-push-back", ct_intrinsic_lexer_push_back);

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
    size_t total = 0;
    for (size_t i = 0; i < bytes.len; i++) {
        total += strlen(bytes.data[i]) + 2;
    }
    char *line = (char *)xmalloc(total + strlen(label) + 6);
    strcpy(line, label);
    strcat(line, ": db ");
    for (size_t i = 0; i < bytes.len; i++) {
        strcat(line, bytes.data[i]);
        if (i + 1 < bytes.len) {
            strcat(line, ", ");
        }
    }
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

    if (parser->custom_prelude) {
        for (size_t i = 0; i < parser->custom_prelude->len; i++) {
            VEC_PUSH(&emission.text, str_dup(parser->custom_prelude->data[i]));
        }
    } else if (parser->uses_libc) {
        emit_libc_prelude(&emission);
    } else {
        emit_default_prelude(&emission);
    }

    VEC_PUSH(&emission.data, str_dup("sys_argc: dq 0"));
    VEC_PUSH(&emission.data, str_dup("sys_argv: dq 0"));

    if (parser->custom_bss) {
        for (size_t i = 0; i < parser->custom_bss->len; i++) {
            VEC_PUSH(&emission.bss, str_dup(parser->custom_bss->data[i]));
        }
    } else {
        emit_default_bss(&emission);
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
    size_t total = 0;
    for (size_t i = 0; i < parts.len; i++) {
        if (parts.data[i]) {
            total += strlen(parts.data[i]) + 1;
        }
    }
    char *buf = (char *)xmalloc(total + 1);
    buf[0] = '\0';
    for (size_t i = 0; i < parts.len; i++) {
        strcat(buf, parts.data[i]);
        strcat(buf, "\n");
    }
    return buf;
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
    size_t total = 0;
    for (size_t i = 0; i < parts.len; i++) {
        total += strlen(parts.data[i]);
    }
    char *out = (char *)xmalloc(total + 1);
    out[0] = '\0';
    for (size_t i = 0; i < parts.len; i++) {
        strcat(out, parts.data[i]);
    }
    free(content);
    free(base_dir);
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

static void parse_tokens(Parser *parser, const char *source) {
    parser->source = str_dup(source);
    tokenizer_init(&parser->tokenizer, parser->reader, source);
    parser->tokenizer_exhausted = false;
    parser->pos = 0;
    parser->current_def = NULL;
    parser->control_len = 0;
    parser->label_counter = 0;
    parser->token_hook = NULL;
    parser->has_last_token = false;
    parser->custom_prelude = NULL;
    parser->custom_bss = NULL;
    parser->pending_inline_def = false;

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
            parser->pending_inline_def = false;
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
            while (!parser_eof(parser)) {
                Token next = parser_next_token(parser);
                if (next.lexeme && strcmp(next.lexeme, "}") == 0) {
                    body_end = (size_t)next.start;
                    break;
                }
            }
            if (body_end <= body_start) {
                fprintf(stderr, "[error] missing '}' to terminate asm body\n");
                exit(1);
            }
            size_t body_len = body_end - body_start;
            char *body = (char *)xmalloc(body_len + 1);
            memcpy(body, parser->source + body_start, body_len);
            body[body_len] = '\0';
            AsmDefinition *def = (AsmDefinition *)xmalloc(sizeof(AsmDefinition));
            memset(def, 0, sizeof(AsmDefinition));
            def->name = str_dup(name_tok.lexeme);
            def->body = body;
            def->effect_string_io = effect_string_io;
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
            Token tok2 = parser_next_token(parser);
            Token tok3 = parser_next_token(parser);
            if (tok2.lexeme && tok3.lexeme && strcmp(tok3.lexeme, "(") == 0) {
                Word *word = dictionary_lookup(parser->dictionary, tok2.lexeme);
                if (!word) {
                    word = (Word *)xmalloc(sizeof(Word));
                    memset(word, 0, sizeof(Word));
                    word->name = str_dup(tok2.lexeme);
                    dictionary_register(parser->dictionary, word);
                }
                word->is_extern = true;
                word->extern_ret_type = str_dup(tok1.lexeme);
                parser->uses_libc = true;
                if (strcmp(tok1.lexeme, "double") == 0 || strcmp(tok1.lexeme, "float") == 0) {
                    if (strcmp(tok2.lexeme, "printf") != 0) {
                        parser->uses_libm = true;
                    }
                }
                word->extern_arg_types = NULL;
                word->extern_arg_count = 0;
                int cap = 0;
                Token arg = parser_peek_token(parser);
                if (arg.lexeme && strcmp(arg.lexeme, ")") == 0) {
                    parser_next_token(parser);
                } else {
                    while (true) {
                        Token type_tok = parser_next_token(parser);
                        if (!type_tok.lexeme) {
                            fprintf(stderr, "[error] unterminated extern signature\n");
                            exit(1);
                        }
                        if (word->extern_arg_count + 1 > cap) {
                            cap = cap ? cap * 2 : 4;
                            word->extern_arg_types = xrealloc(word->extern_arg_types, (size_t)cap * sizeof(char *));
                        }
                        word->extern_arg_types[word->extern_arg_count++] = str_dup(type_tok.lexeme);
                        if (strcmp(type_tok.lexeme, "double") == 0 || strcmp(type_tok.lexeme, "float") == 0) {
                            if (strcmp(tok2.lexeme, "printf") != 0) {
                                parser->uses_libm = true;
                            }
                        }
                        Token maybe_name = parser_peek_token(parser);
                        if (maybe_name.lexeme && strcmp(maybe_name.lexeme, ",") != 0 && strcmp(maybe_name.lexeme, ")") != 0) {
                            parser_next_token(parser);
                        }
                        Token sep = parser_next_token(parser);
                        if (!sep.lexeme) {
                            fprintf(stderr, "[error] unterminated extern signature\n");
                            exit(1);
                        }
                        if (strcmp(sep.lexeme, ")") == 0) {
                            break;
                        }
                        if (strcmp(sep.lexeme, ",") != 0) {
                            fprintf(stderr, "[error] expected ',' or ')' in extern signature\n");
                            exit(1);
                        }
                    }
                }
                continue;
            }
            TokenVec reinject;
            VEC_INIT(&reinject);
            if (tok2.lexeme) {
                VEC_PUSH(&reinject, tok2);
            }
            if (tok3.lexeme) {
                VEC_PUSH(&reinject, tok3);
            }
            parser_inject_tokens(parser, &reinject);
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
                    parser_inject_tokens(parser, &cond_tokens);
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


static void parser_expand_macro(Parser *parser, Word *word) {
    int param_count = word->macro_param_count;
    char **params = NULL;
    if (param_count > 0) {
        params = (char **)xmalloc((size_t)param_count * sizeof(char *));
        for (int i = 0; i < param_count; i++) {
            Token tok = parser_next_token(parser);
            if (!tok.lexeme) {
                fprintf(stderr, "[error] not enough macro parameters for '%s'\n", word->name);
                exit(1);
            }
            params[i] = str_dup(tok.lexeme);
        }
    }
    TokenVec injected;
    VEC_INIT(&injected);
    for (size_t i = 0; word->macro_expansion && word->macro_expansion[i]; i++) {
        const char *item = word->macro_expansion[i];
        if (item && item[0] == '$' && isdigit((unsigned char)item[1])) {
            int idx = atoi(item + 1) - 1;
            if (idx >= 0 && idx < param_count) {
                Token tok = {0};
                tok.lexeme = str_dup(params[idx]);
                VEC_PUSH(&injected, tok);
                continue;
            }
        }
        Token tok = {0};
        tok.lexeme = str_dup(item);
        VEC_PUSH(&injected, tok);
    }
    parser_inject_tokens(parser, &injected);
    for (int i = 0; i < param_count; i++) {
        free(params[i]);
    }
    free(params);
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

    if (strcmp(token.lexeme, "macro") == 0) {
        Token name = parser_next_token(parser);
        if (!name.lexeme) {
            fprintf(stderr, "[error] macro missing name\n");
            exit(1);
        }
        int param_count = 0;
        Token maybe_num = parser_peek_token(parser);
        if (maybe_num.lexeme && isdigit((unsigned char)maybe_num.lexeme[0])) {
            parser_next_token(parser);
            param_count = atoi(maybe_num.lexeme);
        }
        parser_start_macro(parser, name.lexeme, param_count);
        return;
    }

    if (strcmp(token.lexeme, "inline") == 0) {
        parser->pending_inline_def = true;
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
    if (word && word->macro_expansion) {
        parser_expand_macro(parser, word);
        return;
    }
    if (word && word->immediate) {
        ct_word_call(parser->ct_vm, word);
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
        fprintf(stderr, "usage: %s <source.sl> [-o output] [--emit-asm]\n", argv[0]);
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
    size_t total = 0;
    for (size_t i = 0; i < sources.len; i++) {
        total += strlen(sources.data[i]);
    }
    char *combined = (char *)xmalloc(total + 1);
    combined[0] = '\0';
    for (size_t i = 0; i < sources.len; i++) {
        strcat(combined, sources.data[i]);
    }

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

int l2_eval(const char *source, long source_len) {
    int result = -1;
    char *owned_source = NULL;
    char *work_dir = NULL;
    char *source_path = NULL;
    char *output_path = NULL;
    char *temp_dir = NULL;
    char *root_dir = NULL;
    char *stdlib_dir = NULL;

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

    work_dir = make_eval_workdir();
    if (!work_dir) {
        goto cleanup;
    }

    source_path = str_printf("%s/input.sl", work_dir);
    output_path = str_printf("%s/input.out", work_dir);
    temp_dir = str_printf("%s/build", work_dir);
    root_dir = l2_default_root_dir();
    stdlib_dir = path_join(root_dir, "stdlib");
    write_file(source_path, owned_source);
    free(owned_source);
    owned_source = NULL;

    char *compile_argv[] = {
        "l2-eval",
        "-I",
        root_dir,
        "-I",
        stdlib_dir,
        source_path,
        "-o",
        output_path,
        "--temp-dir",
        temp_dir,
        NULL,
    };
    int compile_status = run_l2_cli_in_child(10, compile_argv);
    if (compile_status != 0) {
        result = -compile_status;
        goto cleanup;
    }

    char *run_argv[] = {
        output_path,
        NULL,
    };
    result = run_cmd_status(run_argv);

cleanup:
    if (owned_source) {
        free(owned_source);
    }
    if (work_dir) {
        cleanup_eval_workdir(work_dir);
    }
    free(source_path);
    free(output_path);
    free(temp_dir);
    free(root_dir);
    free(stdlib_dir);
    free(work_dir);
    return result;
}

int l2_eval_cstr(const char *source) {
    if (!source) {
        return -1;
    }
    return l2_eval(source, (long)strlen(source));
}

#ifndef L2_AS_LIBRARY
int main(int argc, char **argv) {
    return l2_cli(argc, argv);
}
#endif

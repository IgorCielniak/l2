# L2 Language Specification (January 2026)

This document reflects the implementation that ships in this repository today (`main.py`, `stdlib`, and `tests`). It replaces the previous aspirational draft with the behavior exercised by the compiler, runtime, and automated samples.

## 1. Scope and Principles
- **Stack-based core** – All user code manipulates a 64-bit data stack plus a separate return stack. Every definition is a “word.”
- **Ahead-of-time native output** – `main.py` always emits NASM-compatible x86-64 assembly, assembles it with `nasm -f elf64`, and links it with `ld`/`ld.lld` into an ELF64 executable. There is no JIT; the REPL repeatedly rebuilds and executes small binaries.
- **Meta-programmable front-end** – Parsing, macro expansion, and syntax sugar live in user space via immediate words, text macros, compile-time intrinsics, and `:py` blocks. Users can reshape syntax without touching the Python host.
- **Unsafe by design** – Memory, syscalls, inline assembly, and FFI expose raw machine power. The standard library is intentionally thin and policy-free.

## 2. Toolchain and Repository Layout
- **Driver (`main.py`)** – Supports `python main.py source.sl -o a.out`, `--emit-asm`, `--run`, `--dbg`, `--repl`, `--temp-dir`, `--clean`, repeated `-I/--include` paths, and repeated `-l` linker flags (either `-lfoo` or `-l libc.so.6`). Unknown `-l` flags are collected and forwarded to the linker. Pass `--ct-run-main` to run the program's `main` word on the compile-time VM before NASM/ld run, which surfaces discrepancies between compile-time and runtime semantics. Pass `--no-artifact` to stop after compilation/assembly emission without building an output file, or use `--script` as shorthand for `--no-artifact --ct-run-main`. Pass `--docs` to open a searchable TUI that scans stack-effect comments and nearby docs from `.sl` files (`--docs-query` sets initial filter and `--docs-root` adds scan roots). `--no-folding` disables constant folding and `--no-peephole` disables peephole rewrites (for example `swap drop` → `nip`, `dup drop` removed, `swap over` → `tuck`, `nip drop` → `2drop`, `x 0 +` removed, `x 1 *` removed, `x -1 *` → `neg`, and `not not` removed).
- **REPL** – `--repl` launches a stateful session with commands such as `:help`, `:reset`, `:load`, `:call <word>`, `:edit`, and `:show`. The REPL still emits/links entire programs for each run; it simply manages the session source for you.
- **Imports** – `import relative/or/absolute/path.sl` inserts the referenced file textually. Resolution order: (1) absolute path, (2) relative to the importing file, (3) each include path (defaults: project root and `./stdlib`). Each file is included at most once per compilation unit. Import lines leave blank placeholders so error spans stay meaningful.
- **Workspace** – `stdlib/` holds library modules, `tests/` contains executable samples with `.expected` outputs, `extra_tests/` houses standalone integration demos, and `libs/` collects opt-in extensions such as `libs/fn.sl` and `libs/nob.sl`.

## 3. Lexical Structure
- **Reader** – Whitespace-delimited; `#` starts a line comment. String literals honor `\"`, `\\`, `\n`, `\r`, `\t`, and `\0`. Numbers default to signed 64-bit integers via `int(token, 0)` (so `0x`, `0o`, `0b` all work). Tokens containing `.` or `e` parse as floats.
- **Identifiers** – `[A-Za-z_][A-Za-z0-9_]*`. Everything else is treated as punctuation or literal.
- **String representation** – At runtime each literal pushes `(addr len)` with the length on top. The assembler stores literals in `section .data` with a trailing `NULL` for convenience.
- **Lists** – `[` begins a list literal, `]` ends it. The compiler captures the intervening stack segment into a freshly `mmap`'d buffer that stores `(len followed by qword items)`, drops the captured values, and pushes the buffer address. Users must `munmap` the buffer when done.
- **Token customization** – Immediate words can call `add-token` or `add-token-chars` to teach the reader about new multi-character tokens. `libs/fn.sl` uses this in combination with token hooks to recognize `foo(1, 2)` syntax.

### Stack-effect comments
- **Location and prefix** – Public words in `stdlib/` (and most user code should) document its stack effect with a line comment directly above the definition: `#word_name …`.
- **Before/after form** – Use `[before] -> [after]`, where each side is a comma-separated list. Items sitting to the left of `|` are deeper in the stack; the segment to the right of `|` runs all the way to the current top-of-stack. Omit the `|` only when a side is empty (`[*]`).
- **Tail sentinel** – `*` represents the untouched rest of the stack. By convention it is always the first entry on each side so readers can quickly see which values are consumed/produced.
- **Alternatives** – Separate multiple outcomes with `||`. Each branch repeats the `[before] -> [after]` structure (e.g., `#read_file [*, path | len] -> [*, addr | len] || [*, tag | neg_errno]`).
- **Examples** – `#dup [* | x] -> [*, x | x]` means a word consumes the top value `x` and returns two copies with the newest copy at TOS; `#arr_pop [* | arr] -> [*, arr | x]` states that the array pointer remains just below the popped element. This notation keeps stack order resonably easy to read and grep.

## 4. Runtime Model
- **Stacks** – `r12` holds the data stack pointer, `r13` the return stack pointer. Both live in `.bss` buffers sized by `DSTK_BYTES`/`RSTK_BYTES` (default 64 KiB each). `stdlib/core.sl` implements all standard stack shuffles, arithmetic, comparisons, boolean ops, `@`/`!`, `c@`/`c!`, and return-stack transfers (`>r`, `r>`, `rdrop`, `rpick`).
- **Calling convention** – Words call each other using the System V ABI. `extern` words marshal arguments into registers before `call symbol`, then push results back onto the data stack. Integer results come from `rax`; floating results come from `xmm0` and are copied into a qword slot.
- **Memory helpers** – `mem` returns the address of the `persistent` buffer (default 64 bytes). `argc`, `argv`, and `argv@` expose process arguments. `alloc`/`free` wrap `mmap`/`munmap` for general-purpose buffers, while `memcpy` performs byte-wise copies.
- **BSS customization** – Compile-time words may call `bss-clear` followed by `bss-append`/`bss-set` to replace the default `.bss` layout (e.g., `tests/bss_override.sl` enlarges `persistent`).
- **Strings & buffers** – IO helpers consume explicit `(addr len)` pairs only; there is no implicit NULL contract except for stored literals.
- **Structured data** – `struct` blocks expand into constants and accessor words (`Foo.bar@`, `Foo.bar!`). Dynamic arrays in `stdlib/arr.sl` allocate `[len, cap, data_ptr, data...]` records via `mmap` and expose `arr_new`, `arr_len`, `arr_cap`, `arr_data`, `arr_push`, `arr_pop`, `arr_reserve`, `arr_free`.

## 5. Definitions, Control Flow, and Syntax Sugar
- **Word definitions** – Always `word name ... end`. Redefinitions overwrite the previous entry (a warning prints to stderr). `inline word name ... end` marks the definition for inline expansion; recursive inline calls are rejected. `immediate` and `compile-only` apply to the most recently defined word.
- **Priority-based redefinition** – Use `priority <int>` before `word`, `:asm`, `:py`, or `extern` to control conflicts for the same name. Higher priority wins; lower-priority definitions are ignored. Equal priority keeps last definition (with a redefinition warning). The compiler prints a note indicating which priority was selected.
- **Control forms** – Built-in tokens drive code emission:
  - `if ... end` and `if ... else ... end`. To express additional branches, place `if` on the same line as the preceding `else` (e.g., `else <condition> if ...`); the reader treats that form as an implicit chained clause, so each inline `if` consumes one flag and jumps past later clauses on success.
  - `while <condition> do <body> end`; the conditional block lives between `while` and `do` and re-runs every iteration.
  - `n for ... end`; the loop count is popped, stored on the return stack, and decremented each pass. The compile-time word `i` exposes the loop index inside macros.
  - `label name` / `goto name` perform local jumps within a definition.
  - `&name` pushes a pointer to word `name` (its callable code label). This is intended for indirect control flow; `&name jmp` performs a tail jump to that word and is compatible with `--ct-run-main`.
- **Text macros** – `macro name [param_count] ... ;` records raw tokens until `;`. `$0`, `$1`, ... expand to positional arguments. Macro definitions cannot nest (attempting to start another `macro` while recording raises a parse error).
- **Struct builder** – `struct Foo ... end` emits `<Foo>.size`, `<Foo>.field.size`, `<Foo>.field.offset`, `<Foo>.field@`, and `<Foo>.field!` helpers. Layout is tightly packed with no implicit padding.
- **With-blocks** – `with a b in ... end` rewrites occurrences of `a`/`b` into accesses against hidden global cells (`__with_a`). On entry the block pops the named values and stores them in those cells; reads compile to `@`, writes to `!`. Because the cells live in `.data`, the slots persist across calls and are not re-entrant.
- **List literals** – `[ values ... ]` capture the current stack slice, allocate storage (`mmap`), copy the elements, and push the pointer. The record stores `len` at offset 0 and items afterwards so user code can fetch length via `@` and iterate.
- **Compile-time execution** – `compile-time foo` runs `foo` immediately but still emits it (if inside a definition). Immediate words always execute during parsing; ordinary words emit `word` ops for later code generation.

## 6. Compile-Time Facilities
- **Virtual machine** – Immediate words run inside `CompileTimeVM`, which keeps its own stacks and exposes helpers registered in `bootstrap_dictionary()`:
  - Lists/maps: `list-new`, `list-append`, `list-pop`, `list-pop-front`, `list-length`, `list-empty?`, `list-get`, `list-set`, `list-extend`, `list-last`, `map-new`, `map-set`, `map-get`, `map-has?`.
  - Strings/numbers: `string=`, `string-length`, `string-append`, `string>number`, `int>string`.
  - Lexer utilities: `lexer-new`, `lexer-pop`, `lexer-peek`, `lexer-expect`, `lexer-collect-brace`, `lexer-push-back` (used by `libs/fn.sl` to parse signatures and infix expressions).
  - Token management: `next-token`, `peek-token`, `inject-tokens`, `token-lexeme`, `token-from-lexeme`.
  - Reader hooks: `set-token-hook` installs a word that receives each token (pushed as a `Token` object) and must leave a truthy handled flag; `clear-token-hook` disables it. `libs/fn.sl`'s `extend-syntax` demonstrates rewriting `foo(1, 2)` into ordinary word calls.
  - Prelude/BSS control: `prelude-clear`, `prelude-append`, `prelude-set`, `bss-clear`, `bss-append`, `bss-set` let user code override the `_start` stub or `.bss` layout.
  - Definition helpers: `emit-definition` injects a `word ... end` definition on the fly (used by the struct macro). `parse-error` raises a custom diagnostic.
- **Text macros** – `macro` is an immediate word implemented in Python; it prevents nesting by tracking active recordings and registers expansion tokens with `$n` substitution.
- **Python bridges** – `:py name { ... } ;` executes once during parsing. The body may define `macro(ctx: MacroContext)` (with helpers such as `next_token`, `emit_literal`, `inject_tokens`, `new_label`, and direct `parser` access) and/or `intrinsic(builder: FunctionEmitter)` to emit assembly directly. The `fn` DSL (`libs/fn.sl`) and other syntax layers are ordinary `:py` blocks.

## 7. Foreign Code, Inline Assembly, and Syscalls
- **`:asm name { ... } ;`** – Defines a word entirely in NASM syntax. The body is copied verbatim into the output and terminated with `ret`. If `keystone-engine` is installed, `:asm` words also execute at compile time; the VM marshals `(addr len)` string pairs by scanning for `data_start`/`data_end` references.
- **`:py` intrinsics** – As above, `intrinsic(builder)` can emit custom assembly without going through the normal AST.
- **`extern`** – Two forms:
  - Raw: `extern foo 2 1` marks `foo` as taking two stack arguments and returning one value. The emitter simply emits `call foo`.
  - C-style: `extern double atan2(double y, double x)` parses the signature, loads integer arguments into `rdi..r9`, floating arguments into `xmm0..xmm7`, aligns `rsp`, sets `al` to the number of SSE arguments, and pushes the result from `xmm0` or `rax`. Only System V register slots are supported.
- **Syscalls** – The built-in word `syscall` expects `(argN ... arg0 count nr -- ret)`. It clamps the count to `[0, 6]`, loads arguments into `rdi`, `rsi`, `rdx`, `r10`, `r8`, `r9`, executes `syscall`, and pushes `rax`. `stdlib/linux.sl` auto-generates macros of the form `syscall.write` → `3 1` plus `.num`/`.argc` helpers, and provides assembly-only `syscall1`–`syscall6` macros so the module works without the rest of the stdlib. `tests/syscall_write.sl` demonstrates the intended usage.

## 8. Standard Library Overview (`stdlib/`)
- **`core.sl`** – Stack shuffles, integer arithmetic, comparisons, boolean ops, memory access, syscall stubs (`mmap`, `munmap`, `exit`), argument helpers (`argc`, `argv`, `argv@`), and pointer helpers (`mem`).
- **`mem.sl`** – `alloc`/`free` wrappers around `mmap`/`munmap` plus a byte-wise `memcpy` used by higher-level utilities.
- **`io.sl`** – `read_file`, `write_file`, `read_stdin`, `write_buf`, `ewrite_buf`, `putc`, `puti`, `puts`, `eputs`, and a smart `print` that detects `(addr,len)` pairs located inside the default `.data` region.
- **`utils.sl`** – String and number helpers (`strcmp`, `strconcat`, `strlen`, `digitsN>num`, `toint`, `count_digits`, `tostr`).
- **`arr.sl`** – Dynamically sized qword arrays with `arr_new`, `arr_len`, `arr_cap`, `arr_data`, `arr_push`, `arr_pop`, `arr_reserve`, `arr_free`.
- **`float.sl`** – SSE-based double-precision arithmetic (`f+`, `f-`, `f*`, `f/`, `fneg`, comparisons, `int>float`, `float>int`, `fput`, `fputln`).
- **`linux.sl`** – Auto-generated syscall macros (one constant block per entry in `syscall_64.tbl`) plus the `syscallN` helpers implemented purely in assembly so the file can be used in isolation.
- **`debug.sl`** – Diagnostics such as `dump`, `rdump`, and `int3`.
- **`stdlib.sl`** – Convenience aggregator that imports `core`, `mem`, `io`, and `utils` so most programs can simply `import stdlib/stdlib.sl`.

## 9. Testing and Usage Patterns
- **Automated coverage** – `python test.py` compiles every `tests/*.sl`, runs the generated binary, and compares stdout against `<name>.expected`. Optional companions include `<name>.stdin` (piped to the process), `<name>.args` (extra CLI args parsed with `shlex`), `<name>.stderr` (expected stderr), and `<name>.meta.json` (per-test knobs such as `expected_exit`, `expect_compile_error`, or `env`). The `extra_tests/` folder ships with curated demos (`extra_tests/ct_test.sl`, `extra_tests/args.sl`, `extra_tests/c_extern.sl`, `extra_tests/fn_test.sl`, `extra_tests/nob_test.sl`) that run alongside the core suite; pass `--extra path/to/foo.sl` to cover more standalone files. Use `python test.py --list` to see descriptions and `python test.py --update foo` to bless outputs after intentional changes. Add `--ct-run-main` when invoking the harness to run each test's `main` at compile time as well; capture that stream with `<name>.compile.expected` if you want automated comparisons.
- **Common commands** –
  - `python test.py` (run the whole suite)
  - `python test.py hello --update` (re-bless a single test)
  - `python test.py --ct-run-main hello` (compile/run a single test while also exercising `main` on the compile-time VM)
  - `python main.py tests/hello.sl -o build/hello && ./build/hello`
  - `python main.py program.sl --emit-asm --temp-dir build`
  - `python main.py --repl`

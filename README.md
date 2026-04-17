# L2

**Give the programmer raw power and get out of the way.**

L2 is a programmable assembly templating engine with a Forth-style stack interface. You write small 'words' that compose into larger programs, and each word compiles to a known, inspectable sequence of x86-64 instructions. The language sits just above raw assembly — close enough to see every byte, high enough to be genuinely productive.

## What is L2?

At its core, L2 is more than a glorified macro assembler. Its compile-time virtual machine lets you run arbitrary L2 code at compile time: generate words, compute lookup tables, build structs, or emit entire subsystems before a single byte of native code is produced. Text macros, `:py` blocks, and token hooks extend the syntax in ways that feel like language features — because they are.

## Quick Start

### Prerequisites

- Python 3.7+
- NASM (Netwide Assembler)
- GNU binutils (`ld`)
- Linux x86-64
- `keystone-engine` (optional, for compile-time `:asm` execution)

### Building

```bash
python3 main.py examples/snake.sl -o snake
./snake
```

### Tests

```bash
python3 test.py
```

### Macro Expansion Profiling

Use `--macro-profile` to inspect macro expansion hotness and timing during parsing:

```bash
python3 main.py tests/macro_ct_superlang.sl --no-artifact --check --macro-profile
```

Optional output targets:

- `--macro-profile` or `--macro-profile stderr`: print to stderr.
- `--macro-profile -`: print to stdout.
- `--macro-profile build/macro_profile.txt`: write a report file.

For transformed-source inspection, use `--preview`:

```bash
python3 main.py tests/macro_ct_superlang.sl --no-artifact --preview
```

`--preview` prints the post-parse transformed source after macro expansion and compile-time execution.

### Complete CT Function Reference

The built-in Compile-Time Reference now includes an auto-generated
`§ 18 COMPLETE CT FUNCTION INDEX` section with one explicit entry for every
compile-time callable word (including handler name and execution flags), plus
template directives such as `ct-call`, `ct-if`, `ct-for`, `emit-list`, and
their aliases.

Open it with:

```bash
python3 main.py --docs
```

Or launch the browser docs UI (static, with tab links, search, and detail/source panes):

```bash
python3 main.py --docs-serve
```

Useful options:

- `--docs-port 8018`
- `--docs-host 0.0.0.0`
- `--docs-no-browser`

Then switch to the `Compile-Time Reference` tab (or open `/?tab=ct`).

### Cache Modes (`--no-cache` vs `--force`)

The compiler now has three cache-related layers:

- Source graph cache: stores the preprocessed import graph (including resolved imports and source-level flags) keyed by source path, defines, include paths, and dependency state.
- Assembly cache: stores emitted assembly snapshots keyed by dependency content and compiler/optimization flags.
- Tool incrementality: NASM/linker reruns are skipped when inputs are unchanged.

Flag behavior:

- Default mode: all cache layers enabled.
- `--no-cache`: disables source/assembly cache reads+writes, but still allows NASM/linker up-to-date checks.
- `--force`: implies `--no-cache` and always recompiles, re-assembles, and re-links.

In short:

- `--no-cache` means "no compiler cache".
- `--force` means "rebuild everything now".

## Metaprogramming Guide

L2 has two complementary metaprogramming layers:

1. Text macros (`macro ... ;`) for syntax-level expansion.
2. Compile-time VM words (`compile-time`, `ct-*`) for programmable parser/compiler control.

### Why this is useful in practice

- Build mini-DSLs without changing compiler source.
- Generate repetitive code while keeping final assembly explicit.
- Add project-local syntax sugar that compiles away completely.
- Control parser behavior for advanced transforms (token hooks and rewrites).

### Text Macros: Legacy and Advanced Forms

Legacy positional form still works:

```l2
macro twice 1
	$0 $0 +
;
```

Named parameters improve readability:

```l2
macro add2 (lhs rhs)
	$lhs $rhs +
;
```

Variadic parameters capture a tail of arguments:

```l2
macro emit_all (head *tail)
	$head $*tail
;
```

Parameter/placeholder rules:

- `$0`, `$1`, ...: positional placeholders (legacy-compatible).
- `$name`: named placeholder.
- `$*name`: splice variadic capture.
- Placeholder transforms: `$name|upper`, `$name|lower`, `$*name|join:","`.
- Variadic parameter must be last in the signature.

### Macro Template Control Flow

Text macros can use compile-time control flow directly in the macro body:

```l2
macro join_with_plus (head *tail)
	$head
	ct-for item in tail do
		$item +
	end
;

macro maybe_bump (x *rest)
	$x
	ct-if has rest then
		1 +
	else
		0 +
	end
;
```

Template-only control keywords:

- `ct-if <cond> then ... else ... end`
- `ct-when <cond> ... end` (single-branch shorthand)
- `ct-unless <cond> ... end` (negated single-branch shorthand)
- `ct-for <name> in <capture> do ... end`
- `ct-each <name> in <capture> do ... end`
- `ct-each <index> <name> in <capture> do ... end` (key/value mode)
- `ct-for <name> in <capture> sep <template...> do ... end`
- `ct-each <...> in <capture> sep <template...> do ... end`
- `ct-let <name> <expr...> do ... end`
- `ct-let <name> = <expr...> do ... end` (optional `=`)
- `ct-fn <name> do ... end` (template-local function)
- `ct-switch <expr...> do ... end`
- `ct-case <expr...> do ... end`
- `ct-default do ... end`
- `ct-match <expr...> do ... end` (expression-style selector)
- `ct-case <expr...> then <template...>` (inside `ct-match`)
- `ct-default then <template...>` (inside `ct-match`)
- `ct-fold <acc> <item> in <capture> with <init...> do ... end`
- `ct-break` / `ct-continue` (inside `ct-for` / `ct-each` / `ct-fold`)
- `ct-call <compile-time-word>`
- `ct-include <path-token>` / `ct-import <path-token>`
- `ct-comment ... ct-endcomment` (nestable template comments)
- `ct-strict` / `ct-permissive` (unknown-symbol handling)
- `ct-version <token>` (per-macro template metadata marker)
- `ct-error <msg>` / `ct-warning <msg>` / `ct-note <msg>`
- `emit-list <capture>` / `ct-emit-list <capture>`
- `emit-block do ... end` / `ct-emit-block do ... end`

Template parser behavior:

- `ct-if` / `ct-when` / `ct-unless` / `ct-for` / `ct-each` are compile-time template directives.
- `ct-let` creates a local template binding for the block body and supports lexical shadowing.
- `ct-fn` defines a template-local function callable with `ct-call`.
- `ct-switch` compares expanded token sequences and executes the first matching `ct-case`; `ct-default` is optional.
- `ct-match` is the compact expression-style selector; case bodies run until the next `ct-case`, `ct-default`, or closing `end`.
- `ct-fold` evaluates to the final accumulator value; each loop body result becomes the next accumulator value.
- `ct-include` splices template nodes from a file (relative to the defining source file).
- `ct-import` loads template helpers (for example `ct-fn`) once per expansion scope and avoids duplicate imports.
- `ct-comment ... ct-endcomment` and `ct-#( ... ct-#)` are treated as template-only comments and do not emit runtime tokens.
- `ct-strict` errors on unknown template symbols; `ct-permissive` treats unknown symbols as empty and emits a warning.
- `ct-version` stores a macro-local version marker for tooling/introspection.
- `emit-list`/`ct-emit-list` splices a capture value; `emit-block`/`ct-emit-block` emits an inline template block.
- Placeholder transforms apply both in emitted placeholders and template guard expressions.
- Plain runtime control tokens (`if`, `for`, `else`, `end`) are treated as ordinary output tokens.
- Nested runtime blocks inside `ct-if` / `ct-for` are tracked so their `end` tokens do not accidentally close compile-time directives.

Guard expressions support:

- unary: `not`, `!`
- binary boolean: `and`, `&&`, `or`, `||`
- comparisons: `==`, `!=`, `<`, `<=`, `>`, `>=`
- grouping with `( ... )`
- capture/local refs: `$x`, `name`
- loop predicates: `first`, `last`
- capture predicates: `has <capture>`, `empty <capture>`

`ct-if` always uses expression parsing. `ct-when` / `ct-unless` keep their shorthand
form (`has ...`, `empty ...`, etc.) and also accept expression guards when written
with `then`.

Constant-only guard subexpressions are folded during template parse.

`ct-call` bridges text macros with compile-time words. The target word receives
a context map on the CT stack:

- `"macro"`: macro name
- `"captures"`: merged capture map for current scope
- `"loop"`: loop metadata map (`index`, `count`, `first`, `last`) or `nil`

Return value from the CT word is spliced back into the macro output. It can be
`nil`, a token/string/number, or nested lists/tuples of token-like values.

These execute only during macro expansion and emit ordinary L2 tokens, so there is no runtime overhead.

### Capture Toolkit

The compile-time API now includes a capture/scope/hygiene toolkit for advanced macro pipelines:

- Hygienic symbols: `ct-gensym`.
- Capture namespaces in `ct-call` contexts: `args`, `locals`, `globals`.
- Capture operators: get/has/shape/assert/count/slice/map/filter/separate/join/equal.
- Typed placeholder constraints in text macro expansions (e.g. `$x:int`).
- Capture normalization/pretty/clone/coercion helpers.
- Capture schema declaration + validation (`ct-capture-schema-put`, `ct-capture-schema-validate`).
- Lifetime/origin/taint metadata and checks for scope-aware metaprogramming.
- Serialization/compression/hash/diff helpers for memoization and debugging.
- Replay logs and lint checks for deterministic diagnostics.

### Macro Call Styles

Prefix style (classic):

```l2
add2 10 32
```

Call style with comma-separated arguments:

```l2
add2(10, 32)
```

Call style is useful when an argument is a token sequence:

```l2
macro sum3 (a b c)
	$a $b + $c +
;

sum3(20 1 +, 10, 11)
```

### Pattern-Matching Macros

L2 now supports clause-based pattern macros inside normal `macro` definitions:

```l2
macro simplify
	$x:int + 0 => $x ;
	0 + $x:int => $x ;
;
```

Behavior:

- Clauses are checked in definition order.
- Captures support rewrite syntax: `$x`, `$*xs`, `$x:int`.
- Reusing the same capture name in one pattern enforces equality.
- Pattern macros terminate with a trailing `;` after the final clause.
- Under the hood, this compiles to grammar-stage rewrite rules.

### Compile-Time Registration APIs

You can define text macros from compile-time code:

- `ct-register-text-macro`: register by positional arity.
- `ct-register-text-macro-signature`: register with named/variadic parameter spec.
- `ct-register-pattern-macro`: register pattern macro clauses programmatically.
- `ct-unregister-pattern-macro`: remove a previously registered pattern macro.
- `ct-word-is-text-macro`: query whether a word is a text macro.
- `ct-word-is-pattern-macro`: query whether a word is a pattern macro.
- `ct-get-macro-signature`: retrieve parameter names and variadic parameter.
- `ct-get-macro-expansion` / `ct-set-macro-expansion`: inspect and update text-macro expansions.
- `ct-clone-macro` / `ct-rename-macro`: clone/rename text or pattern macros.
- `ct-macro-doc-get` / `ct-macro-doc-set`: attach free-form documentation strings to macros.
- `ct-macro-attrs-get` / `ct-macro-attrs-set`: attach structured metadata maps to macros.
- `ct-list-pattern-macros`: enumerate pattern macro names.
- `ct-set-pattern-macro-enabled` / `ct-get-pattern-macro-enabled`: toggle/query rule activation.
- `ct-set-pattern-macro-priority` / `ct-get-pattern-macro-priority`: adjust/query rule priority.
- `ct-get-pattern-macro-clauses`: inspect registered clause pairs.

### CT-Call Policy Controls

- Typed contracts:
  - `ct-set-ct-call-contract`, `ct-get-ct-call-contract`
- Exception handling policy:
  - `ct-set-ct-call-exception-policy`, `ct-get-ct-call-exception-policy`
- Sandbox mode + allowlist:
  - `ct-set-ct-call-sandbox-mode`, `ct-get-ct-call-sandbox-mode`
  - `ct-set-ct-call-sandbox-allowlist`, `ct-get-ct-call-sandbox-allowlist`
- Deterministic randomness:
  - `ct-ctrand-seed`, `ct-ctrand-int`, `ct-ctrand-range`
- Memoization:
  - `ct-set-ct-call-memo`, `ct-get-ct-call-memo`
  - `ct-clear-ct-call-memo`, `ct-get-ct-call-memo-size`
- Side-effect log:
  - `ct-set-ct-call-side-effects`, `ct-get-ct-call-side-effects`
  - `ct-get-ct-call-side-effect-log`, `ct-clear-ct-call-side-effect-log`
- Recursion and timeout guards:
  - `ct-set-ct-call-recursion-limit`, `ct-get-ct-call-recursion-limit`
  - `ct-set-ct-call-timeout-ms`, `ct-get-ct-call-timeout-ms`

Example:

```l2
# docs:skip-check
word setup-macros
	"sum2"                                # name
	list-new "x" list-append "y" list-append
	list-new "$x" list-append "$y" list-append "+" list-append
	ct-register-text-macro-signature
end
compile-time setup-macros
```

### Rewrite Rules (Reader/Grammar Stages)

For deeper syntax customization, use compile-time rewrites:

- Reader-stage rewrites: token stream rewrites after lexing.
- Grammar-stage rewrites: rewrites before normal parse handling.

Core tools include:

- `ct-add-reader-rewrite`, `ct-add-grammar-rewrite`
- named/priority variants
- enable/disable, list, clear, remove, and priority query/update words

These are best for local syntax normalization patterns and DSL sugar.

### Language Extension Pack APIs (`ct-lang-*`)

L2 now includes a language-extension registry in the compile-time VM so you can
create, activate, introspect, and cleanly remove DSL packs without hard-coding
language-specific behavior in the core parser.

New compile-time words:

- `ct-lang-create`
- `ct-lang-exists?`
- `ct-lang-list`
- `ct-lang-activate`
- `ct-lang-deactivate`
- `ct-lang-active?`
- `ct-lang-active-list`
- `ct-lang-meta-set`
- `ct-lang-meta-get`
- `ct-lang-set-auto-validate`
- `ct-lang-get-auto-validate`
- `ct-lang-add-validator`
- `ct-lang-run-validators`
- `ct-lang-run-active-validators`
- `ct-lang-set-token-hook`
- `ct-lang-add-reader-rewrite-named`
- `ct-lang-add-grammar-rewrite-named`
- `ct-lang-register-text-macro-signature`
- `ct-lang-register-pattern-macro`
- `ct-lang-status`
- `ct-lang-remove`

Typical lifecycle flow:

1. `ct-lang-create`
2. register rewrites/macros/hooks
3. `ct-lang-activate`
4. optional validator configuration (`ct-lang-add-validator`, auto/manual runs)
5. introspection via `ct-lang-status`
6. cleanup via `ct-lang-remove`

### Advanced Pattern/Rewrite Controls

L2 now includes a richer rewrite toolchain for large macro systems:

- Pattern grouping/scope activation:
  - `ct-set-pattern-macro-group`, `ct-get-pattern-macro-group`
  - `ct-set-pattern-macro-scope`, `ct-get-pattern-macro-scope`
  - `ct-set-pattern-group-active`, `ct-set-pattern-scope-active`
  - `ct-list-active-pattern-groups`, `ct-list-active-pattern-scopes`
- Pattern clause guards and introspection:
  - `ct-set-pattern-macro-clause-guard`
  - `ct-get-pattern-macro-clause-details`
- Pattern diagnostics and analysis:
  - `ct-detect-pattern-conflicts`, `ct-detect-pattern-conflicts-named`
  - `ct-get-rewrite-specificity`
  - `ct-rewrite-compatibility-matrix`
- Rewrite pipelines and matcher indexing:
  - `ct-set-rewrite-pipeline`, `ct-get-rewrite-pipeline`
  - `ct-set-rewrite-pipeline-active`, `ct-list-rewrite-active-pipelines`
  - `ct-rebuild-rewrite-index`, `ct-get-rewrite-index-stats`
- Rewrite transactions and packs:
  - `ct-rewrite-txn-begin`, `ct-rewrite-txn-commit`, `ct-rewrite-txn-rollback`
  - `ct-export-rewrite-pack`, `ct-import-rewrite-pack`, `ct-import-rewrite-pack-replace`
  - `ct-get-rewrite-provenance`
- Dry-run and fixture tooling:
  - `ct-rewrite-dry-run`
  - `ct-rewrite-generate-fixture`
- Runtime safety/observability:
  - `ct-set-rewrite-saturation`, `ct-get-rewrite-saturation`
  - `ct-set-rewrite-max-steps`, `ct-get-rewrite-max-steps`
  - `ct-set-rewrite-loop-detection`, `ct-get-rewrite-loop-detection`
  - `ct-get-rewrite-loop-reports`, `ct-clear-rewrite-loop-reports`
  - `ct-set-rewrite-trace`, `ct-get-rewrite-trace`, `ct-get-rewrite-trace-log`, `ct-clear-rewrite-trace-log`
  - `ct-get-rewrite-profile`, `ct-clear-rewrite-profile`

Pattern syntax now supports additional operators in rewrite patterns:

- Negative token/capture match: `!token`, `!$x:int`
- Optional piece: `token?`, `$x?`, `$x:int?`
- Repetition piece: `token*`, `token+`, `$x:int*`, `$x:int+`
- Guarded clauses in macro syntax: `... when guard_word => ... ;`

### Safety and Debugging Controls

- `ct-set-macro-expansion-limit` / `ct-get-macro-expansion-limit`
- `ct-set-macro-preview` / `ct-get-macro-preview`
- `--macro-preview` (trace each macro/rewrite step)
- `--preview` (print final transformed source after macro + CT execution)

Use preview when developing complex expansions; keep limits sensible to avoid accidental recursive explosion.

### Execution-Mode Markers

You can explicitly control where a word is allowed to execute:

- `compile-only`: word can execute only during compilation.
- `runtime` (alias: `runtime-only`): word can execute only at runtime.

Example:

```l2
word only-runtime
	1
end
runtime
```

Trying to execute `only-runtime` from compile-time code now fails with a clear error.

L2 also provides a `CT` word for branching on execution mode:

- During compile-time execution, `CT` pushes `1`.
- In emitted runtime code, `CT` pushes `0`.

Example:

```l2
word maybe-debug
	CT if
		"[ct] running in compile-time VM" puts
	else
		"[rt] running in runtime code" puts
	end
end
```

## Runtime Eval Library (From main.c)

You can build a C library from [main.c](main.c) and call into L2 compilation/evaluation at runtime.

### 1. Build the library

```bash
./tools/build_l2eval_lib.sh
```

This produces:

- [build/libl2eval.a](build/libl2eval.a)
- [build/libl2eval.so](build/libl2eval.so)

### 2. Use from C

Public header: [libs/l2eval.h](libs/l2eval.h)

Example:

```c
#include <stdio.h>
#include "libs/l2eval.h"

int main(void) {
		int rc = l2_eval_cstr("word main 0 end");
		printf("rc=%d\n", rc);
		return 0;
}
```

Build and run:

```bash
cc -O2 host.c -I. -Lbuild -ll2eval -Wl,-rpath,$PWD/build -o host
./host
```

### 3. Use from L2 code

L2 string literals push two values: `(addr len)`. The runtime `l2_eval` API expects exactly those two arguments.

Example source: [examples/eval_runtime.sl](examples/eval_runtime.sl)

```l2
extern l2_eval 2 1

word main
	"import stdlib.sl word main 1 2 + puti cr end" l2_eval
end
```

Build and run (static link path shown):

```bash
python3 main.py examples/eval_runtime.sl -o /tmp/eval_runtime build/libl2eval.a -lc
/tmp/eval_runtime
```

## Core Tenets

1. **SIMPLICITY OVER CONVENIENCE** — No garbage collector, no hidden magic. You own every allocation and every free.

2. **TRANSPARENCY** — Every word compiles to a known, inspectable sequence of x86-64 instructions. `--emit-asm` shows exactly what runs on the metal.

3. **COMPOSABILITY** — Small words build big programs. The stack is the universal interface — no types to reconcile, no generics to instantiate.

4. **META-PROGRAMMABILITY** — The front-end is user-extensible: text macros, `:py` blocks, immediate words, and token hooks reshape syntax at compile time.

5. **UNSAFE BY DESIGN** — Safety is the programmer's job, not the language's. L2 trusts you with raw memory, inline assembly, and direct syscalls.

6. **MINIMAL STANDARD LIBRARY** — The stdlib provides building blocks — not policy. It gives you `alloc`/`free`, `puts`/`puti`, arrays, and file I/O. Everything else is your choice.

7. **FUN FIRST** — If using L2 feels like a chore, the design has failed.

---

L2 is for programmers who want to understand every byte their program emits, and who believe that the best abstraction is the one you built yourself.

## License

Apache-2.0 — See [LICENSE](LICENSE)

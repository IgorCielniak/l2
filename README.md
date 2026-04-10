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
- `qiling` (optional, only needed for `--ct-run-main` on non-Linux hosts such as Windows/Android). L2 uses Qiling in VM shellcode mode for runtime asm execution fallback and does not create rootfs directories.

### Building

```bash
python3 main.py examples/snake.sl -o snake
./snake
```

### Tests

```bash
python3 test.py
```

To simulate the phone/non-native `--ct-run-main` path on Linux without copying
files to a device, force VM fallback locally:

```bash
L2_FORCE_QILING_VM=1 python3 main.py tests/hello.sl --script
```

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
- Variadic parameter must be last in the signature.

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

### Safety and Debugging Controls

- `ct-set-macro-expansion-limit` / `ct-get-macro-expansion-limit`
- `ct-set-macro-preview` / `ct-get-macro-preview`

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

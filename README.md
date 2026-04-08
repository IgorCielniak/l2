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

CC BY-NC-SA 4.0 — See [LICENSE](LICENSE)

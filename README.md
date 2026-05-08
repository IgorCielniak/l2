# L2: A Forth-Inspired Assembly Language with Compile-Time Power

> **Give the programmer raw power and get out of the way.**

## What Is L2?

L2 is a systems programming language that sits at the sweet spot between **assembly** and **high-level metaprogramming**. It gives you:

- **Direct control**: Every word you write compiles to inspectable x86-64 instructions. No hidden overhead, no garbage collection, no surprise codegen.
- **Stack-based composition**: Small reusable "words" (functions) compose into larger programs through a universal stack interface.
- **Compile-time computation**: Run arbitrary L2 code at compile time to generate code, compute constants, build data structures, or implement DSLs—all with **zero runtime overhead**.
- **Syntax extensibility**: Text macros, token hooks, and pattern rewriting let you craft the syntax you need without forking the compiler.

If you've used **Forth** or **Factor**, L2 will feel familiar. If you've hand-written assembly, you'll appreciate having abstraction without mystery.

## Why L2?

### You might choose L2 if you:

- **Want transparency**: Every byte your program emits should be *your* choice, not the compiler's guess.
- **Need fine-grained control**: Direct memory access, inline assembly, and syscalls are first-class citizens.
- **Like minimalism**: No stdlib bloat, no implicit behavior. You get allocation, I/O, arrays—the building blocks. Everything else is your design.
- **Enjoy metaprogramming**: Generate repetitive code safely at compile time, define DSLs, or build code generators without external tools.
- **Value performance**: Direct assembly execution, early computation of constants, and predictable codegen mean no surprise stalls.

### You might **not** choose L2 if you:

- Need rapid prototyping with maximum convenience (try Python or Go instead).
- Want memory safety guaranteed by the language (try Rust or Zig instead).
- Prefer garbage collection and dynamic typing (try Lua or Python instead).

---

## Core Design Principles

1. **Simplicity over Convenience** — No garbage collector, no hidden magic. You own every allocation and every byte.

2. **Transparency** — Every word compiles to a known, inspectable sequence of x86-64 instructions. Use `--emit-asm` to see exactly what runs.

3. **Composability** — Small words build big programs. The stack is the universal interface—no types to reconcile, no generics to instantiate.

4. **Meta-Programmability** — The front-end is user-extensible: text macros, token hooks, compile-time words, and rewrite rules let you reshape syntax without forking the compiler.

5. **Unsafe by Design** — Safety is the programmer's responsibility. L2 trusts you with raw memory, inline assembly, and direct syscalls. With great power comes great responsibility.

6. **Minimal Standard Library** — The stdlib provides building blocks, not policy. You get `alloc`/`free`, `puts`/`puti`, arrays, file I/O etc. Everything else is your architectural choice.

7. **Fun First** — If using L2 feels like a chore, the design has failed.

---

## Getting Started

### Prerequisites

**Required:**
- Python 3.7+
- NASM (Netwide Assembler)
- GNU binutils (`ld`)
- Linux x86-64

**Optional:**
- `keystone-engine` (for compile-time JIT and `:asm` execution; otherwise compile-time words run interpreted)

### Your First L2 Program

Create `hello.sl`:

```
import stdlib.sl

word main
  "Hello, World!\n" puts
  0
end
```

Compile and run:

```bash
python3 main.py hello.sl -o hello
./hello
```

### Running Tests & Examples

Run the test suite:

```bash
python3 test.py
```

Build and run examples:

```bash
# Conway's Game of Life
python3 main.py examples/game_of_life.sl -o life
./life

# Interactive Snake game
python3 main.py examples/snake.sl -o snake
./snake
```

---

## Language Features

### Stack-Based Computation

L2 uses a data stack (like Forth) as its primary mechanism for passing values. Words pop arguments from the stack and push results back:

```
import stdlib.sl

word double
  dup +         # duplicate top, add them
end

word main
  5 double      # → 10
  puti          # print it
  0
end
```

### Definitions & Control Flow

Define reusable words with `word ... end`. Control flow uses `if`/`else`, `for` loops, and conditionals:

```
import stdlib.sl

word factorial
  dup 1 <= if
    drop 1
  else
    dup 1 - factorial *
  end
end

word main
  5 factorial puti cr   # prints: 120
  0
end
```

### Inline Assembly (`:asm` blocks)

Drop into raw x86-64 when you need it:

```
:asm fast-memcpy {
    mov rax, [r12]       # load src
    mov rbx, [r12 + 8]   # load dst
    mov rcx, [r12 + 16]  # load size
    rep movsb
};
```

### External C Functions

Call C functions directly:

```
extern malloc 1 1         # takes 1 arg, returns 1 result
extern free 1 0

word main
  1024 malloc
  free
  0
end
```

In order to link with libc compile with `-lc` like this:

```
python3 main.py file_name.sl -lc
```


### Memory & Arrays

Allocate and manipulate memory:

```
import stdlib.sl

word main
  1000 alloc dup         # allocate, keep a copy
  dup 0 + 42 !64         # store 42 at offset 0
  dup 8 + 99 !64         # store 99 at offset 8
  
  dup 0 + @64 puti cr    # print 42
  dup 8 + @64 puti cr    # print 99
  
  free
  0
end
```

### Modules & Imports

Modularize your code:

```
import stdlib.sl           # standard library
import my_utils.sl         # your own modules

word main
  ...
end
```

---

## Compile-Time Metaprogramming

L2's killer feature is its **compile-time virtual machine**. Run arbitrary L2 code at compile time to:

- Generate repetitive code patterns
- Compute constants and lookup tables
- Build custom data structures
- Implement domain-specific languages (DSLs)
- Customize parsing and syntax

All with **zero runtime cost**.

### Text Macros

The simplest metaprogramming: parametrized text replacement:

```
import stdlib.sl

macro double-all (n)
  $n $n +
;

macro sum3 (a b c)
  $a $b + $c +
;

word main
  sum3(1, 2, 3) puti cr    # prints: 6
  0
end
```

Use compile-time control flow inside macros:

```
import stdlib.sl

macro emit-all (*items)
  ct-for item in items do
    $item
  end
;

word main
  emit-all(1, 2, 3, 4, 5) + + + + puti cr  # prints: 15
  0
end
```

### Compile-Time Words

Mark a word `compile-time` to run while compiling instead of emitting runtime code:

```
word build-lookup-table
  # This runs at compile time
  0 100 for i
    i i * ,    # emit i² into data section
  end
end

compile-time build-lookup-table
```

### Pattern Macros & Rewrites

Use pattern-matching macros and rewrite rules for deep code transformation:

```
# Eliminate redundant operations at parse time
macro simplify
  $x:int + 0 => $x ;
  0 + $x:int => $x ;
  $x:int * 1 => $x ;
;
```

And syntax rewrites for DSL customization:

```
# Custom parsing: transform [a, b, c] into a list
ct-add-grammar-rewrite "[" "$*items ]" "(list-literal $*items)"
```

### More Metaprogramming

L2 has extensive CT APIs for:

- Template control flow: `ct-if`, `ct-for`, `ct-each`, `ct-fold`, `ct-let`, `ct-switch`
- Capture/scope management for hygienic macros
- Pattern matching with guards
- Language extension packs and DSL lifecycle
- Memoization, sandboxing, and recursion limits
- Rewrite rule priority, pipelines, and analysis

**For the complete API reference:**

- Read the docstring: `python3 main.py --docs`
- Serve in browser: `python3 main.py --docs-serve --docs-port 8018`

---

## Building & Compilation

### Compiler Invocation

```bash
python3 main.py source.sl [options] -o binary
```

Common options:

- `-o FILE`: Output executable name
- `-l PATH`: Link against a library (`.so`, `.a`, or system lib like `c`)
- `--check`: Parse and check without generating output
- `--emit-asm`: Emit assembly (.asm file) without assembling/linking
- `--no-artifact`: Skip final linking (useful with `--emit-asm`)
- `--force`: Rebuild everything (ignore cache)
- `--no-cache`: Skip compiler caches but allow tool-level incremental builds
- `-v LEVEL`: Verbosity (1-3 for timing and diagnostics)

### Caching & Optimization

L2 has multi-layer caching to speed up recompilation:

- **Source cache**: Preprocessed imports and dependency graph
- **Assembly cache**: Emitted x86-64 assembly
- **Tool-level incrementality**: NASM/linker skip unchanged inputs

Use `--no-cache` to skip compiler caches (but keep NASM/linker incremental builds). Use `--force` to rebuild from scratch.

### Debugging & Profiling

Inspect macro expansion and compile-time behavior:

```bash
# Show timing for each macro expansion
python3 main.py source.sl --macro-profile

# Print expanded source after macros/CT execution
python3 main.py source.sl --no-artifact --preview

# Write detailed profile to file
python3 main.py source.sl --macro-profile build/profile.txt
```

---

## Documentation & Reference

### Browse the Compile-Time API

Generate and view the complete API documentation:

```bash
# View in terminal
python3 main.py --docs

# Serve in browser with search and tabs
python3 main.py --docs-serve --docs-port 8018
```

### Learning Resources

- **Examples**: Start with [examples/](examples/) (Game of Life, Snake, eval_runtime)
- **Tests**: Run `python3 test.py` to see the test suite
- **Stdlib**: Browse [stdlib/](stdlib/) for reusable words and patterns

---

## Runtime Eval Library

You can call L2 from C (and vice versa) via the runtime evaluation library built from [main.c](main.c).

### Build the Library

```bash
./tools/build_l2eval_lib.sh
```

Produces `build/libl2eval.a` (static) and `build/libl2eval.so` (dynamic).

### Example: Call L2 from C

```c
#include "libs/l2eval.h"
#include <stdio.h>

int main(void) {
    long result = l2_eval_cstr("word main 5 2 * end");
    printf("5 * 2 = %ld\n", result);  // outputs: 10
    return 0;
}
```

Compile and link:

```bash
cc -O2 program.c -I. -Lbuild -ll2eval -Wl,-rpath,build -o program
./program
```

### Example: Call C from L2

```
import stdlib.sl

extern l2_eval 2 1

word main
  "1 2 +" l2_eval         # compile & evaluate L2 code at runtime
  puti cr
  0
end
```

```bash
python3 main.py program.sl -o program -lbuild/libl2eval.a -lc
./program
```

---

## Architecture & Design

### Compilation Pipeline

1. **Lexing** (Reader): Tokenize source → Token stream
2. **Macro expansion**: Text macro and pattern rewrite passes
3. **Parsing**: Build AST from transformed tokens
4. **Compile-time execution**: Run `compile-time` words and `ct-*` directives
5. **Code generation**: Emit x86-64 assembly
6. **Assembly**: `nasm` → object files
7. **Linking**: `ld` → executable

Each step can be cached and profiled.

### Runtime Model

L2 uses two runtime stacks:

- **Data stack** (r12): Operand stack for computation
- **Return stack** (r13): Call frames for word calls and `>r` / `r>` operations

Words interact via these stacks—no hidden state, no implicit contexts.

### Compile-Time vs Runtime

- **Compile-time** (`compile-time`, `CT = 1`): Code runs during compilation. Can generate new words, compute constants, and control syntax.
- **Runtime** (`CT = 0`): Code runs when the executable is invoked. Normal computation happens here.

---

## Contributing & Development

L2 is actively developed. Areas for contribution:

- **Standard library**: More array/string/math utilities
- **Examples**: Real-world programs showcasing L2
- **Performance**: Faster parsing, better codegen, optimized stdlib
- **Documentation**: Guides, tutorials, API docs
- **Tooling**: Debuggers, profilers, LSP support
- **Ports**: Support for other architectures (ARM, RISC-V, etc.)

To contribute:

1. Fork or branch
2. Make changes
3. Test: `python3 test.py`
4. Check syntax: `python3 -m py_compile l2_main.py`
5. Submit a PR with a clear description

---

## License

Apache-2.0 — See [LICENSE](LICENSE)

---

## Acknowledgments

L2 draws inspiration from:
- **Forth**: Stack-based composition, minimalism
- **Lisp**: Meta-programmability and compile-time power
- **Assembly**: Transparency and direct control
- **Lua/Zig**: Pragmatic balance of power and clarity

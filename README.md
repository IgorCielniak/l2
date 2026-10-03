# L2: A stack based, compiled, low level and untyped programming language

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

- **Want transparency**: Every byte your program emits should be _your_ choice, not the compiler's guess.
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

- `keystone-engine` is used by the runtime JIT path when available. The repo vendors a copy and the build script can fetch it automatically, so the JIT is available in typical local builds without a system-wide Keystone install.

### Your First L2 Program

Create `hello.sl`:

```l2
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

```l2
import stdlib.sl

word double
  dup +         # duplicate top, add them
end

word main
  5 double
  puti
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
:asm fast_memcpy {
    mov rsi, [r12]       # src
    mov rdi, [r12 + 8]   # dst
    mov rcx, [r12 + 16]  # size
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
  "lookup_table:" data-append
  0 100 for i
    i i * ct-repr "dq " swap string-append data-append
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

This token hook adds simple comma-separated function-call syntax. It rewrites
`sum3(1, 2, 3)` to the stack-based call `1 2 3 sum3` (arguments here must not
contain nested calls):

```
import stdlib.sl

word call-syntax-rewrite
  dup token-lexeme identifier? 0 == if drop 0 exit end
  peek-token dup nil? if drop drop 0 exit end
  dup token-lexeme "(" string= 0 == if drop drop 0 exit end
  swap >r
  drop
  next-token drop
  list-new
  list-new
begin
  next-token dup nil? if "unterminated call" parse-error end
  dup token-lexeme ")" string= if
    drop
    list-extend
    r> list-append
    inject-tokens
    1 exit
  end
  dup token-lexeme "," string= if
    drop
    list-extend
    list-new
    continue
  end
  list-append
again
end
immediate
compile-only

word enable-call-syntax
  "simple-calls" ct-lang-create drop
  "simple-calls" ct-lang-activate drop
  "simple-calls" "call-syntax-rewrite" ct-lang-set-token-hook drop
end
compile-time enable-call-syntax

word sum3
  + +
end

word main
  sum3(1, 2, 3) puti cr  # prints: 6
  0
end
```

This code might look scary and complicated but after reading the documentations it becomes pretty straight forward, the system is relatively complicated so it can support many usecases and provide a universal way of extending the language.

### More Metaprogramming

L2 has extensive CT APIs for:

- Template control flow: `ct-if`, `ct-for`, `ct-each`, `ct-fold`, `ct-let`, `ct-switch`
- Capture/scope management for hygienic macros
- Pattern matching with guards
- Language extension packs and DSL lifecycle
- Memoization, sandboxing, and recursion limits
- Rewrite rule priority, pipelines, and analysis

**For the complete API reference:**

- Read the docs: `python3 main.py --docs`
- Serve in browser: `python3 main.py --docs-serve --docs-port 8018`

---

## Building & Compilation

### Compiler Invocation

```bash
python3 main.py source.sl [options] -o binary
```

By default, `main.py` acts as a client and sends requests to a background
compiler daemon. This keeps the compiler process warm and enables concurrent
requests (for example, compile requests and future LSP/API requests).

Common options:

- `-o FILE`: Output executable name
- `-l PATH`: Link against a library (`.so`, `.a`, or system lib like `c`)
- `--check`: Parse and check without generating output
- `--emit-asm`: Emit assembly (.asm file) without assembling/linking
- `--no-artifact`: Skip final linking (useful with `--emit-asm`)
- `--force`: Rebuild everything (ignore cache)
- `--no-cache`: Skip compiler caches but allow tool-level incremental builds
- `-v LEVEL`: Verbosity (1-3 for timing and diagnostics)
- `--no-daemon`: Bypass the daemon for this invocation and run in-process

Daemon mode is enabled by default. Set `L2_DAEMON=0` to disable it, or
`L2_DAEMON=1` to enable it explicitly.

With daemon mode enabled, eligible `main.py` compiler invocations check for the
background compiler, start it on demand when necessary, and forward the
compiler arguments. Local-only commands, such as `--docs`, `--repl`, and
`--run`, stay in-process. The daemon is hosted internally by
`l2_main.py --daemon-serve`; users do not need a daemon lifecycle command. It
records bounded compilation history, per-request logs under `build/logs/req/`,
and a rotating global log at `build/logs/daemon.log`. It watches `main.py`,
`l2_main.py`, and `docs.py` for changes, then reloads after active requests drain
while preserving recent history in `build/.l2_daemon_state.json`. Set
`L2_DAEMON_NO_AUTO_RELOAD=1` to disable source-triggered reloads, for example in
deterministic CI or daemon integration tests. Embedding and tooling clients can
query the internal status API for uptime, memory, open file descriptors, queue
depth, cache counters, request history, and event-bus statistics. The daemon
also exposes a live event stream over its Unix socket via `subscribe_events`.

### Compiler events

L2 compile-time source is the primary event API. The compiler emits detailed
event objects for parsing, imports, macro expansion, compile-time execution,
word compilation, optimization, emission, diagnostics, and user events.
`on-event` can register a compile-only word whose event object is pushed on the
compile-time stack:

```l2
word inspect_compile_event
  get-event-type "compile" string= static_assert
  get-event-name string-length 0 > static_assert
end on-event compile
```

Handlers are compile-time words, so event filtering and processing use the same
stack operations as the rest of the language:

```l2
word inspect_word_compile_event
  get-event-payload
  "name" map-get static_assert
  drop drop
end on-event word.compile.begin
```

Patterns use glob matching (`word.compile.*`, `import.*`, `*`). Event objects
expose `type`, `name`, `timestamp_ns`, `location`, and `payload`/`data`.
Compile-time code can construct and emit its own objects:

```l2
word emit_build_event
  "build.note" event-object-create
  "message" "generated" event-object-prop-append
  event-object-emit
end on-event build.request

emit-event build.request

word main
  0
end
```

The event builtins are `get-event-type`, `get-event-name`,
`get-event-timestamp`, `get-event-payload`, `event-object-create`,
`event-object-prop-append`, `event-object-prop-get`, and
`event-object-emit`. `ct-repr` consumes a compile-time value and pushes its
normalized JSON representation as a string; use `puts` to print it while
inspecting an event payload. Lower-level `event-subscribe`, `event-unsubscribe`, and
`event-emit` remain available for compiler extensions. Python tooling can use
`EventBus`, while external IDEs can subscribe to the daemon's Unix-socket
`subscribe_events` request or consume `--events-stream` JSON Lines.

Python exposes the same `EventBus` for IDEs and external tooling; compiler
invocations can also write JSON Lines to a file path, including when routed
through the daemon:

```bash
python3 main.py tests/general/hello.sl --no-artifact --events-stream build/events.jsonl
python3 tools/event_tail.py --pattern 'word.compile.*' --replay
```

Events include compile/import boundaries, word emission, macro expansion,
compile-time execution, diagnostics, and emitted sections. Daemon clients can
subscribe with the `subscribe_events` JSON-socket request; `event_tail.py` is
the supplied human-readable client.

### C ABI library exports

Build a shared/static/object library with SysV x86-64 C wrappers. With
`--c-abi`, every runtime function receives a C wrapper; no per-word export
marker or stack-effect comment is needed. Each wrapper accepts an argument
count followed by that many `int64_t` variadic arguments and returns the top
value left on the L2 data stack:

```l2
word add2 + end
```

```bash
python3 main.py add2.sl --artifact shared --c-abi -o libadd2.so
```

This emits `libadd2.so` and `libadd2.h`; call the entry point as
`l2_add2(2, a, b)`. Arguments are pushed onto the L2 data stack in order,
including arguments passed on the native stack. This mode targets the System
V AMD64 ABI.

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

## L2 Runtime Library

You can call L2 from C (and vice versa) via the L2 runtime library built from [main.c](main.c). The library exposes `eval`, `eval_env`, and `compile()` (runtime code compilation) — everything needed to embed L2 in a host program.

For scalar evaluation, prefer `l2_eval_ex()` from [libs/l2.h](libs/l2.h): it returns an explicit status and writes the full `int64_t` result through an output pointer. The legacy `l2_eval()` API returns `int` and can truncate results or make a valid `-1` indistinguishable from failure.

> **Note:** The library was previously called `libl2eval`. It has been renamed to `libl2` to reflect its expanded scope. Backwards-compatible symlinks (`libl2eval.a`, `libl2eval.so`) and a forwarding header (`libs/l2eval.h`) are still generated so existing consumers keep working.

### Build the Library

```bash
./tools/build_l2_lib.sh
```

Produces `build/libl2.a` (static) and `build/libl2.so` (dynamic).

The build script also fetches a prebuilt `libkeystone.so` from PyPI's
`keystone-engine` wheel into `tools/vendor/` (via
[tools/fetch_keystone.sh](tools/fetch_keystone.sh)) and copies it to
`build/libkeystone.so`. This means the runtime JIT (`l2_jit_from_asm`,
etc.) works out of the box with **no system Keystone install required**
— libl2 `dlopen`s the vendored copy at first use. Set
`L2_KEYSTONE_PATH=/path/to/libkeystone.so` to override, or
`L2_SKIP_KEYSTONE_FETCH=1` when running the build script to skip the
download (JIT will then be disabled at runtime unless a system copy is
already available).

### Example: Call L2 from C

```c
#include "libs/l2.h"
#include <stdio.h>

int main(void) {
    long result = l2_eval_cstr("word main 5 2 * end");
    printf("5 * 2 = %ld\n", result);  // outputs: 10
    return 0;
}
```

Compile and link:

```bash
cc -O2 program.c -I. -Lbuild -ll2 -Wl,-rpath,build -o program
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
python3 main.py program.sl -o program -lbuild/libl2.a -lc
./program
```

### Example: `compile()` — runtime code compilation

`l2_compile(source, len)` returns a pointer to executable native code that
runs the given L2 snippet against the current data stack. Invoke it via
`call` (from `stdlib/core.sl`) and free it with `l2_release`.

```
import stdlib.sl

extern l2_compile 2 1     # (ptr, len) -> fn_ptr
extern l2_release 1 0

word main
  "3 4 +" l2_compile     # compile at runtime
  dup                     # keep the pointer for l2_release
  call                    # execute -> pushes 7
  puti cr
  l2_release              # free the executable page
  0
end
```

```bash
python3 main.py program.sl -o program -lbuild/libl2.a -lc
./program        # prints: 7
```

The runtime compile path is a hybrid implementation: it tries a native
Keystone-backed JIT for JIT-safe snippets and falls back to the trampoline path
for more complex cases. In other words, the current runtime already supports
native code generation for compatible snippets, while still preserving the more
general fallback path for cases that cannot be emitted safely.

---

## Architecture & Design

### Compilation Pipeline

1. **Lexing** (Reader): Tokenize source → Token stream
2. **Macro expansion**: Text macro and pattern rewrite passes
3. **Parsing**: Build Modules and definitions
4. **Compile-time execution**: Run `compile-time` words and `ct-*` directives
5. **Code generation**: Emit x86-64 assembly
6. **Assembly**: `nasm` → object files
7. **Linking**: `ld` → executable

Each step can be cached and profiled.

### Runtime Model

L2 uses two runtime stacks:

- **Data stack** (r12): Operand stack for computation
- **Return stack** (r13): Counters for loops and `>r` / `r>` operations

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

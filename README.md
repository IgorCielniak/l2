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

- Python 3.12+
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

#### Arithmetic, logic and bits

All values are 64-bit integers with wrapping arithmetic (`+ - * / %`, with `/` and
`%` truncating toward zero). Integer division by zero, and `INT64_MIN -1 /`, raise
the CPU's divide error (SIGFPE), as in C; the compiler does not add checks.

`and`, `or` and `not` are *logical*: they treat any nonzero value as true and return
0 or 1. The bitwise operations are separate words:

| Word | Effect |
|------|--------|
| `band` `bor` `bxor` | bitwise AND, OR, XOR |
| `bnot` | bitwise complement |
| `shl` `shr` `sar` | shift left, logical shift right, arithmetic shift right |

### Definitions & Control Flow

Define reusable words with `word ... end`. Control flow uses `if`/`else`, counted `for` loops, `while ... do ... end`, and `begin ... again`; `break` leaves and `continue` restarts the innermost loop:

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

All definitions share one namespace across imported files. Defining a name
again does not discard the earlier definition: every definition stays as a
candidate, and the one with the highest `priority` is used, with the newest
winning a tie. A redefinition at the same priority prints a warning. Use
`priority` to override a standard-library word deliberately:

```
priority 1
word puti "custom puti" puts drop end   # used instead of stdlib's puti
```

Compile-time code can inspect and change candidates with the
`ct-definition-*` words (see `docs.py`, § 11B).

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

Each file is imported once, at the point where the parser reaches its
`import` line, so compile-time code that runs earlier (for example a
tokenizer change) applies to files imported later.

### Custom Frontends

A file can hand its whole syntax to a frontend written in L2 by starting
with a header:

```
frontend "libs/my_lang.sl" my-lang-parse
```

The implementation is loaded once per compilation; its entry point (default
`frontend-parse`, stack effect `text path -- tokens`) runs at compile time
for each file that selects it and returns the canonical L2 tokens to parse.
Imported files without a header use the importing file's frontend. Frontends
are supported by the Python compiler only.

---

## Compile-Time Metaprogramming

L2's killer feature is its **compile-time virtual machine**. Run arbitrary L2 code at compile time to:

- Generate repetitive code patterns
- Compute constants and lookup tables
- Build custom data structures
- Implement domain-specific languages (DSLs)
- Customize parsing and syntax

All with **zero runtime cost**.

### Macros

A macro is a small L2 program that runs at compile time. It consumes its
arguments (the next tokens after its name, or a whole backtick quotation per
argument) and `emit`s replacement code:

```
import stdlib.sl

macro double (n) `$n $n +` emit ;

macro sum3 (a b c) `$a $b + $c +` emit ;

word main
  sum3 1 2 3 puti cr      # prints: 6
  double 21 puti cr       # prints: 42
  0
end
```

A constant is a one-token macro, written with `const`:

```
const BUFFER_SIZE 4096
const NEWLINE '\n'
```

`const` is cheap: the macro is created only when the name is first used, so the
stdlib's ~870 syscall and flag constants cost almost nothing when unused.

The body is ordinary compile-time code, so it can use `if`, loops, helper
words, and the Syntax words to decide what to emit:

```
import stdlib.sl

# Sum the numbers in a quotation.
macro sum-all (items)
  $items 0 1 syntax-slice emit
  $items syntax>list list-pop-front drop drop
  begin
    dup list-empty? if drop break end
    list-pop-front drop list-new swap list-append list>syntax emit
    `+` emit
  again
;

word main
  sum-all `1 2 3 4 5` puti cr  # prints: 15
  0
end
```

### Running Words at Compile Time

The `compile-time <word>` directive immediately invokes an already-defined word
in the CT VM. At top level it runs only during compilation; inside another word
definition it also adds a call to that definition's body (whether the body is
emitted for runtime depends on the enclosing definition's flags):

```
word build-lookup-table
  # This runs at compile time
  "lookup_table:" ct-output-append-data
  0 100 for i
    i i * repr "dq " swap string-append ct-output-append-data
  end
end

compile-time build-lookup-table
```

### Quotations and `emit`

Backticks quote source as a compile-time value: `` `1 2 +` `` is an immutable
`Syntax` value holding those tokens, and nothing inside runs when it is
built. `emit` inserts a Syntax value into the code being compiled, so a
compile-time word can generate definitions:

```
word define_answer
  `word answer 42 end` emit
end
compile-time define_answer

word main answer puti cr 0 end   # prints: 42
```

Quotations exist only at compile time, so words that use them are run with
`compile-time` or marked `compile-only`.

`$( … )` inside a quotation runs compile-time code and splices its value: a token
or Syntax as is, a string as a word, an integer as a literal. Text written directly
against it is pasted on, so a word can generate named definitions:

```
# getter NAME: defines get-NAME, which pushes 42
word getter
  ct-parser-consume                       # the name token
  `word get-$(dup token-lexeme) 42 end` emit
  drop
end
immediate compile-only

getter answer        # defines get-answer
```

A macro can generate another macro. Quotations do not nest, so the inner macro's
quotation is written with escapes: `` \` `` for its backticks and `\$name` for its own
parameters, while `$name` and `$( … )` still splice the outer macro's values:

```
macro def-adder (name n)
  `macro $name (x) \` \$x $n + \` emit ;` emit
;
def-adder add5 5        # add5 1 -> 6
```

The spliced value is the top of the stack after the code runs; the code may consume one
value below it, so `$( )` splices (and consumes) the top value and `$(dup)` a copy. Macros are not hygienic: a name a macro introduces can clash with yours.
Use `gensym` for helper names, for example `` `word $("tmp" gensym) … end` ``.

`match` dispatches on the upcoming input tokens at compile time. The first
clause whose pattern fits is consumed, and its `$name` bindings can be
spliced into quotations:

```
macro simplify
  match
    [$x:int, +, 0] => `$x` emit   # simplify 5 + 0  becomes  5
    [0, +, $x:int] => `$x` emit
    [_] =>                        # anything else passes through
  end
;
```

Repetition groups match any number of values: `$( … )*` (zero or more) or `$( … )+`
(one or more), with an optional separator written against the quantifier. Each name
inside binds to a list, and `$( … )*` in a quotation repeats once per element:

```
# pairs a = 10 , b = 32 end   defines the words a and b
macro pairs
  match
    [ $( $k, =, $v:int ),+, end ] => `$( word $k $v end )*` emit
  end
;
```

### Stack effects of generated code

Effect comments (`# name [*, a | b] -> [* | c]`) document words but count items, not
cells (a string is one item and two cells), so the stack checker does not rely on
them for calls. Code that knows its exact effect declares it in cells:

```
word two 1 2 end
stack-effect two 0 2          # NAME INPUTS OUTPUTS
```

`stack-effect` (or `ct-definition-set-effect` from compile-time code) lets the
checker verify calls to the word and that its body leaves that many cells. `fn`
declares the effect of every function it defines.

### Rewrites and Token Hooks

This token hook adds simple comma-separated function-call syntax. A hook owned
by a language pack runs only while the pack is active; activation likewise
enables the pack's rewrites, words, and macros. It rewrites `sum3(1, 2, 3)` to the stack-based call `1 2 3 sum3` (arguments
here must not contain nested calls):

```
import stdlib.sl

word call-syntax-rewrite
  dup token-lexeme identifier? 0 == if drop 0 exit end
  0 ct-parser-peek dup nil? if drop drop 0 exit end
  dup token-lexeme "(" string-equal? 0 == if drop drop 0 exit end
  swap >r
  drop
  ct-parser-consume drop
  list-new
  list-new
begin
  ct-parser-eof? if "unterminated call" ct-parser-error end
  ct-parser-consume
  dup token-lexeme ")" string-equal? if
    drop
    list-extend
    r> list-append
    ct-parser-inject
    1 exit
  end
  dup token-lexeme "," string-equal? if
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

word install-call-syntax
  "simple-calls" ct-lang-create drop
  "simple-calls" ct-lang-activate drop
  "simple-calls" "call-syntax-rewrite" ct-lang-add-token-hook drop
end
compile-time install-call-syntax

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

- Quotations, Syntax values, `emit`, and `match`
- Definition candidates, priorities, and macro records (`ct-definition-*`, `ct-macro-*`)
- Language extension packs and owned-resource cleanup
- Rewrite rule priority, pipelines, and analysis

Small stdlib libraries build on these APIs:

| Library | What it gives |
|---------|---------------|
| `ct_store.sl` | A compile-time key/value store shared by all macros (`ct-store-set`, `ct-store-get`, `ct-store-keys`, ...) |
| `macro_variadic.sl` | Variadic macro arguments: `"end" variadic-rest` takes the input up to a terminator, `"(" ")" variadic-balanced` a balanced group |
| `macro_switch.sl` | `macros-off` ... `macros-on`: macro names in between compile as ordinary word calls |
| `rewrite_trace.sl` | `rewrite-trace-on` ... `rewrite-trace-off`: each applied rewrite is reported as a note with its location |
| `meta.sl` | Helpers for generating definitions, macros, and rewrites |
| `lambda.sl` | `lambda( ... )`: an anonymous word, pushed as a word pointer |

**For the complete API reference:**

- Read the docs: `python3 main.py --docs`
- Serve in browser: `python3 main.py --docs-serve --docs-port 8018`

The explorer lists words with their effect comments, and macros and constants as the
compiler parses them: parameters, the `doc` metadata a library sets with
`ct-macro-set` (else the comment above the definition), and the declared value.

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
- `--check-references`: Also report unknown words in every definition and macro, even
  unused ones (normally an unknown word is reported only when its definition is used,
  which keeps forward references working)
- `--emit-asm`: Emit assembly (.asm file) without assembling/linking
- `--no-artifact`: Skip final linking (useful with `--emit-asm`)
- `--force`: Rebuild everything (ignore cache)
- `--force-stdlib`: Rebuild the daemon's pre-parsed stdlib (see below)
- `--no-cache`: Skip compiler caches but allow tool-level incremental builds
- `-v LEVEL`: Verbosity (1-3 for timing and diagnostics)
- `--no-daemon`: Bypass the daemon for this invocation and run in-process

Daemon mode is enabled by default. Set `L2_DAEMON=0` to disable it, or
`L2_DAEMON=1` to enable it explicitly. `--daemon` requires the daemon: the compile
fails instead of running in-process when the daemon cannot be used.

**One daemon per checkout.** `main.py` from any directory talks to the same daemon:
it is found by the compiler checkout, the Python interpreter and an instance name, in
a private per-user runtime directory (`$XDG_RUNTIME_DIR/l2/`, else `/tmp/l2-<uid>/`;
only your processes may connect). Each request carries the caller's working
directory, umask, `PATH` and per-compile environment, so relative paths, `-o`, `-I`
and the `build/` directory behave exactly as without the daemon. Simultaneous
clients from any number of directories start one daemon. Another checkout or Python
gets its own daemon. Daemon logs and state live in `$XDG_STATE_HOME/l2/` (else
`~/.local/state/l2/`), not in the project.

The daemon starts one worker and adds more while jobs queue, up to `min(cpu, 8)`
(`--daemon-workers N` or `L2_DAEMON_WORKERS` when it starts); extra workers idle for
5 minutes are stopped, and a worker using more than 1 GB is replaced after its job
(`L2_DAEMON_WORKER_IDLE` seconds, `L2_DAEMON_WORKER_MAX_RSS_MB`). With no compile for
30 minutes the daemon exits (`--daemon-idle-timeout MIN` or `L2_DAEMON_IDLE_TIMEOUT`,
0 = never); the next compile starts it again. It reloads itself after active requests
drain when `main.py`, `l2_main.py` or `docs.py` change (`L2_DAEMON_NO_AUTO_RELOAD=1`
turns that off). A daemon that holds its socket but stops answering is replaced by
the next client. Daemons of the old per-directory layout (`build/.l2_daemon.sock`)
are stopped when a new daemon starts, unless they were used recently: one of this
checkout after a minute without requests, one of another checkout after 30 minutes.

Managing daemons (add `--json` for machine-readable output, `--daemon-name NAME` or
`L2_DAEMON_NAME` to select a named instance with its own workers and settings):

| Flag | Does |
|---|---|
| `--daemon-status` | pid, uptime, workers (busy, jobs, memory), queue, requests, settings, recent compiles |
| `--daemon-start` | start now (with `--daemon-workers N`, `--daemon-idle-timeout MIN`) |
| `--daemon-stop` | stop after running jobs finish |
| `--daemon-kill` | stop now: daemon, workers and running jobs |
| `--daemon-restart` | graceful stop and start, keeping its settings |
| `--daemon-reload` | re-execute in place after running jobs finish |
| `--daemon-list` | every daemon of yours: instances of all checkouts and old-layout daemons |
| `--daemon-logs [N or REQUEST]` | last N lines of the daemon log, or one request's log; `--follow` |
| `--daemon-events [PATTERN]` | recent events; `--follow` streams them |
| `--daemon-clear` | drop the request history and restart workers with empty caches |
| `--daemon-prune [--days N]` | remove files of stopped daemons and request logs older than N days |
| `--daemon-config` | settings for a new daemon, where each comes from, and where the running one differs |

`--daemon-stop`, `--daemon-kill`, `--daemon-reload` and `--daemon-clear` take `--all`
for every instance (stop and kill also stop old-layout daemons). `L2_DAEMON_SCOPE`
chooses what shares a daemon: `checkout` (default), `project` (the nearest directory
with `.git` or `l2.toml`) or `directory`. `L2_DAEMON_SOCKET` names a socket path
directly. `L2_DAEMON_RUNTIME_DIR` and `L2_DAEMON_STATE_DIR` move the runtime and
state directories. The daemon keeps the last 200 request logs.

Each daemon worker keeps a pre-parsed copy of the stdlib, and each job starts from it.
A job uses it when its file imports `stdlib.sl` as its first form and no setting that
changes parsing is given (`-D`, `-W`/`-Werror`, `--preview`, `--macro-preview`,
`--macro-profile`, `--macro-expansion-limit`, event streams). The copy is rebuilt when a
stdlib file changes (any write), or with `--force-stdlib`; `--force`
rebuilds your program but not the pre-parsed stdlib. The same is done for the compiler's
other libraries a program imports first (for example `libs/fn.sl`), and per `-D`/`-W`
setting. Set `L2_NO_WARM_PREFIX=1` (when the daemon starts) to turn this off.
`L2_DAEMON_TRUSTED_DIRS` (colon-separated directories, sent with each request) extends it to your own files:
leading imports from those directories are pre-parsed too, so editing `main.sl` reuses
the parsed project libraries until one of them changes. Only list directories whose
compile-time code you trust, since it runs in the long-lived worker.

Machine code assembled for compile-time `:asm` words (and the `--script` JIT) is cached
by its exact assembly text, so a changed definition always gets new code. The cache is
kept in memory, in a pack file under `$XDG_CACHE_HOME/l2` (or `~/.cache/l2`, or
`L2_ASM_CACHE_DIR`) shared by all compiler processes, and in each daemon worker, which
loads keystone and the stdlib's code before forking a job. A compile whose code is all
cached does not load keystone at all. The result of the post-emission assembly optimizer
is cached the same way (by the exact assembly text, in a second pack file tied to the
compiler's source), so compiling an unchanged program again skips that pass.
Large executables are assembled by several nasm processes at once: whole word
definitions that cannot fall through into each other are split into parts, labels used
across parts get hidden aliases, and `ld -r` joins the parts into the usual object file.
Anything unusual (nasm macros, alignment or data directives inside a word, exported
labels, a part that fails or warns) assembles the file in one piece instead, so errors
and warnings are nasm's own. `L2_NASM_PARALLEL=0` turns it off. These variables, like
`L2_ASM_CACHE`, are sent with each request, so a daemon compile uses the client's values.
`L2_ASM_CACHE=0` turns both pack files off;
`L2_NO_ASM_PREWARM=1` (when the daemon starts) turns off the worker's preparation.

Imports are searched in the importing file's directory, then `.`, `./stdlib`, the `-I`
paths, and finally the compiler's own directory and its `stdlib/`, so `import
stdlib.sl` works from any directory.

### Compiler events

L2 compile-time source is the primary event API. The compiler emits detailed
event objects for parsing, imports, macro expansion, compile-time execution,
word compilation, optimization, emission, diagnostics, and user events.
`on-event` subscribes a compile-only word as a handler. Each handler receives
its own copy of the event, a map `{event, timestamp_ns, payload}`, and must
consume it (stack effect `event --`):

```l2
word inspect_compile_event
  dup event-name "compile" string-equal? static-assert
  event-name string-length 0 > static-assert
end on-event compile
```

Handlers are compile-time words, so event filtering and processing use the same
stack operations as the rest of the language:

```l2
word inspect_word_compile_event
  event-payload
  "name" map-get static-assert
  drop
end on-event word.compile.begin
```

Patterns use glob matching (`word.compile.*`, `import.*`, `*`). Compile-time
code can build and publish its own events:

```l2
word emit_build_event
  drop
  "build.note" event-new
  "message" "generated" event-payload-set
  ct-event-emit-object drop
end on-event build.request

emit-event build.request

word main
  0
end
```

The event words are `event-new`, `event-payload-set`, `event-payload-get`,
`event-name`, `event-timestamp`, `event-payload`, `ct-event-emit`,
`ct-event-emit-object`, `ct-event-subscribe`, and `ct-event-unsubscribe`;
`ct-event-set-enabled` and `--no-events` turn events off, while
`ct-event-stream-set-enabled` and `--no-events-json` affect only the JSON Lines
file. `repr` consumes a
compile-time value and pushes a readable debug representation (not a stable
format); use `puts` to print it while inspecting an event payload. Python tooling can use
`EventBus`, while external IDEs can subscribe to the daemon's Unix-socket
`subscribe_events` request or consume `--events-stream` JSON Lines.

Python exposes the same `EventBus` for IDEs and external tooling; compiler
invocations can also write JSON Lines to a file path, including when routed
through the daemon:

```bash
python3 main.py tests/general/hello.sl --no-artifact --events-stream build/events.jsonl
python3 tools/event_tail.py --pattern 'word.compile.*' --replay
python3 main.py --daemon-events 'word.compile.*' --follow
```

Events include compile/import boundaries, word emission, macro expansion,
compile-time execution, diagnostics, and emitted sections. Daemon clients can
subscribe with the `subscribe_events` JSON-socket request; `event_tail.py` is
the supplied human-readable client.

`--events-filter` limits what `--events-stream` writes, as comma-separated glob
patterns, for example `--events-filter 'compiler.error*,definition.*'`. The
compiler keeps the newest 8,192 events for replay; `--events-ring N` changes that.

High-volume events (`ct.execute.*`, `macro.expansion`, and `definition.*`) are only
built when a subscription, observer, or stream wants them. Replay
(`subscribe(replay=True)`, `event_tail.py --replay`) returns only retained events,
so an event that nobody wanted at the time it happened cannot be replayed later.

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

Compile-time performance is tracked with `tools/bench_compile.py`. To compare this
checkout against another one (for example a worktree of `main`), run an interleaved A/B:

```bash
python3 tools/bench_compile.py --ab ../l2-main --runs 9 --max-file-regression 5 --max-geomean 0
```

It exits with status 1 when a file is more than 5% slower, or the geometric mean is
slower at all.

### Debugging & Profiling

Inspect macro expansion and compile-time behavior:

```bash
# Print each macro expansion and rewrite, at its source location
python3 main.py source.sl --macro-preview

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

The library has its own compiler (in C), which supports a subset of the compile-time
language: the core value, list, map, string, token and parser words, output buffers,
token hooks, `const`, and macros (general bodies, quotations with `$name` and `$( … )`,
quotation arguments). Language packs, the rewrite engine, `match`, events and custom
frontends are not supported there yet, so libraries such as `libs/fn.sl` need the Python
compiler. `tools/test_c_vm_parity.py` checks that every word the C compiler implements
behaves as in the Python compiler.

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

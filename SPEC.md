# L2 Language Specification (Draft)

## 1. Design Goals
- **Meta-language first**: L2 is a minimal core designed to be reshaped into other languages at runtime, matching Forth's malleability with modern tooling.
- **Native code generation**: Source compiles directly to NASM-compatible x86-64 assembly, enabling both AOT binaries and JIT-style pipelines.
- **Runtime self-modification**: Parsers, macro expanders, and the execution pipeline are ordinary user-defined words that can be swapped or rewritten on demand.
- **Total control**: Provide unchecked memory access, inline assembly, and ABI-level hooks for syscalls/FFI, leaving safety policies to user space.
- **Self-hosting path**: The bootstrap reference implementation lives in Python, but the language must be able to reimplement its toolchain using its own facilities plus inline asm.

## 2. Program Model
- **Execution units (words)**: Everything is a word. Words can be defined in high-level L2, inline asm, or as parser/runtime hooks.
- **Compilation pipeline**:
  1. Source stream tokenized via active reader (user-overridable).
  2. Tokens dispatched to interpreter or compiler hooks (also user-overridable).
  3. Resulting IR is a threaded list of word references.
  4. Code generator emits NASM `.text` with helper macros.
  5. `nasm` + `ld` (or custom linker) build an ELF64 executable.
- **Interpreted mode**: For REPLs or rapid experimentation, the compiler can emit temporary asm, assemble to an object in memory, and `dlopen` or `execve` it.
- **Bootstrapping**: `main.py` orchestrates tokenizer, dictionary, IR, and final asm emission.

## 3. Parsing & Macro System
- **Reader hooks**:
  - `read-token`: splits the byte stream; default is whitespace delimited with numeric/string literal recognizers.
  - `on-token`: user code decides whether to interpret, compile, or treat the token as syntax.
  - `lookup`: resolves token → word entry; can be replaced to build new namespaces or module systems.
- **Compile vs interpret**: Each word advertises stack effect + immediacy. Immediate words execute during compilation (macro behavior). Others emit code or inline asm.
- **Syntax morphing**: Provide primitives `set-reader`, `with-reader`, and word-lists so layers (e.g., Lisp-like forms) can be composed.

## 4. Core Types & Data Model
- **Cells**: 64-bit signed integers; all stack operations use cells.
- **Double cells**: 128-bit values formed by two cells; used for addresses or 128-bit arithmetic.
- **Typed views**: Optional helper words interpret memory as bytes, half-words, floats, or structs but core semantics stay cell-based.
- **User-defined types**: `struct`, `union`, and `enum` builders produce layout descriptors plus accessor words that expand to raw loads/stores.

## 5. Stacks & Calling Convention
- **Data stack**: Unlimited (up to memory). Manipulated via standard words (`dup`, `swap`, `rot`, `over`). Compiled code keeps top-of-stack in registers when possible for performance.
- **Return stack**: Used for control flow. Directly accessible for meta-programming; users must avoid corrupting call frames unless intentional.
- **Control stack**: Optional third stack for advanced flow transformations (e.g., continuations) implemented in the standard library.
- **Call ABI**: Compiled words follow System V: arguments mapped from data stack into registers before `call`, results pushed back afterward.

## 6. Memory & Allocation
- **Linear memory primitives**: `@` (fetch), `!` (store), `+!`, `-!`, `memcpy`, `memset` translate to plain loads/stores without checks.
- **Address spaces**: Single flat 64-bit space; no segmentation. Users may map devices via `mmap` or syscalls.
- **Allocators**:
  - Default bump allocator in the runtime prelude.
  - `install-allocator` allows swapping malloc/free pairs at runtime.
  - Allocators are just words; nothing prevents multiple domains.

## 7. Control Flow
- **Branching**: `if ... else ... then`, `begin ... until`, `case ... endcase` compile to standard conditional jumps. Users can redefine the parsing words to create new control forms.
- **Tail calls**: `tail` word emits `jmp` instead of `call`, enabling explicit TCO.
- **Exceptions**: Not baked in; provide optional libraries that implement condition stacks via return-stack manipulation.

## 8. Inline Assembly & Low-Level Hooks
- **Asm blocks**: `asm { ... }` injects raw NASM inside a word. The compiler preserves stack/register invariants by letting asm declare its stack effect signature.
- **Asm-defined words**: `:asm name ( in -- out ) { ... } ;` generates a label and copies the block verbatim, wrapping prologue/epilogue helpers.
- **Macro assembler helpers**: Provide macros for stack slots (`.tos`, `.nos`), temporary registers, and calling runtime helpers.

## 9. Foreign Function Interface
- **Symbol import**: `c-import "libc.so" clock_gettime` loads a symbol and records its address as a constant word. Multiple libraries can be opened and cached.
- **Call sites**: `c-call ( in -- out ) symbol` pops arguments, loads System V argument registers, issues `call symbol`, then pushes return values. Variadic calls require the user to manage `al` for arg count.
- **Struct marshalling**: Helper words `with-struct` and `field` macros emit raw loads/stores so C structs can be passed by pointer without extra runtime support.
- **Error handling**: The runtime never inspects `errno`; users can read/write the TLS slot through provided helper words.

## 10. Syscalls & OS Integration
- **Primitive syscall**: `syscall ( args... nr -- ret )` expects the syscall number at the top of stack, maps previous values to `rdi`, `rsi`, `rdx`, `r10`, `r8`, `r9`, runs `syscall`, and returns `rax`.
- **Wrappers**: The standard library layers ergonomic words (`open`, `mmap`, `clone`, etc.) over the primitive but exposes hooks to override or extend them.
- **Process bootstrap**: Entry stub captures `argc`, `argv`, `envp`, stores them in global cells (`argc`, `argv-base`), and pushes them on the data stack before invoking the user `main` word.

## 11. Module & Namespace System
- **Wordlists**: Dictionaries can be stacked; `within wordlist ... end` temporarily searches a specific namespace.
- **Sealing**: Wordlists may be frozen to prevent redefinition, but the default remains open-world recompilation.
- **Import forms**: `use module-name` copies references into the active wordlist; advanced loaders can be authored entirely in L2.

## 12. Build & Tooling Pipeline
- **Compiler driver**: `main.py` exposes modes: `build <src> -o a.out`, `repl`, `emit-asm`, `emit-obj`.
- **External tools**: Default path is `nasm -f elf64` then `ld`; flags pass-through so users can link against custom CRT or libc replacements.
- **Incremental/JIT**: Driver may pipe asm into `nasm` via stdin and `dlopen` the resulting shared object for REPL-like workflows.
- **Configuration**: A manifest (TOML or `.sl`) records include paths, default allocators, and target triples for future cross-compilation.

## 13. Self-Hosting Strategy
- **Phase 1**: Python host provides tokenizer, parser hooks, dictionary, and code emitter.
- **Phase 2**: Re-implement tokenizer + dictionary in L2 using inline asm for hot paths; Python shrinks to a thin driver.
- **Phase 3**: Full self-host—compiler, assembler helpers, and driver written in L2, requiring only `nasm`/`ld`.

## 14. Standard Library Sketch
- **Core words**: Arithmetic, logic, stack ops, comparison, memory access, control flow combinators.
- **Meta words**: Reader management, dictionary inspection, definition forms (`:`, `:noninline`, `:asm`, `immediate`).
- **Allocators**: Default bump allocator, arena allocator, and hook to install custom malloc/free pairs.
- **FFI/syscalls**: Thin wrappers plus convenience words for POSIX-level APIs.
- **Diagnostics**: Minimal `type`, `emit`, `cr`, `dump`, and tracing hooks for debugging emitted asm.

## 15. Command-Line & Environment
- **Entry contract**: `main` receives `argc argv -- exit-code` on the data stack. Programs push the desired exit code before invoking `exit` or returning to runtime epilogue.
- **Environment access**: `envp` pointer stored in `.data`; helper words convert entries to counted strings or key/value maps.
- **Args parsing**: Library combinators transform `argv` into richer domain structures, though raw pointer arithmetic remains available.

## 16. Extensibility & Safety Considerations
- **Hot reload**: Redefining a word overwrites its dictionary entry and emits fresh asm. Users must relink or patch call sites if binaries are already running.
- **Sandboxing**: None by default. Documented patterns show how to wrap memory/syscall words to build capability subsets without touching the core.
- **Testing hooks**: Interpreter-mode trace prints emitted asm per word to aid verification.
- **Portability**: Spec targets x86-64 System V for now but the abstraction layers (stack macros, calling helpers) permit future backends.
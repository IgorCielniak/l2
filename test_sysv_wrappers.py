#!/usr/bin/env python3
"""Tests for the --sysv-wrappers compiler flag.

Verifies that compiling with `--artifact obj|static|shared --sysv-wrappers`
emits System V ABI wrapper functions under the generated `l2_<word>` names.
Each wrapper takes an argument count followed by its L2 stack arguments:

  1. obj artifact: symbols exported, C driver links and returns correct values
  2. static archive: C driver links against the .a and returns correct values
  3. shared object: C driver links against the .so and returns correct values
    4. without the flag, no wrapper symbols are exported
    5. --artifact exe rejects the unsupported wrapper flag
  6. toggling the flag invalidates the assembly cache

Requires a C compiler (cc/gcc/clang); nm is used for symbol checks when
available. Run directly:  python3 test_sysv_wrappers.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
L2_MAIN = ROOT / "l2_main.py"
WORK = ROOT / "build" / "sysv_wrappers_test"

LIB_SOURCE = """\
import stdlib.sl

word add2  # (a b -- c)
    +
end

word sub2  # (a b -- c)
    -
end

word add3  # (a b c -- d)
    + +
end

word forty_two  # (-- n)
    42
end

# No stack-effect comment on purpose: wrappers must not depend on it.
word incr
    1 +
end

# Consumes its argument and pushes nothing: negative stack balance, so the
# wrapper must return 0 (not the unconsumed argument slot).
word sink
    drop
end

# Leaves the stack untouched: zero balance, wrapper must return 0.
word noop
end
"""

# A C driver that calls the wrapped l2 words. Repeated calls and interleaved
# printf usage smoke-test that callee-saved registers are preserved and the
# l2 stacks are re-initialized on every entry.
DRIVER_SOURCE = """\
#include <stdio.h>
#include <stdint.h>

extern long l2_add2(size_t argc, ...);
extern long l2_sub2(size_t argc, ...);
extern long l2_add3(size_t argc, ...);
extern long l2_forty_two(size_t argc, ...);
extern long l2_incr(size_t argc, ...);
extern long l2_sink(size_t argc, ...);
extern long l2_noop(size_t argc, ...);

int main(void) {
    int fails = 0;
    fails += l2_add2(2, 3, 4) != 7;          /* basic call */
    fails += l2_add2(2, (int64_t)-5, (int64_t)5) != 0; /* 64-bit negative arg */
    fails += l2_sub2(2, 10, 3) != 7;         /* first argument is deepest */
    fails += l2_sub2(2, 3, 10) != -7;
    fails += l2_add3(3, 1, 2, 3) != 6;       /* three args */
    fails += l2_forty_two(0) != 42;          /* zero args */
    fails += l2_incr(1, 41) != 42;           /* inferred stack effect */
    fails += l2_incr(1, (int64_t)-1) != 0;
    fails += l2_sink(1, 12345) != 0;         /* no result */
    fails += l2_noop(0) != 0;                /* no result */
    fails += l2_add2(2, 100, 23) != 123;     /* repeated calls */
    fails += l2_add3(3, 0, 0, 0) != 0;
    if (fails) {
        printf("FAIL (%d checks failed)\\n", fails);
        return 1;
    }
    printf("sysv_wrappers ok\\n");
    return 0;
}
"""

WRAPPER_SYMBOLS = (
    "l2_add2", "l2_sub2", "l2_add3", "l2_forty_two", "l2_incr", "l2_sink", "l2_noop"
)

# More complex words exercising recursion, while loops, two-arg words, and
# word composition through the SysV wrapper boundary.
COMPLEX_SOURCE = """\
import stdlib.sl

word fib  # (n -- fib)
    dup 2 < if
    else
        dup 1 - fib
        swap 2 - fib
        2dup + -rot 2drop
    end
end

word sum_to  # (n -- sum)
    0 swap
    while dup 0 > do
        swap over + swap
        1 -
    end
    drop
end

word gcd  # (a b -- gcd)
    while dup 0 != do
        swap over %
    end
    drop
end

word collatz  # (n -- steps)
    0 swap
    while dup 1 > do
        dup 2 % 0 == if
            2 /
        else
            3 * 1 +
        end
        swap 1 + swap
    end
    drop
end

word countdown  # (n -- n)
    dup 0 <= if
        drop 0
    else
        1 - countdown 1 +
    end
end

# Straight-line words: arity is inferred without a stack-effect comment.
word min
    2dup < if drop else nip end
end

word max
    2dup > if drop else nip end
end

# Composition: clamp_pair calls the user word min.
word clamp_pair  # (a b -- min)
    min
end
"""

COMPLEX_DRIVER = """\
#include <stdio.h>
#include <stdint.h>

extern long l2_fib(size_t argc, ...);
extern long l2_sum_to(size_t argc, ...);
extern long l2_gcd(size_t argc, ...);
extern long l2_collatz(size_t argc, ...);
extern long l2_countdown(size_t argc, ...);
extern long l2_min(size_t argc, ...);
extern long l2_max(size_t argc, ...);
extern long l2_clamp_pair(size_t argc, ...);

static long c_collatz(long n) {
    long s = 0;
    while (n > 1) { n = (n % 2 == 0) ? n / 2 : 3 * n + 1; s++; }
    return s;
}
static long c_gcd(long a, long b) {
    while (b != 0) { long t = a % b; a = b; b = t; }
    return a;
}
static long c_fib(long n) {
    if (n < 2) return n;
    return c_fib(n - 1) + c_fib(n - 2);
}

int main(void) {
    int fails = 0;
    fails += l2_fib(1, 0) != 0;
    fails += l2_fib(1, 1) != 1;
    fails += l2_fib(1, 10) != 55;
    fails += l2_fib(1, 15) != 610;

    fails += l2_sum_to(1, 0) != 0;
    fails += l2_sum_to(1, 1) != 1;
    fails += l2_sum_to(1, 100) != 5050;
    fails += l2_sum_to(1, 1000) != 500500;

    fails += l2_gcd(2, 12, 18) != 6;
    fails += l2_gcd(2, 17, 5) != 1;
    fails += l2_gcd(2, 100, 75) != 25;
    fails += l2_gcd(2, 1071, 462) != 21;

    fails += l2_collatz(1, 1) != 0;
    fails += l2_collatz(1, 6) != 8;
    fails += l2_collatz(1, 27) != c_collatz(27);
    fails += l2_collatz(1, 97) != c_collatz(97);
    fails += l2_collatz(1, 871) != c_collatz(871);

    fails += l2_countdown(1, 0) != 0;
    fails += l2_countdown(1, 1) != 1;
    fails += l2_countdown(1, 500) != 500;

    fails += l2_min(2, 7, 3) != 3;
    fails += l2_max(2, 7, 3) != 7;
    fails += l2_clamp_pair(2, 5, 9) != 5;
    fails += l2_clamp_pair(2, 12, 9) != 9;
    fails += l2_clamp_pair(2, (int64_t)-4, (int64_t)2) != -4;

    /* Interleaved repeated calls: no state may leak between entries. */
    for (long i = 0; i <= 20; i++)
        fails += l2_fib(1, i) != c_fib(i);
    for (long i = 1; i <= 50; i++)
        fails += l2_sum_to(1, i) != i * (i + 1) / 2;
    fails += l2_gcd(2, 12, 18) != 6;
    fails += l2_collatz(1, 27) != c_collatz(27);

    if (fails) {
        printf("FAIL (%d checks failed)\\n", fails);
        return 1;
    }
    printf("complex sysv wrappers ok\\n");
    return 0;
}
"""

COMPLEX_SYMBOLS = (
    "l2_fib", "l2_sum_to", "l2_gcd", "l2_collatz",
    "l2_countdown", "l2_min", "l2_max", "l2_clamp_pair"
)

_failures = []
_passes = 0


def _report(ok: bool, name: str, detail: str = "") -> None:
    global _passes
    if ok:
        _passes += 1
        print(f"  ok    {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))


def _run(cmd, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd],
        cwd=ROOT,
        capture_output=True,
        text=True,
        **kwargs,
    )


def _compile(src: Path, out: Path, artifact: str, *extra: str, temp_dir: Path = WORK) -> subprocess.CompletedProcess:
    return _run([
        sys.executable, L2_MAIN, src,
        "-o", out,
        "--artifact", artifact,
        "--temp-dir", temp_dir,
        *extra,
    ])


def _exported_text_symbols(artifact: Path):
    """Return the set of symbols reported as 'T' (global text) by nm, or None."""
    nm = shutil.which("nm")
    if nm is None:
        return None
    proc = _run([nm, artifact])
    if proc.returncode != 0:
        return None
    syms = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "T":
            syms.add(parts[2])
    return syms


def test_obj_artifact(cc: str) -> None:
    print("test: obj artifact")
    src = WORK / "libsysv.sl"
    src.write_text(LIB_SOURCE)
    obj = WORK / "libsysv.o"
    proc = _compile(src, obj, "obj", "--sysv-wrappers", "--force")
    if proc.returncode != 0:
        _report(False, "obj compile", proc.stderr.strip().splitlines()[-1] if proc.stderr else "compile failed")
        return
    _report(True, "obj compile")

    syms = _exported_text_symbols(obj)
    if syms is not None:
        missing = [s for s in WRAPPER_SYMBOLS if s not in syms]
        _report(not missing, "obj wrapper symbols exported",
                f"missing: {', '.join(missing)}" if missing else "")
        internal = [s for s in syms if s == "w_add2"]
        _report(bool(internal), "obj internal w_ label still present")

    driver = WORK / "driver.c"
    driver.write_text(DRIVER_SOURCE)
    exe = WORK / "sysv_obj_runner"
    link = _run([cc, driver, obj, "-o", exe])
    if link.returncode != 0:
        _report(False, "obj C link", link.stderr.strip()[:200])
        return
    _report(True, "obj C link")
    run = _run([exe])
    _report(run.returncode == 0 and "sysv_wrappers ok" in run.stdout,
            "obj C driver run", (run.stdout + run.stderr).strip()[:200])


def test_static_artifact(cc: str) -> None:
    print("test: static archive")
    src = WORK / "libsysv.sl"
    archive = WORK / "libsysv.a"
    proc = _compile(src, archive, "static", "--sysv-wrappers", "--force")
    if proc.returncode != 0 or not archive.exists():
        _report(False, "static compile", proc.stderr.strip()[:200])
        return
    _report(True, "static compile")

    syms = _exported_text_symbols(archive)
    if syms is not None:
        missing = [s for s in WRAPPER_SYMBOLS if s not in syms]
        _report(not missing, "static wrapper symbols exported",
                f"missing: {', '.join(missing)}" if missing else "")

    driver = WORK / "driver.c"
    exe = WORK / "sysv_static_runner"
    link = _run([cc, driver, archive, "-o", exe])
    if link.returncode != 0:
        _report(False, "static C link", link.stderr.strip()[:200])
        return
    _report(True, "static C link")
    run = _run([exe])
    _report(run.returncode == 0 and "sysv_wrappers ok" in run.stdout,
            "static C driver run", (run.stdout + run.stderr).strip()[:200])


def test_shared_artifact(cc: str) -> None:
    print("test: shared object")
    src = WORK / "libsysv.sl"
    so = WORK / "libsysv.so"
    proc = _compile(src, so, "shared", "--sysv-wrappers", "--force")
    if proc.returncode != 0 or not so.exists():
        _report(False, "shared compile", proc.stderr.strip()[:200])
        return
    _report(True, "shared compile")

    driver = WORK / "driver.c"
    exe = WORK / "sysv_shared_runner"
    link = _run([cc, driver, so, f"-Wl,-rpath,{WORK}", "-o", exe])
    if link.returncode != 0:
        _report(False, "shared C link", link.stderr.strip()[:200])
        return
    _report(True, "shared C link")
    run = _run([exe])
    _report(run.returncode == 0 and "sysv_wrappers ok" in run.stdout,
            "shared C driver run", (run.stdout + run.stderr).strip()[:200])


def test_no_wrappers_by_default() -> None:
    print("test: wrappers off by default")
    syms = None
    nm_available = shutil.which("nm") is not None
    if not nm_available:
        print("  skip  (nm not available)")
        return
    src = WORK / "libsysv.sl"
    obj = WORK / "libsysv_plain.o"
    proc = _compile(src, obj, "obj", "--force")
    if proc.returncode != 0:
        _report(False, "default obj compile", proc.stderr.strip()[:200])
        return
    syms = _exported_text_symbols(obj)
    leaked = [s for s in WRAPPER_SYMBOLS if syms is not None and s in syms]
    _report(not leaked, "no wrapper symbols without flag",
            f"unexpected exports: {', '.join(leaked)}" if leaked else "")


def test_complex_functions(cc: str) -> None:
    print("test: complex functions (recursion, loops, composition)")
    src = WORK / "libcomplex.sl"
    src.write_text(COMPLEX_SOURCE)
    obj = WORK / "libcomplex.o"
    proc = _compile(src, obj, "obj", "--sysv-wrappers", "--force")
    if proc.returncode != 0:
        _report(False, "complex compile", proc.stderr.strip()[:200])
        return
    _report(True, "complex compile")

    syms = _exported_text_symbols(obj)
    if syms is not None:
        missing = [s for s in COMPLEX_SYMBOLS if s not in syms]
        _report(not missing, "complex wrapper symbols exported",
                f"missing: {', '.join(missing)}" if missing else "")

    driver = WORK / "complex_driver.c"
    driver.write_text(COMPLEX_DRIVER)
    exe = WORK / "sysv_complex_runner"
    link = _run([cc, "-g", driver, obj, "-o", exe])
    if link.returncode != 0:
        _report(False, "complex C link", link.stderr.strip()[:200])
        return
    _report(True, "complex C link")
    run = _run([exe])
    _report(run.returncode == 0 and "complex sysv wrappers ok" in run.stdout,
            "complex C driver run", (run.stdout + run.stderr).strip()[:200])


def test_exe_warning() -> None:
    print("test: exe artifact rejection")
    src = WORK / "libsysv.sl"
    out = WORK / "sysv_warn_exe"
    proc = _compile(src, out, "exe", "--sysv-wrappers", "--force")
    combined = proc.stdout + proc.stderr
    _report(
        proc.returncode != 0 and "--c-abi requires --artifact shared, static, or obj" in combined,
        "--artifact exe rejects SysV wrappers",
        combined.strip()[:200],
    )


def test_cache_invalidation() -> None:
    print("test: flag toggling invalidates asm cache")
    if shutil.which("nm") is None:
        print("  skip  (nm not available)")
        return
    cache_dir = WORK / "cache_case"
    cache_dir.mkdir(parents=True, exist_ok=True)
    src = cache_dir / "cached.sl"
    src.write_text(LIB_SOURCE)
    obj = cache_dir / "cached.o"

    # First compile WITHOUT the flag (populates the asm cache).
    first = _compile(src, obj, "obj", temp_dir=cache_dir)
    if first.returncode != 0:
        _report(False, "cache seed compile", first.stderr.strip()[:200])
        return
    # Second compile WITH the flag must not reuse the cached asm.
    second = _compile(src, obj, "obj", "--sysv-wrappers", temp_dir=cache_dir)
    if second.returncode != 0:
        _report(False, "cache flag compile", second.stderr.strip()[:200])
        return
    syms = _exported_text_symbols(obj)
    missing = [s for s in WRAPPER_SYMBOLS if syms is not None and s not in syms]
    _report(not missing, "cache invalidated by --sysv-wrappers",
            f"missing after flag toggle: {', '.join(missing)}" if missing else "")


def main() -> int:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        print("error: no C compiler found (expected one of: cc, gcc, clang)", file=sys.stderr)
        return 2

    WORK.mkdir(parents=True, exist_ok=True)
    # Start from a clean slate so stale artifacts cannot mask failures.
    for stale in WORK.iterdir():
        if stale.is_file():
            stale.unlink()

    test_obj_artifact(cc)
    test_static_artifact(cc)
    test_shared_artifact(cc)
    test_complex_functions(cc)
    test_no_wrappers_by_default()
    test_exe_warning()
    test_cache_invalidation()

    total = _passes + len(_failures)
    print(f"\n{total} checks, {_passes} passed, {len(_failures)} failed")
    if _failures:
        print("failures: " + ", ".join(_failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

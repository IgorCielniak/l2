#!/usr/bin/env python3
"""Check what each build flag and each kind of change actually recompiles,
assembles and links, with and without the compile daemon.

Usage:  python3 tools/check_build_flags.py [--daemon] [--keep]

The tool copies this checkout's tracked files into a temporary directory,
adds fixtures (a stdlib word, a program with a library, a static C library,
a program with compile-time output), and puts logging wrappers for nasm and
ld first on PATH.  Each scenario starts with an empty temp dir.  For every
build it records:

  compiled  the program went through tokenize/parse/emit ("[v1] compilation")
  hit       the whole-assembly build cache was used ("cache hit for")
  nasm/ld   how often the wrappers ran (ld -r of parallel nasm parts excluded)
  out       what the built program prints

and compares with what the flag or change must do.  Exit status 1 when a
check fails.  Takes a few minutes; not part of run_test_matrix.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
KEEP = "--keep" in sys.argv
S = Path(tempfile.mkdtemp(prefix="l2-build-flags-"))
TREE = S / "tree"
W = S / "work"
LOG = S / "tools.log"
CACHE = S / "cache"


def setup() -> None:
    files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.splitlines()
    for rel in sorted(set(files) | {"main.py", "l2_main.py", "l2_client.py", "docs.py"}):
        if rel.split("/", 1)[0] in ("tests", "extra_tests", "examples", "docs"):
            continue
        target = TREE / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, target)
    with open(TREE / "stdlib" / "stdlib.sl", "a") as handle:
        handle.write("\n# check_build_flags fixture\nword stdval 100 end\n")
    W.mkdir()
    (W / "mylib.sl").write_text("word libval 10 end\n")
    (W / "prog.sl").write_text("")
    (W / "prog_ext.sl").write_text("import stdlib.sl\nextern long myval()\nword main myval puti cr 0 end\n")
    (W / "prog_ct.sl").write_text('import stdlib.sl\nword announce "ct-ran" puts cr end\ncompile-time announce\n'
                                  "word main 7 puti cr 0 end\n")
    (W / "myx.c").write_text("long myval(void) { return 5; }\n")
    subprocess.run(["gcc", "-c", "-O2", "-fno-pic", "-o", str(W / "myx.o"), str(W / "myx.c")], check=True)
    subprocess.run(["ar", "rcs", str(W / "libmyx.a"), str(W / "myx.o")], check=True)
    shim = S / "shim"
    shim.mkdir()
    for tool in ("nasm", "ld"):
        real = shutil.which(tool)
        if real is None:
            raise SystemExit(f"{tool} not found")
        path = shim / tool
        path.write_text(f'#!/bin/sh\necho "{tool} $*" >> "$L2T_LOG"\nexec {real} "$@"\n')
        path.chmod(0o755)


try:
    setup()
except BaseException:
    shutil.rmtree(S, ignore_errors=True)
    raise
ENV = dict(os.environ, PATH=f"{S / 'shim'}:{os.environ['PATH']}", L2T_LOG=str(LOG), L2_ASM_CACHE_DIR=str(CACHE))
for name in list(ENV):
    if name.startswith("L2_DAEMON") or name in ("L2_NASM_PARALLEL", "L2_ASM_CACHE", "L2_NO_WARM_PREFIX"):
        ENV.pop(name)
# The sandbox's daemon lives in the sandbox.
ENV["L2_DAEMON_RUNTIME_DIR"] = str(S / "run")
ENV["L2_DAEMON_STATE_DIR"] = str(S / "state")

results: list = []
DAEMON = "--daemon" in sys.argv


def write(path: Path, text: str, *, keep_mtime: bool = False) -> None:
    st = path.stat() if keep_mtime and path.exists() else None
    path.write_text(text)
    if st is not None:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    else:
        time.sleep(0.01)


def build(src: Path, *flags: str, out: Path, temp: Path, extra_env=None) -> dict:
    LOG.write_text("")
    argv = [sys.executable, "main.py", str(src), "-o", str(out), "--temp-dir", str(temp), "--verbose", "1", *flags]
    if not DAEMON:
        argv.append("--no-daemon")
    env = dict(ENV, **(extra_env or {}))
    p = subprocess.run(argv, cwd=TREE, env=env, capture_output=True, text=True, timeout=300)
    tools = [line for line in LOG.read_text().splitlines() if line]
    obs = {
        "rc": p.returncode,
        "compiled": "[v1] compilation:" in p.stdout,
        "hit": "cache hit for" in p.stdout,
        "nasm": sum(1 for t in tools if t.startswith("nasm ")),
        "ld": sum(1 for t in tools if t.startswith("ld ") and " -r " not in t),
        "stdout": p.stdout,
        "stderr": p.stderr,
    }
    if p.returncode == 0 and out.exists() and "--emit-asm" not in flags and "--no-artifact" not in flags \
            and "--check" not in flags and "--artifact" not in flags:
        r = subprocess.run([str(out)], capture_output=True, text=True, timeout=30)
        obs["out"] = r.stdout.strip()
    return obs


def check(scenario: str, step: str, obs: dict, **expected) -> None:
    bad = {k: (v, obs.get(k)) for k, v in expected.items() if obs.get(k) != v}
    if obs["rc"] != 0 and expected.get("rc", 0) == 0:
        bad["rc"] = (0, obs["rc"])
    results.append((scenario, step, not bad, bad, obs))


class Fresh:
    """A scenario: fresh temp dir and output, fixtures reset."""
    count = 0

    def __init__(self, name: str) -> None:
        Fresh.count += 1
        self.name = name
        self.temp = S / "runs" / f"{Fresh.count:02d}" / "build"
        self.out = S / "runs" / f"{Fresh.count:02d}" / "prog.out"
        shutil.rmtree(self.temp.parent, ignore_errors=True)
        self.temp.parent.mkdir(parents=True)
        reset_fixtures()

    def b(self, step: str, *flags: str, src: Path = W / "prog.sl", exe=None, extra_env=None, **expected) -> dict:
        obs = build(src, *flags, out=exe or self.out, temp=self.temp, extra_env=extra_env)
        check(self.name, step, obs, **expected)
        return obs


STD = TREE / "stdlib" / "stdlib.sl"
STD_ORIG = None


def reset_fixtures() -> None:
    global STD_ORIG
    if STD_ORIG is None:
        STD_ORIG = STD.read_text()
    if STD.read_text() != STD_ORIG:
        write(STD, STD_ORIG)
    write(W / "mylib.sl", "word libval 10 end\n") if (W / "mylib.sl").read_text() != "word libval 10 end\n" else None
    prog = ("import stdlib.sl\nimport mylib.sl\nifdef FOO\nword fooval 1000 end\nelsedef\nword fooval 0 end\nendif\n"
            "word main libval fooval + stdval + puti cr 0 end\n")
    if (W / "prog.sl").read_text() != prog:
        write(W / "prog.sl", prog)
    for leftover in (W / "shadow.sl", TREE / "stdlib" / "shadow.sl", W / "incl.sl", W / "link.sl"):
        if leftover.exists() or leftover.is_symlink():
            leftover.unlink()


def shutdown_daemon() -> None:
    subprocess.run([sys.executable, "-c", "import l2_client; l2_client._shutdown_daemon()"], cwd=TREE, env=ENV,
                   capture_output=True)


def run_all() -> None:
    if DAEMON:
        shutdown_daemon()

    s = Fresh("baseline / unchanged rebuild")
    s.b("first build", compiled=True, nasm=1, ld=1, out="110")
    s.b("rebuild, nothing changed", compiled=False, hit=True, nasm=0, ld=0, out="110")
    os.utime(W / "prog.sl")
    s.b("touch source (mtime only)", compiled=False, nasm=0, ld=0, out="110")

    s = Fresh("edits")
    s.b("first build", out="110")
    write(W / "prog.sl", (W / "prog.sl").read_text().replace("stdval +", "stdval + 1 +"))
    s.b("edit program", compiled=True, nasm=1, ld=1, out="111")
    write(W / "mylib.sl", "word libval 20 end\n")
    s.b("edit imported library", compiled=True, nasm=1, ld=1, out="121")
    write(STD, STD.read_text().replace("word stdval 100 end", "word stdval 200 end"))
    s.b("edit stdlib file", compiled=True, nasm=1, ld=1, out="221")

    s = Fresh("same size, mtime preserved")
    s.b("first build", out="110")
    write(W / "mylib.sl", "word libval 30 end\n", keep_mtime=True)
    s.b("library edit keeping size+mtime", compiled=True, out="130")
    s.b("same, with --force", "--force", compiled=True, out="130")

    s = Fresh("--force")
    s.b("first build", out="110")
    s.b("--force, nothing changed", "--force", compiled=True, hit=False, nasm=1, ld=1, out="110")
    s.b("plain rebuild after --force", compiled=False, nasm=0, ld=0, out="110")

    s = Fresh("--no-cache")
    s.b("first build", out="110")
    s.b("--no-cache, nothing changed", "--no-cache", compiled=True, hit=False, nasm=0, ld=0, out="110")
    write(W / "mylib.sl", "word libval 40 end\n")
    s.b("--no-cache after edit", "--no-cache", compiled=True, nasm=1, ld=1, out="140")

    s = Fresh("-D / -I / codegen options")
    s.b("first build", out="110")
    s.b("-D FOO", "-D", "FOO", compiled=True, nasm=1, ld=1, out="1110")
    s.b("-D FOO again", "-D", "FOO", compiled=False, nasm=0, ld=0, out="1110")
    s.b("without -D", compiled=True, nasm=1, ld=1, out="110")
    s.b("-I extra dir", "-I", str(S), compiled=True, out="110")
    for flag in ("--no-stack-check", "--no-loop-unroll", "--no-extern-type-check", "--no-folding", "--no-peephole",
                 "--no-auto-inline", "--no-string-dedup", "--no-asm-opt", "-O0", "-O2"):
        s.b(f"{flag}", flag, compiled=True, out="110")
        s.b(f"{flag} again", flag, compiled=False, nasm=0, ld=0, out="110")
    s.b("-W all", "-W", "all", compiled=True, out="110")
    s.b("--macro-expansion-limit 500", "--macro-expansion-limit", "500", compiled=True, out="110")

    s = Fresh("--debug")
    s.b("first build", out="110")
    o = s.b("--debug", "--debug", compiled=True, nasm=1, ld=1, out="110")
    results.append(("--debug", "nasm got -g", "-g" in (LOG.read_text()), {}, o))
    s.b("--debug again", "--debug", compiled=False, nasm=0, ld=0, out="110")
    s.b("back to normal", compiled=True, nasm=1, ld=1, out="110")

    s = Fresh("deleted outputs")
    s.b("first build", out="110")
    s.out.unlink()
    s.b("output deleted", compiled=False, nasm=0, ld=1, out="110")
    obj = next(s.temp.glob("prog.o"))
    obj.unlink()
    s.b(".o deleted", compiled=False, nasm=1, ld=1, out="110")
    next(s.temp.glob("prog.asm")).unlink()
    s.b(".asm deleted", compiled=False, nasm=1, ld=1, out="110")

    s = Fresh("output paths")
    s.b("first build", out="110")
    other = s.temp.parent / "other.out"
    s.b("different -o", exe=other, compiled=False, nasm=0, ld=1)
    check("output paths", "different -o runs", {"rc": 0, "out": subprocess.run([str(other)], capture_output=True, text=True).stdout.strip()}, out="110")
    s.b("two programs, same -o (ct prog)", src=W / "prog_ct.sl", compiled=True, nasm=1, ld=1, out="7")
    s.b("two programs, same -o (back)", compiled=False, nasm=0, ld=1, out="110")

    s = Fresh("partial builds then full build")
    s.b("first build", out="110")
    write(W / "mylib.sl", "word libval 50 end\n")
    s.b("--no-artifact after edit", "--no-artifact", compiled=True, nasm=0, ld=0)
    s.b("full build after --no-artifact", compiled=False, nasm=1, ld=1, out="150")
    write(W / "mylib.sl", "word libval 60 end\n")
    s.b("--emit-asm after edit", "--emit-asm", exe=s.temp.parent / "x.asm", compiled=True, nasm=0, ld=0)
    s.b("full build after --emit-asm", nasm=1, ld=1, out="160")
    write(W / "mylib.sl", "word libval 70 end\n")
    s.b("--check after edit", "--check", compiled=True, nasm=0, ld=0)
    s.b("full build after --check", nasm=1, ld=1, out="170")

    s = Fresh("linked library")
    lib = W / "libmyx.a"
    s.b("first build", "-l", str(lib), src=W / "prog_ext.sl", nasm=1, ld=1, out="5")
    s.b("rebuild", "-l", str(lib), src=W / "prog_ext.sl", compiled=False, nasm=0, ld=0, out="5")
    (W / "myx.c").write_text("long myval(void) { return 6; }\n")
    subprocess.run(["gcc", "-c", "-O2", "-fno-pic", "-o", str(W / "myx.o"), str(W / "myx.c")], check=True)
    subprocess.run(["ar", "rcs", str(lib), str(W / "myx.o")], check=True)
    s.b("library file changed", "-l", str(lib), src=W / "prog_ext.sl", compiled=False, nasm=0, ld=1, out="6")
    (W / "myx.c").write_text("long myval(void) { return 5; }\n")
    subprocess.run(["gcc", "-c", "-O2", "-fno-pic", "-o", str(W / "myx.o"), str(W / "myx.c")], check=True)
    subprocess.run(["ar", "rcs", str(lib), str(W / "myx.o")], check=True)

    s = Fresh("compile-time side effects")
    o1 = s.b("first build", src=W / "prog_ct.sl", compiled=True, out="7")
    o2 = s.b("rebuild re-runs CT code", src=W / "prog_ct.sl", compiled=True, hit=False, nasm=0, ld=0, out="7")
    results.append(("compile-time side effects", "CT output each build",
                    "ct-ran" in o1["stdout"] and "ct-ran" in o2["stdout"], {}, o2))

    s = Fresh("--clean")
    s.b("first build", out="110")
    subprocess.run([sys.executable, "main.py", "--clean", "--temp-dir", str(s.temp)], cwd=TREE, env=ENV,
                   capture_output=True)
    check("--clean", "temp dir removed", {"rc": 0, "gone": not s.temp.exists()}, gone=True)
    s.b("build after --clean", compiled=True, nasm=1, ld=1, out="110")

    s = Fresh("import shadowed by a new file")
    (TREE / "stdlib" / "shadow.sl").write_text("word shadowval 1 end\n")
    write(W / "prog.sl", "import stdlib.sl\nimport shadow.sl\nword main shadowval puti cr 0 end\n")
    s.b("first build (stdlib/shadow.sl)", compiled=True, out="1")
    (W / "shadow.sl").write_text("word shadowval 2 end\n")
    s.b("new shadow.sl next to the program", compiled=True, out="2")

    s = Fresh("compiler changed")
    s.b("first build", out="110")
    os.utime(TREE / "l2_main.py")
    s.b("compiler file touched", compiled=True, hit=False, out="110")


    # ---- second round ----
    s = Fresh("shadowing file removed")
    (TREE / "stdlib" / "shadow.sl").write_text("word shadowval 1 end\n")
    (W / "shadow.sl").write_text("word shadowval 2 end\n")
    write(W / "prog.sl", "import stdlib.sl\nimport shadow.sl\nword main shadowval puti cr 0 end\n")
    s.b("first build (shadow next to program)", compiled=True, out="2")
    (W / "shadow.sl").unlink()
    s.b("shadow removed", compiled=True, out="1")

    s = Fresh("shadowing an -I import")
    inc = S / "incdir"
    inc.mkdir(exist_ok=True)
    (inc / "incl.sl").write_text("word inclval 3 end\n")
    write(W / "prog.sl", "import stdlib.sl\nimport incl.sl\nword main inclval puti cr 0 end\n")
    s.b("first build (-I)", "-I", str(inc), compiled=True, out="3")
    (W / "incl.sl").write_text("word inclval 4 end\n")
    s.b("new incl.sl next to the program", "-I", str(inc), compiled=True, out="4")
    (W / "incl.sl").unlink()

    s = Fresh("graph cache (CT program, no build cache)")
    write(W / "prog_ct2.sl", "import stdlib.sl\nimport mylib.sl\nword announce \"ct\" puts cr end\n"
          "compile-time announce\nword main libval puti cr 0 end\n")
    s.b("first build", src=W / "prog_ct2.sl", compiled=True, out="10")
    write(W / "mylib.sl", "word libval 11 end\n", keep_mtime=True)
    s.b("library edit keeping size+mtime", src=W / "prog_ct2.sl", compiled=True, out="11")
    (TREE / "stdlib" / "shadow.sl").write_text("word shadowval 1 end\n")
    write(W / "prog_ct2.sl", "import stdlib.sl\nimport shadow.sl\nword announce \"ct\" puts cr end\n"
          "compile-time announce\nword main shadowval puti cr 0 end\n")
    s.b("import from stdlib dir", src=W / "prog_ct2.sl", compiled=True, out="1")
    (W / "shadow.sl").write_text("word shadowval 2 end\n")
    s.b("shadowed by new file", src=W / "prog_ct2.sl", compiled=True, out="2")

    s = Fresh("deleted import")
    s.b("first build", out="110")
    saved = (W / "mylib.sl").read_text()
    (W / "mylib.sl").unlink()
    o = s.b("import file deleted", rc=1)
    results.append(("deleted import", "reports the missing import", "mylib.sl" in o["stderr"], {}, o))
    (W / "mylib.sl").write_text(saved)
    # Same content again: the hash check reuses the cached build.
    s.b("import restored (same content)", nasm=0, out="110")

    s = Fresh("symlinked import retargeted")
    (W / "alt_a.sl").write_text("word libval 21 end\n")
    (W / "alt_b.sl").write_text("word libval 22 end\n")
    link = W / "link.sl"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(W / "alt_a.sl")
    write(W / "prog.sl", "import stdlib.sl\nimport link.sl\nword main libval puti cr 0 end\n")
    s.b("first build (link -> alt_a)", compiled=True, out="21")
    link.unlink()
    link.symlink_to(W / "alt_b.sl")
    s.b("link retargeted to alt_b", compiled=True, out="22")
    link.unlink()

    s = Fresh("-I order")
    d1, d2 = S / "inc1", S / "inc2"
    for d, v in ((d1, 31), (d2, 32)):
        d.mkdir(exist_ok=True)
        (d / "ord.sl").write_text(f"word ordval {v} end\n")
    write(W / "prog.sl", "import stdlib.sl\nimport ord.sl\nword main ordval puti cr 0 end\n")
    s.b("-I inc1 -I inc2", "-I", str(d1), "-I", str(d2), out="31")
    s.b("-I inc2 -I inc1", "-I", str(d2), "-I", str(d1), compiled=True, out="32")

    s = Fresh("same file name, two directories, one temp dir")
    for name, v in (("a", 41), ("b", 42)):
        (W / name).mkdir(exist_ok=True)
        (W / name / "prog.sl").write_text(f"import stdlib.sl\nword main {v} puti cr 0 end\n")
    ea, eb = s.temp.parent / "a.out", s.temp.parent / "b.out"
    s.b("build a", src=W / "a" / "prog.sl", exe=ea, out="41")
    s.b("build b", src=W / "b" / "prog.sl", exe=eb, out="42")
    s.b("build a again", src=W / "a" / "prog.sl", exe=ea, out="41")
    s.b("build b again", src=W / "b" / "prog.sl", exe=eb, out="42")

    s = Fresh("artifact kinds")
    s.b("exe", out="110")
    for kind, suffix in (("obj", ".o"), ("static", ".a"), ("shared", ".so")):
        target = s.temp.parent / f"lib{kind}{suffix}"
        o = s.b(f"--artifact {kind}", "--artifact", kind, exe=target)
        kind_ok = target.exists() and subprocess.run(["file", str(target)], capture_output=True, text=True).stdout
        expect = {"obj": "relocatable", "static": "archive", "shared": "shared object"}[kind]
        results.append(("artifact kinds", f"{kind} file is a {expect}", bool(kind_ok) and expect in kind_ok, {}, o))
    s.b("exe again", out="110")

    s = Fresh("large program, parallel nasm")
    big = TREE / "tools" / "bench" / "large_generated.sl"
    o = s.b("first build (split)", src=big, compiled=True, ld=1)
    results.append(("large program, parallel nasm", "assembled in parts", o["nasm"] >= 2, {"nasm": (">=2", o["nasm"])}, o))
    s.b("rebuild: same asm, no nasm or ld", src=big, nasm=0, ld=0)
    serial = Fresh("large program, one nasm")
    o3 = serial.b("build with L2_NASM_PARALLEL=0", src=big, extra_env={"L2_NASM_PARALLEL": "0"}, nasm=1, ld=1)
    results.append(("large program, one nasm", "same output as the split build",
                    o.get("out") is not None and o.get("out") == o3.get("out"), {}, o3))

    if DAEMON:
        s = Fresh("daemon: stdlib prefix")
        s.b("first build", out="110")
        write(STD, STD.read_text().replace("word stdval 100 end", "word stdval 300 end"), keep_mtime=True)
        s.b("stdlib edit keeping size+mtime, --force", "--force", compiled=True, out="310")
        s.b("same, --force --force-stdlib", "--force", "--force-stdlib", compiled=True, out="310")
        write(STD, STD.read_text().replace("word stdval 300 end", "word stdval 400 end"))
        s.b("stdlib edit (new mtime)", compiled=True, out="410")

        s = Fresh("daemon: trusted dir prefix")
        shutdown_daemon()
        env = {"L2_DAEMON_TRUSTED_DIRS": str(W)}
        s.b("first build", extra_env=env, out="110")
        s.b("second build", extra_env=env, out="110")
        write(W / "mylib.sl", "word libval 90 end\n")
        s.b("trusted library edited", "--force", extra_env=env, compiled=True, out="190")
        shutdown_daemon()

        s = Fresh("daemon: --force-stdlib alone")
        s.b("first build", out="110")
        s.b("--force-stdlib, nothing changed", "--force-stdlib", compiled=False, nasm=0, ld=0, out="110")
        write(STD, STD.read_text().replace("word stdval 100 end", "word stdval 500 end"), keep_mtime=True)
        s.b("stdlib edit keeping size+mtime, plain build", compiled=True, out="510")

        s = Fresh("daemon: trusted dir, same-size edit")
        shutdown_daemon()
        env = {"L2_DAEMON_TRUSTED_DIRS": str(W)}
        s.b("first build", extra_env=env, out="110")
        write(W / "mylib.sl", "word libval 70 end\n", keep_mtime=True)
        s.b("trusted library edit keeping size+mtime", extra_env=env, compiled=True, out="170")
        shutdown_daemon()

    if DAEMON:
        shutdown_daemon()


def report() -> int:
    failed = 0
    current = None
    for scenario, step, ok, bad, obs in results:
        if scenario != current:
            print(f"\n## {scenario}")
            current = scenario
        mark = "PASS" if ok else "FAIL"
        detail = ""
        if not ok:
            failed += 1
            detail = "  " + ", ".join(f"{k}: expected {v[0]!r}, got {v[1]!r}" for k, v in bad.items())
            if obs.get("rc"):
                detail += "\n      " + (obs.get("stderr") or "")[-300:].replace("\n", "\n      ")
        print(f"  [{mark}] {step}{detail}")
    print(f"\n{len(results) - failed}/{len(results)} checks passed ({'daemon' if DAEMON else 'no daemon'})")
    return failed


if __name__ == "__main__":
    try:
        run_all()
    finally:
        if DAEMON:
            shutdown_daemon()
        if not KEEP:
            shutil.rmtree(S, ignore_errors=True)
        else:
            print(f"kept {S}")
    sys.exit(1 if report() else 0)

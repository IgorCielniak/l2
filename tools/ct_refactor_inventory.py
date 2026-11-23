#!/usr/bin/env python3
"""Inventory of compile-time words affected by the CT API refactor.

For every compile-time word registered by the compiler (plus the template
directives being retired), this tool records the planned disposition from
`groups/` (keep, rename, or retire) and counts where each *old* name is still
used in git-tracked files. It also lists pure alias wrappers in
`stdlib/meta.sl` and `libs/fn.sl` and reports registered words that have no
recorded disposition.

Usage:
    python3 tools/ct_refactor_inventory.py            # summary to stdout
    python3 tools/ct_refactor_inventory.py --markdown groups/inventory-baseline.md
    python3 tools/ct_refactor_inventory.py --check    # exit 1 on unclassified words

Renamed and retired names should have zero uses outside the compiler and
migration notes.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Dispositions. Each entry: old name -> (group, final name or None if retired)
# A final name equal to the old name means "keep".
# ---------------------------------------------------------------------------

RENAMES: Dict[str, Tuple[str, str]] = {
    # Data and value utilities
    "map-update": ("1", "map-merge"),
    "string=": ("1", "string-equal?"),
    "string>number": ("1", "try-string>int"),
    # Macro authoring and templates
    "ct-get-macro-expansion-limit": ("2", "ct-expansion-get-depth-limit"),
    "ct-set-macro-expansion-limit": ("2", "ct-expansion-set-depth-limit"),
    "ct-gensym": ("2", "gensym"),
    # Lexing and parser input
    "next-token": ("3", "ct-parser-consume"),
    "inject-tokens": ("3", "ct-parser-inject"),
    "ct-current-token": ("3", "ct-parser-current"),
    "ct-parser-pos": ("3", "ct-parser-get-pos"),
    "ct-parser-diff": ("3", "ct-parser-slice"),
    # Syntax transformations and language packs
    "set-token-hook": ("4", "ct-parser-set-token-hook"),
    "clear-token-hook": ("4", "ct-parser-clear-token-hook"),
    "ct-lang-meta-get": ("4", "ct-lang-get-meta"),
    "ct-lang-meta-set": ("4", "ct-lang-set-meta"),
    "ct-lang-active-list": ("4", "ct-lang-list-active"),
    "ct-add-reader-rewrite": ("4", "ct-rewrite-add-reader"),
    "ct-add-reader-rewrite-named": ("4", "ct-rewrite-add-reader-named"),
    "ct-add-grammar-rewrite": ("4", "ct-rewrite-add-grammar"),
    "ct-add-grammar-rewrite-named": ("4", "ct-rewrite-add-grammar-named"),
    "ct-remove-reader-rewrite": ("4", "ct-rewrite-remove"),
    "ct-remove-grammar-rewrite": ("4", "ct-rewrite-remove"),
    "ct-clear-reader-rewrites": ("4", "ct-rewrite-clear-reader"),
    "ct-clear-grammar-rewrites": ("4", "ct-rewrite-clear-grammar"),
    "ct-list-reader-rewrites": ("4", "ct-rewrite-list-reader"),
    "ct-list-grammar-rewrites": ("4", "ct-rewrite-list-grammar"),
    "ct-set-rewrite-loop-detection": ("4", "ct-rewrite-set-loop-detection"),
    "ct-get-rewrite-loop-detection": ("4", "ct-rewrite-loop-detection?"),
    "ct-get-rewrite-loop-reports": ("4", "ct-rewrite-get-loop-reports"),
    "ct-clear-rewrite-loop-reports": ("4", "ct-rewrite-clear-loop-reports"),
    "ct-set-rewrite-max-steps": ("4", "ct-rewrite-set-max-steps"),
    "ct-get-rewrite-max-steps": ("4", "ct-rewrite-get-max-steps"),
    "ct-set-rewrite-pipeline-active": ("4", "ct-rewrite-set-pipeline-active"),
    "ct-list-rewrite-active-pipelines": ("4", "ct-rewrite-list-active-pipelines"),
    # Definition lifecycle and priority
    "ct-unregister-word": ("5", "ct-definition-unregister-by-name"),
    "ct-word-exists?": ("5", "ct-definition-exists?"),
    "ct-list-words": ("5", "ct-definition-list-names"),
    "ct-list-words-prefix": ("5", "ct-definition-list-names-prefix"),
    "ct-get-word-body": ("5", "ct-definition-body-by-name"),
    "ct-get-word-asm": ("5", "ct-definition-asm-by-name"),
    "ct-predef-all": ("5", "ct-definition-predeclare-all"),
    "ct-error-define": ("5", "ct-definition-register-source"),
    "use-l2-ct": ("5", "ct-definition-prefer-l2-by-name"),
    # Assembly output buffers
    "prelude-append": ("6", "ct-output-append-text-preamble"),
    "prelude-set": ("6", "ct-output-set-text-preamble"),
    "prelude-clear": ("6", "ct-output-clear-text-preamble"),
    "data-append": ("6", "ct-output-append-data"),
    "data-set": ("6", "ct-output-set-data"),
    "data-clear": ("6", "ct-output-clear-data"),
    "bss-append": ("6", "ct-output-append-bss"),
    "bss-set": ("6", "ct-output-set-bss"),
    "bss-clear": ("6", "ct-output-clear-bss"),
    # CT call and randomness
    "ct-ctrand-seed": ("7", "ct-rng-seed"),
    "ct-ctrand-int": ("7", "ct-rng-int"),
    "ct-ctrand-range": ("7", "ct-rng-range"),
    # Events, diagnostics and recovery
    "event-subscribe": ("8", "ct-event-subscribe"),
    "event-unsubscribe": ("8", "ct-event-unsubscribe"),
    "event-emit": ("8", "ct-event-emit"),
    "event-object-emit": ("8", "ct-event-emit-object"),
    "event-object-create": ("8", "event-new"),
    "event-object-prop-append": ("8", "event-payload-set"),
    "event-object-prop-get": ("8", "event-payload-get"),
    "get-event-type": ("8", "event-name"),
    "get-event-name": ("8", "event-name"),
    "get-event-timestamp": ("8", "event-timestamp"),
    "get-event-payload": ("8", "event-payload"),
    "ct-error-restore-points": ("8", "ct-recovery-list-restore-points"),
    "ct-error-restore-point": ("8", "ct-recovery-get-restore-point"),
    "ct-error-jump": ("8", "ct-recovery-jump"),
    "ct-error-resume": ("8", "ct-recovery-resume"),
    "ct-error-get-retry-limit": ("8", "ct-recovery-get-retry-limit"),
    "ct-error-set-retry-limit": ("8", "ct-recovery-set-retry-limit"),
    "parse-error": ("8", "ct-parser-error"),
    # Compiler control and operation emission
    "ct-emit-op": ("9", "ct-op-emit"),
    "ct-new-label": ("9", "ct-label-new"),
    "ct-register-block-opener": ("9", "ct-parser-register-block-opener"),
    "ct-unregister-block-opener": ("9", "ct-parser-unregister-block-opener"),
    "ct-register-control-override": ("9", "ct-control-register-override"),
    "ct-unregister-control-override": ("9", "ct-control-unregister-override"),
    "ct-last-token-line": ("9", "ct-parser-last-line"),
    # Assertions, loop and reflection helpers
    "static_assert": ("10", "static-assert"),
    "ct-repr": ("10", "repr"),
}

_CAPTURE = [
    "args", "assert-shape", "clone", "coerce-number", "coerce-string",
    "coerce-tokens", "compress", "count", "decompress", "deserialize", "diff",
    "equal?", "filter", "freeze", "get", "global-clear", "global-delete",
    "global-get", "globals", "global-set", "has?", "hash", "join", "lifetime",
    "lifetime-assert", "lifetime-live?", "lint", "locals", "map", "mutable?",
    "normalize", "origin", "pretty", "replay-clear", "replay-log",
    "schema-get", "schema-put", "schema-validate", "separate", "serialize",
    "shape", "slice", "tainted?", "taint-get", "taint-set", "thaw",
]
_CT_CALL = [
    "ct-set-ct-call-contract", "ct-get-ct-call-contract",
    "ct-set-ct-call-exception-policy", "ct-get-ct-call-exception-policy",
    "ct-set-ct-call-sandbox-mode", "ct-get-ct-call-sandbox-mode",
    "ct-set-ct-call-sandbox-allowlist", "ct-get-ct-call-sandbox-allowlist",
    "ct-set-ct-call-memo", "ct-get-ct-call-memo", "ct-clear-ct-call-memo",
    "ct-get-ct-call-memo-size", "ct-set-ct-call-side-effects",
    "ct-get-ct-call-side-effects", "ct-get-ct-call-side-effect-log",
    "ct-clear-ct-call-side-effect-log", "ct-set-ct-call-recursion-limit",
    "ct-get-ct-call-recursion-limit", "ct-set-ct-call-timeout-ms",
    "ct-get-ct-call-timeout-ms",
]
TEMPLATE_DIRECTIVES = [
    "ct-break", "ct-call", "ct-case", "ct-comment", "ct-continue",
    "ct-default", "ct-each", "ct-emit-block", "ct-emit-list", "ct-endcomment",
    "ct-error", "ct-fn", "ct-fold", "ct-for", "ct-if", "ct-import",
    "ct-include", "ct-let", "ct-match", "ct-note", "ct-permissive",
    "ct-strict", "ct-switch", "ct-unless", "ct-version", "ct-warning",
    "ct-when", "emit-block", "emit-list",
]

RETIRED: Dict[str, str] = {}
def _retire(group: str, names: List[str]) -> None:
    for name in names:
        RETIRED[name] = group

_retire("1", ["list-join"])
_retire("2", [f"ct-capture-{s}" for s in _CAPTURE])
_retire("2", [
    "ct-clone-macro", "ct-get-macro-expansion", "ct-get-macro-preview",
    "ct-get-macro-signature", "ct-get-macro-template-mode",
    "ct-get-macro-template-program-size", "ct-get-macro-template-version",
    "ct-register-pattern-macro", "ct-register-text-macro",
    "ct-register-text-macro-signature", "ct-rename-macro",
    "ct-set-macro-expansion", "ct-set-macro-preview",
    "ct-unregister-pattern-macro", "ct-word-is-pattern-macro",
    "ct-word-is-text-macro", "ct-macro-attrs-get", "ct-macro-attrs-set",
    "ct-macro-doc-get", "ct-macro-doc-set",
    "ct-list-pattern-macros", "ct-set-pattern-macro-enabled",
    "ct-get-pattern-macro-enabled", "ct-set-pattern-macro-priority",
    "ct-get-pattern-macro-priority", "ct-set-pattern-macro-group",
    "ct-get-pattern-macro-group", "ct-set-pattern-macro-scope",
    "ct-get-pattern-macro-scope", "ct-get-pattern-macro-clauses",
    "ct-get-pattern-macro-clause-details", "ct-set-pattern-macro-clause-guard",
    "ct-detect-pattern-conflicts", "ct-detect-pattern-conflicts-named",
    "ct-lang-register-pattern-macro", "ct-lang-register-text-macro-signature",
])
_retire("2", TEMPLATE_DIRECTIVES)
_retire("3", [
    "add-token", "add-token-chars", "ct-parser-mark", "inject-lexemes",
    "token-clone", "token-from-lexeme", "token-shift-column", "peek-token",
    "lexer-new", "lexer-pop", "lexer-peek", "lexer-expect",
    "lexer-collect-brace", "lexer-push-back", "emit-definition",
])
_retire("4", [
    "ct-lang-set-token-hook", "ct-lang-add-validator", "ct-lang-run-validators",
    "ct-lang-run-active-validators", "ct-lang-set-auto-validate",
    "ct-lang-get-auto-validate",
    "ct-add-reader-rewrite-priority", "ct-add-grammar-rewrite-priority",
    "ct-set-reader-rewrite-enabled", "ct-get-reader-rewrite-enabled",
    "ct-set-reader-rewrite-priority", "ct-get-reader-rewrite-priority",
    "ct-set-grammar-rewrite-enabled", "ct-get-grammar-rewrite-enabled",
    "ct-set-grammar-rewrite-priority", "ct-get-grammar-rewrite-priority",
    "ct-rebuild-rewrite-index", "ct-get-rewrite-index-stats",
    "ct-get-rewrite-profile", "ct-clear-rewrite-profile",
    "ct-set-rewrite-saturation", "ct-get-rewrite-saturation",
    "ct-get-rewrite-specificity", "ct-get-rewrite-provenance",
    "ct-rewrite-generate-fixture", "ct-rewrite-compatibility-matrix",
    "ct-rewrite-txn-begin", "ct-rewrite-txn-commit", "ct-rewrite-txn-rollback",
    "ct-export-rewrite-pack", "ct-import-rewrite-pack",
    "ct-import-rewrite-pack-replace",
    "ct-set-pattern-group-active", "ct-set-pattern-scope-active",
    "ct-list-active-pattern-groups", "ct-list-active-pattern-scopes",
    "ct-set-rewrite-pipeline", "ct-get-rewrite-pipeline",
    "ct-set-rewrite-trace", "ct-get-rewrite-trace", "ct-get-rewrite-trace-log",
    "ct-clear-rewrite-trace-log",
])
_retire("7", _CT_CALL)
_retire("10", ["ct-runtime-get-source", "shunt"])

# Words kept under their current name, by owning group.
KEEP: Dict[str, str] = {}
def _keep(group: str, names: List[str]) -> None:
    for name in names:
        KEEP[name] = group

_keep("1", [
    "nil", "nil?", "identifier?", "int>string",
    # added by the CT API refactor
    "list-deep-clone", "map-merge", "string-equal?", "try-string>int",
    "list-append", "list-clear", "list-clone", "list-contains?", "list-empty?",
    "list-extend", "list-find", "list-get", "list-insert", "list-last",
    "list-length", "list-new", "list-peek-front", "list-pop", "list-pop-front",
    "list-push-front", "list-remove", "list-reverse", "list-set", "list-slice",
    "map-clear", "map-clone", "map-delete", "map-empty?", "map-get", "map-has?",
    "map-keys", "map-length", "map-new", "map-set", "map-values",
    "string-append", "string-contains?", "string-ends-with?", "string-join",
    "string-length", "string-lower", "string-replace", "string-split",
    "string-starts-with?", "string-strip", "string-upper",
])
_keep("2", [
    # added by the CT API refactor
    "gensym", "ct-expansion-get-depth-limit", "ct-expansion-set-depth-limit",
    "syntax-length", "syntax-get", "syntax-slice", "syntax-concat", "syntax>list", "list>syntax", "emit",
    # added by the CT API refactor
    "ct-macro-get", "ct-macro-get-by-name", "ct-macro-set", "ct-macro-set-by-name", "ct-macro-context",
])
_keep("3", [
    "ct-parser-checkpoint", "ct-parser-collect-balanced", "ct-parser-collect-until",
    "ct-parser-eof?", "ct-parser-expected", "ct-parser-peek", "ct-parser-remaining",
    "ct-parser-restore", "ct-parser-session-begin", "ct-parser-session-commit",
    "ct-parser-session-rollback", "ct-parser-set-pos", "ct-parser-tail",
    "token-column", "token-lexeme", "token-line", "token-with-lexeme",
    "token-slice", "lexeme", "token?", "ct-definition-set-effect", "ct-definition-effect",
    # added by the CT API refactor
    "token-from-origin", "token-origin", "token-expansion-chain",
    "source-origin-path", "source-origin-start", "source-origin-end",
    "source-origin-start-line", "source-origin-start-column",
    "source-origin-end-line", "source-origin-end-column",
    "expansion-frame-kind", "expansion-frame-name",
    "expansion-frame-call-site", "expansion-frame-definition-origin",
    # added by the CT API refactor
    "ct-parser-consume", "ct-parser-current", "ct-parser-get-pos",
    "ct-parser-inject", "ct-parser-slice",
    # added by the CT API refactor
    "ct-tokenizer-get-config", "ct-tokenizer-set-config", "ct-tokenizer-run",
    "source-cursor-new", "source-cursor-peek", "source-cursor-read",
    "source-cursor-eof?", "source-cursor-pos", "source-cursor-set-pos",
    "source-cursor-slice", "source-cursor-span",
])
_keep("4", [
    # added by the CT API refactor
    "ct-lang-register-macro",
    "ct-lang-activate", "ct-lang-active?", "ct-lang-add-grammar-rewrite-named",
    "ct-lang-add-reader-rewrite-named", "ct-lang-create", "ct-lang-deactivate",
    "ct-lang-exists?", "ct-lang-list", "ct-lang-remove", "ct-lang-status",
    "ct-rewrite-dry-run", "ct-rewrite-run-on-list", "ct-rewrite-scope-push",
    "ct-rewrite-scope-pop",
    # added by the CT API refactor
    "ct-rewrite-add-reader", "ct-rewrite-add-reader-named", "ct-rewrite-add-grammar",
    "ct-rewrite-add-grammar-named", "ct-rewrite-remove", "ct-rewrite-clear-reader",
    "ct-rewrite-clear-grammar", "ct-rewrite-list-reader", "ct-rewrite-list-grammar",
    "ct-rewrite-set-loop-detection", "ct-rewrite-loop-detection?", "ct-rewrite-get-loop-reports",
    "ct-rewrite-clear-loop-reports", "ct-rewrite-set-max-steps", "ct-rewrite-get-max-steps",
    "ct-rewrite-set-pipeline-active", "ct-rewrite-list-active-pipelines",
    "ct-rewrite-rule-set-priority", "ct-rewrite-rule-get-priority", "ct-rewrite-rule-set-enabled",
    "ct-rewrite-rule-get-enabled", "ct-rewrite-rule-set-pipeline", "ct-rewrite-rule-get-pipeline",
    "ct-rewrite-rule-set-guard", "ct-rewrite-rule-get-guard",
    # added by the CT API refactor
    "ct-parser-set-token-hook", "ct-parser-clear-token-hook", "ct-lang-list-active",
    "ct-lang-get-meta", "ct-lang-set-meta", "ct-lang-add-token-hook", "ct-lang-set-token-hook-priority",
    "ct-lang-set-token-hook-enabled", "ct-lang-remove-token-hook", "ct-lang-register-word",
    "ct-lang-set-resource-priority", "ct-lang-get-resource-priority", "ct-lang-unregister-resource",
])
_keep("5", [
    # added by the CT API refactor
    "ct-definition-candidates", "ct-definition-exists?", "ct-definition-list-names",
    "ct-definition-list-names-prefix", "ct-definition-get", "ct-definition-get-by-name",
    "ct-definition-body", "ct-definition-body-by-name", "ct-definition-asm",
    "ct-definition-asm-by-name", "ct-definition-source", "ct-definition-source-by-name",
    "ct-definition-set-priority", "ct-definition-set-priority-by-name",
    "ct-definition-set-flags", "ct-definition-set-flags-by-name",
    "ct-definition-set-asm", "ct-definition-set-asm-by-name",
    "ct-definition-rename", "ct-definition-rename-by-name",
    "ct-definition-clone", "ct-definition-clone-by-name",
    "ct-definition-unregister", "ct-definition-unregister-by-name",
    "ct-definition-prefer-l2", "ct-definition-prefer-l2-by-name",
    "ct-definition-register-source", "ct-definition-predeclare-all",
    # added by the CT API refactor
    "ct-definition-set-body", "ct-definition-set-body-by-name",
])
_keep("6", [
    # added by the CT API refactor
    "ct-output-append-text-preamble", "ct-output-set-text-preamble", "ct-output-clear-text-preamble",
    "ct-output-append-data", "ct-output-set-data", "ct-output-clear-data",
    "ct-output-append-bss", "ct-output-set-bss", "ct-output-clear-bss",
])
_keep("7", [
    # added by the CT API refactor
    "ct-rng-seed", "ct-rng-int", "ct-rng-range",
])
_keep("8", [
    # added by the CT API refactor
    "ct-parser-error", "ct-event-stream-enabled?", "ct-event-stream-set-enabled",
    # added by the CT API refactor
    "diagnostic-new", "diagnostic-severity", "diagnostic-code", "diagnostic-category",
    "diagnostic-phase", "diagnostic-message", "diagnostic-primary", "diagnostic-related",
    "ct-diagnostic-raise", "ct-diagnostic-list", "ct-diagnostic-error",
    "ct-diagnostic-warning", "ct-diagnostic-note",
    # added after the CT API refactor
    "ct-parser-error-at", "ct-diagnostic-error-at",
    # added by the CT API refactor
    "ct-event-subscribe", "ct-event-unsubscribe", "ct-event-emit", "ct-event-emit-object",
    "event-new", "event-payload-set", "event-payload-get", "event-name", "event-timestamp",
    "event-payload", "ct-event-enabled?", "ct-event-set-enabled",
    # added by the CT API refactor
    "ct-recovery-list-restore-points", "ct-recovery-get-restore-point", "ct-recovery-jump",
    "ct-recovery-resume", "ct-recovery-get-retry-limit", "ct-recovery-set-retry-limit",
])
_keep("9", [
    "ct-control-add-close-op", "ct-control-depth", "ct-control-frame-new",
    "ct-control-get", "ct-control-get?", "ct-control-peek", "ct-control-pop", "ct-control-push",
    "ct-control-set",
    # added by the CT API refactor
    "ct-op-emit", "ct-label-new", "ct-parser-last-line", "ct-parser-register-block-opener",
    "ct-parser-unregister-block-opener", "ct-control-register-override", "ct-control-unregister-override",
])
_keep("10", ["i", "CT", "repr", "static-assert"])
_keep("1", ["string-utf8-length"])  # added by the CT API refactor

# ---------------------------------------------------------------------------

AREAS = [
    ("stdlib", lambda p: p.startswith("stdlib/")),
    ("libs", lambda p: p.startswith("libs/")),
    ("tests", lambda p: p.startswith("tests/") or p.startswith("extra_tests/")),
    ("examples", lambda p: p.startswith("examples/")),
    ("docs.py", lambda p: p == "docs.py"),
    ("readme", lambda p: p == "README.md"),
    ("compiler", lambda p: p == "l2_main.py"),
    ("tools", lambda p: p.startswith("tools/") or p.endswith(".py")),
]
NAME_CHARS = r"A-Za-z0-9_?!>=<*+/.\-"


def tracked_files() -> List[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    files = []
    for line in out.splitlines():
        if line.startswith("groups/") or line.endswith(
                (".png", ".jpg", ".gz", ".so", ".a", ".o", ".tbl")):
            continue
        files.append(line)
    return files


def area_of(path: str) -> str:
    for name, pred in AREAS:
        if pred(path):
            return name
    return "other"


def registered_words() -> List[str]:
    """Every word with a compile-time intrinsic in a fresh compiler
    dictionary (read from the compiler itself, not by scanning source)."""
    sys.path.insert(0, str(ROOT))
    import l2_main  # noqa: E402

    dictionary = l2_main.bootstrap_dictionary()
    names = {name for name, word in dictionary.words.items()
             if l2_main.is_ct_api_word(name, word)}
    names.add("CT")
    return sorted(names)


def disposition(name: str) -> Tuple[Optional[str], str, Optional[str]]:
    """Return (group, action, final-name)."""
    if name in RENAMES:
        group, final = RENAMES[name]
        return group, "rename", final
    if name in RETIRED:
        return RETIRED[name], "retire", None
    if name in KEEP:
        return KEEP[name], "keep", name
    return None, "unclassified", None


def count_uses(names: List[str], files: List[str]) -> Dict[str, Dict[str, Dict[str, int]]]:
    patterns = {n: re.compile(rf"(?<![{NAME_CHARS}]){re.escape(n)}(?![{NAME_CHARS}])")
                for n in names}
    uses: Dict[str, Dict[str, Dict[str, int]]] = {n: defaultdict(dict) for n in names}
    for rel in files:
        try:
            text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        area = area_of(rel)
        for name, pat in patterns.items():
            if name not in text:
                continue
            n = len(pat.findall(text))
            if n:
                uses[name][area][rel] = n
    return uses


def alias_wrappers(path: Path, known: set) -> List[Tuple[str, str]]:
    """Find `word NAME TARGET end` wrappers whose body is a single CT word."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    body_re = re.compile(r"^word\s+(\S+)\s*(?:#[^\n]*)?\n((?:.*\n)*?)end\b", re.MULTILINE)
    inline_re = re.compile(r"^word\s+(\S+)\s+(\S+)\s+end\b", re.MULTILINE)
    found: Dict[str, str] = {}
    for m in inline_re.finditer(text):
        if m.group(2) in known:
            found[m.group(1)] = m.group(2)
    for m in body_re.finditer(text):
        tokens = [t for line in m.group(2).splitlines()
                  for t in line.split("#", 1)[0].split()]
        if len(tokens) == 1 and tokens[0] in known:
            found.setdefault(m.group(1), tokens[0])
    return sorted(found.items())


def build() -> dict:
    files = tracked_files()
    words = registered_words()
    names = sorted(set(words) | set(TEMPLATE_DIRECTIVES))
    uses = count_uses(names, files)
    known = set(words)
    wrappers = {
        "stdlib/meta.sl": alias_wrappers(ROOT / "stdlib/meta.sl", known),
        "libs/fn.sl": alias_wrappers(ROOT / "libs/fn.sl", known),
    }
    return {"words": words, "names": names, "uses": uses, "wrappers": wrappers}


def summarize(data: dict) -> Tuple[Dict[str, List[str]], List[str]]:
    by_group: Dict[str, List[str]] = defaultdict(list)
    unclassified = []
    for name in data["names"]:
        group, action, _ = disposition(name)
        if action == "unclassified":
            unclassified.append(name)
        else:
            by_group[group].append(name)
    return by_group, unclassified


def area_counts(per_area: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    return {area: sum(files.values()) for area, files in per_area.items()}


def render_markdown(data: dict) -> str:
    by_group, unclassified = summarize(data)
    out = ["# CT API Refactor — Slice-0 Inventory", "",
           "Generated by `tools/ct_refactor_inventory.py`. Counts are word-boundary",
           "matches of each **old** name in git-tracked files (`groups/` excluded).",
           "`compiler` is `l2_main.py`; `tests` includes `extra_tests/`.", ""]
    total = {"keep": 0, "rename": 0, "retire": 0}
    for name in data["names"]:
        _, action, _ = disposition(name)
        if action in total:
            total[action] += 1
    out += [f"Registered CT words: {len(data['words'])}; template directives: "
            f"{len(TEMPLATE_DIRECTIVES)}.",
            f"Dispositions: keep {total['keep']}, rename {total['rename']}, "
            f"retire {total['retire']}, unclassified {len(unclassified)}.", ""]
    if unclassified:
        out += ["## Unclassified (needs a decision)", ""]
        out += [f"* `{n}`" for n in unclassified] + [""]
    areas = ["stdlib", "libs", "tests", "examples", "docs.py", "readme", "tools", "other"]
    for group in sorted(by_group, key=lambda g: int(g)):
        rows = []
        for name in sorted(by_group[group]):
            _, action, final = disposition(name)
            if action == "keep":
                continue
            counts = area_counts(data["uses"][name])
            outside = sum(v for k, v in counts.items() if k != "compiler")
            top = sorted(((f, n) for a, fs in data["uses"][name].items() if a != "compiler"
                          for f, n in fs.items()), key=lambda x: -x[1])[:3]
            rows.append((name, action, final, counts, outside, top))
        if not rows:
            continue
        out += [f"## Group {group}", "",
                "| Old name | Action | Final name | " + " | ".join(areas) + " | Main files |",
                "|---|---|---|" + "---|" * len(areas) + "---|"]
        for name, action, final, counts, outside, top in rows:
            cells = " | ".join(str(counts.get(a, 0) or "") for a in areas)
            topf = ", ".join(f"`{f}` ({n})" for f, n in top)
            out.append(f"| `{name}` | {action} | {('`'+final+'`') if final else '—'} | {cells} | {topf} |")
        out.append("")
    out += ["## Pure alias wrappers", "",
            "Words whose whole body is one registered CT word. The plan removes these",
            "(callers move to the final CT name).", ""]
    for path, items in data["wrappers"].items():
        out.append(f"### `{path}` — {len(items)} wrappers")
        out.append("")
        for wrapper, target in items:
            _, action, final = disposition(target)
            note = f"→ `{final}`" if action == "rename" else ("retired" if action == "retire" else "kept")
            out.append(f"* `{wrapper}` = `{target}` ({note})")
        out.append("")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--markdown", metavar="PATH", help="write a markdown report")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any registered word has no disposition")
    args = ap.parse_args(argv)
    data = build()
    by_group, unclassified = summarize(data)
    if args.markdown:
        Path(args.markdown).write_text(render_markdown(data) + "\n", encoding="utf-8")
    counts = {"keep": 0, "rename": 0, "retire": 0}
    pending = 0
    for name in data["names"]:
        _, action, _ = disposition(name)
        if action in counts:
            counts[action] += 1
        if action in ("rename", "retire"):
            outside = sum(sum(f.values()) for a, f in data["uses"][name].items() if a != "compiler")
            if outside:
                pending += 1
    wrappers = sum(len(v) for v in data["wrappers"].values())
    print(f"registered={len(data['words'])} directives={len(TEMPLATE_DIRECTIVES)} "
          f"keep={counts['keep']} rename={counts['rename']} retire={counts['retire']} "
          f"unclassified={len(unclassified)}")
    print(f"old names still used outside the compiler: {pending}; alias wrappers: {wrappers}")
    for name in unclassified:
        print(f"  unclassified: {name}")
    return 1 if (args.check and unclassified) else 0


if __name__ == "__main__":
    sys.exit(main())

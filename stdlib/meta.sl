# Compile-time metaprogramming toolkit.
#
# This library keeps compatibility wrappers for raw ct-* words while also
# providing higher-level helpers for common macro/rewrite workflows.

# ---------------------------------------------------------------------------
#  Base wrappers (compatibility)
# ---------------------------------------------------------------------------

# Compatibility wrapper around ct-add-reader-rewrite.
word meta-reader-add ct-add-reader-rewrite end
compile-only

# Compatibility wrapper around ct-add-reader-rewrite-named.
word meta-reader-add-named ct-add-reader-rewrite-named end
compile-only

# Compatibility wrapper around ct-add-reader-rewrite-priority.
word meta-reader-add-priority ct-add-reader-rewrite-priority end
compile-only

# Compatibility wrapper around ct-remove-reader-rewrite.
word meta-reader-remove ct-remove-reader-rewrite end
compile-only

# Compatibility wrapper around ct-clear-reader-rewrites.
word meta-reader-clear ct-clear-reader-rewrites end
compile-only

# Compatibility wrapper around ct-list-reader-rewrites.
word meta-reader-list ct-list-reader-rewrites end
compile-only

# Compatibility wrapper around ct-set-reader-rewrite-enabled.
word meta-reader-enabled-set ct-set-reader-rewrite-enabled end
compile-only

# Compatibility wrapper around ct-get-reader-rewrite-enabled.
word meta-reader-enabled-get ct-get-reader-rewrite-enabled end
compile-only

# Compatibility wrapper around ct-set-reader-rewrite-priority.
word meta-reader-priority-set ct-set-reader-rewrite-priority end
compile-only

# Compatibility wrapper around ct-get-reader-rewrite-priority.
word meta-reader-priority-get ct-get-reader-rewrite-priority end
compile-only

# Compatibility wrapper around ct-add-grammar-rewrite.
word meta-grammar-add ct-add-grammar-rewrite end
compile-only

# Compatibility wrapper around ct-add-grammar-rewrite-named.
word meta-grammar-add-named ct-add-grammar-rewrite-named end
compile-only

# Compatibility wrapper around ct-add-grammar-rewrite-priority.
word meta-grammar-add-priority ct-add-grammar-rewrite-priority end
compile-only

# Compatibility wrapper around ct-remove-grammar-rewrite.
word meta-grammar-remove ct-remove-grammar-rewrite end
compile-only

# Compatibility wrapper around ct-clear-grammar-rewrites.
word meta-grammar-clear ct-clear-grammar-rewrites end
compile-only

# Compatibility wrapper around ct-list-grammar-rewrites.
word meta-grammar-list ct-list-grammar-rewrites end
compile-only

# Compatibility wrapper around ct-set-grammar-rewrite-enabled.
word meta-grammar-enabled-set ct-set-grammar-rewrite-enabled end
compile-only

# Compatibility wrapper around ct-get-grammar-rewrite-enabled.
word meta-grammar-enabled-get ct-get-grammar-rewrite-enabled end
compile-only

# Compatibility wrapper around ct-set-grammar-rewrite-priority.
word meta-grammar-priority-set ct-set-grammar-rewrite-priority end
compile-only

# Compatibility wrapper around ct-get-grammar-rewrite-priority.
word meta-grammar-priority-get ct-get-grammar-rewrite-priority end
compile-only

# Compatibility wrapper around ct-set-macro-expansion-limit.
word meta-macro-limit-set ct-set-macro-expansion-limit end
compile-only

# Compatibility wrapper around ct-get-macro-expansion-limit.
word meta-macro-limit-get ct-get-macro-expansion-limit end
compile-only

# Compatibility wrapper around ct-set-macro-preview.
word meta-macro-preview-set ct-set-macro-preview end
compile-only

# Compatibility wrapper around ct-get-macro-preview.
word meta-macro-preview-get ct-get-macro-preview end
compile-only

# Compatibility wrapper around ct-register-text-macro.
word meta-macro-register ct-register-text-macro end
compile-only

# Compatibility wrapper around ct-register-text-macro-signature.
word meta-macro-register-signature ct-register-text-macro-signature end
compile-only

# Compatibility wrapper around ct-register-pattern-macro.
word meta-macro-pattern-register ct-register-pattern-macro end
compile-only

# Compatibility wrapper around ct-unregister-pattern-macro.
word meta-macro-pattern-unregister ct-unregister-pattern-macro end
compile-only

# Compatibility wrapper around ct-word-is-text-macro.
word meta-macro-is-text ct-word-is-text-macro end
compile-only

# Compatibility wrapper around ct-word-is-pattern-macro.
word meta-macro-is-pattern ct-word-is-pattern-macro end
compile-only

# Compatibility wrapper around ct-get-macro-signature.
word meta-macro-signature ct-get-macro-signature end
compile-only

# Compatibility wrapper around ct-get-macro-expansion.
word meta-macro-expansion-get ct-get-macro-expansion end
compile-only

# Compatibility wrapper around ct-set-macro-expansion.
word meta-macro-expansion-set ct-set-macro-expansion end
compile-only

# Compatibility wrapper around ct-clone-macro.
word meta-macro-clone ct-clone-macro end
compile-only

# Compatibility wrapper around ct-rename-macro.
word meta-macro-rename ct-rename-macro end
compile-only

# Compatibility wrapper around ct-macro-doc-get.
word meta-macro-doc-get ct-macro-doc-get end
compile-only

# Compatibility wrapper around ct-macro-doc-set.
word meta-macro-doc-set ct-macro-doc-set end
compile-only

# Compatibility wrapper around ct-macro-attrs-get.
word meta-macro-attrs-get ct-macro-attrs-get end
compile-only

# Compatibility wrapper around ct-macro-attrs-set.
word meta-macro-attrs-set ct-macro-attrs-set end
compile-only

# Compatibility wrapper around ct-set-ct-call-contract.
word meta-ct-call-contract-set ct-set-ct-call-contract end
compile-only

# Compatibility wrapper around ct-get-ct-call-contract.
word meta-ct-call-contract-get ct-get-ct-call-contract end
compile-only

# Compatibility wrapper around ct-set-ct-call-exception-policy.
word meta-ct-call-exception-policy-set ct-set-ct-call-exception-policy end
compile-only

# Compatibility wrapper around ct-get-ct-call-exception-policy.
word meta-ct-call-exception-policy-get ct-get-ct-call-exception-policy end
compile-only

# Compatibility wrapper around ct-set-ct-call-sandbox-mode.
word meta-ct-call-sandbox-mode-set ct-set-ct-call-sandbox-mode end
compile-only

# Compatibility wrapper around ct-get-ct-call-sandbox-mode.
word meta-ct-call-sandbox-mode-get ct-get-ct-call-sandbox-mode end
compile-only

# Compatibility wrapper around ct-set-ct-call-sandbox-allowlist.
word meta-ct-call-sandbox-allowlist-set ct-set-ct-call-sandbox-allowlist end
compile-only

# Compatibility wrapper around ct-get-ct-call-sandbox-allowlist.
word meta-ct-call-sandbox-allowlist-get ct-get-ct-call-sandbox-allowlist end
compile-only

# Compatibility wrapper around ct-ctrand-seed.
word meta-ct-rand-seed ct-ctrand-seed end
compile-only

# Compatibility wrapper around ct-ctrand-int.
word meta-ct-rand-int ct-ctrand-int end
compile-only

# Compatibility wrapper around ct-ctrand-range.
word meta-ct-rand-range ct-ctrand-range end
compile-only

# Compatibility wrapper around ct-set-ct-call-memo.
word meta-ct-call-memo-set ct-set-ct-call-memo end
compile-only

# Compatibility wrapper around ct-get-ct-call-memo.
word meta-ct-call-memo-get ct-get-ct-call-memo end
compile-only

# Compatibility wrapper around ct-clear-ct-call-memo.
word meta-ct-call-memo-clear ct-clear-ct-call-memo end
compile-only

# Compatibility wrapper around ct-get-ct-call-memo-size.
word meta-ct-call-memo-size ct-get-ct-call-memo-size end
compile-only

# Compatibility wrapper around ct-set-ct-call-side-effects.
word meta-ct-call-side-effects-set ct-set-ct-call-side-effects end
compile-only

# Compatibility wrapper around ct-get-ct-call-side-effects.
word meta-ct-call-side-effects-get ct-get-ct-call-side-effects end
compile-only

# Compatibility wrapper around ct-get-ct-call-side-effect-log.
word meta-ct-call-side-effects-log ct-get-ct-call-side-effect-log end
compile-only

# Compatibility wrapper around ct-clear-ct-call-side-effect-log.
word meta-ct-call-side-effects-clear ct-clear-ct-call-side-effect-log end
compile-only

# Compatibility wrapper around ct-set-ct-call-recursion-limit.
word meta-ct-call-recursion-limit-set ct-set-ct-call-recursion-limit end
compile-only

# Compatibility wrapper around ct-get-ct-call-recursion-limit.
word meta-ct-call-recursion-limit-get ct-get-ct-call-recursion-limit end
compile-only

# Compatibility wrapper around ct-set-ct-call-timeout-ms.
word meta-ct-call-timeout-ms-set ct-set-ct-call-timeout-ms end
compile-only

# Compatibility wrapper around ct-get-ct-call-timeout-ms.
word meta-ct-call-timeout-ms-get ct-get-ct-call-timeout-ms end
compile-only

# Compatibility wrapper around ct-get-macro-template-mode.
word meta-macro-template-mode ct-get-macro-template-mode end
compile-only

# Compatibility wrapper around ct-get-macro-template-version.
word meta-macro-template-version ct-get-macro-template-version end
compile-only

# Compatibility wrapper around ct-get-macro-template-program-size.
word meta-macro-template-program-size ct-get-macro-template-program-size end
compile-only

# Compatibility wrapper around ct-gensym.
word meta-gensym ct-gensym end
compile-only

# Compatibility wrapper around ct-capture-args.
word meta-capture-args ct-capture-args end
compile-only

# Compatibility wrapper around ct-capture-locals.
word meta-capture-locals ct-capture-locals end
compile-only

# Compatibility wrapper around ct-capture-globals.
word meta-capture-globals ct-capture-globals end
compile-only

# Compatibility wrapper around ct-capture-get.
word meta-capture-get ct-capture-get end
compile-only

# Compatibility wrapper around ct-capture-has?.
word meta-capture-has ct-capture-has? end
compile-only

# Compatibility wrapper around ct-capture-shape.
word meta-capture-shape ct-capture-shape end
compile-only

# Compatibility wrapper around ct-capture-assert-shape.
word meta-capture-assert-shape ct-capture-assert-shape end
compile-only

# Compatibility wrapper around ct-capture-count.
word meta-capture-count ct-capture-count end
compile-only

# Compatibility wrapper around ct-capture-slice.
word meta-capture-slice ct-capture-slice end
compile-only

# Compatibility wrapper around ct-capture-map.
word meta-capture-map ct-capture-map end
compile-only

# Compatibility wrapper around ct-capture-filter.
word meta-capture-filter ct-capture-filter end
compile-only

# Compatibility wrapper around ct-capture-separate.
word meta-capture-separate ct-capture-separate end
compile-only

# Compatibility wrapper around ct-capture-join.
word meta-capture-join ct-capture-join end
compile-only

# Compatibility wrapper around ct-capture-equal?.
word meta-capture-equal ct-capture-equal? end
compile-only

# Compatibility wrapper around ct-capture-normalize.
word meta-capture-normalize ct-capture-normalize end
compile-only

# Compatibility wrapper around ct-capture-pretty.
word meta-capture-pretty ct-capture-pretty end
compile-only

# Compatibility wrapper around ct-capture-clone.
word meta-capture-clone ct-capture-clone end
compile-only

# Compatibility wrapper around ct-capture-coerce-tokens.
word meta-capture-coerce-tokens ct-capture-coerce-tokens end
compile-only

# Compatibility wrapper around ct-capture-coerce-string.
word meta-capture-coerce-string ct-capture-coerce-string end
compile-only

# Compatibility wrapper around ct-capture-coerce-number.
word meta-capture-coerce-number ct-capture-coerce-number end
compile-only

# Compatibility wrapper around ct-capture-origin.
word meta-capture-origin ct-capture-origin end
compile-only

# Compatibility wrapper around ct-capture-lifetime.
word meta-capture-lifetime ct-capture-lifetime end
compile-only

# Compatibility wrapper around ct-capture-lifetime-live?.
word meta-capture-lifetime-live ct-capture-lifetime-live? end
compile-only

# Compatibility wrapper around ct-capture-lifetime-assert.
word meta-capture-lifetime-assert ct-capture-lifetime-assert end
compile-only

# Compatibility wrapper around ct-capture-lint.
word meta-capture-lint ct-capture-lint end
compile-only

# Compatibility wrapper around ct-capture-global-set.
word meta-capture-global-set ct-capture-global-set end
compile-only

# Compatibility wrapper around ct-capture-global-get.
word meta-capture-global-get ct-capture-global-get end
compile-only

# Compatibility wrapper around ct-capture-global-delete.
word meta-capture-global-delete ct-capture-global-delete end
compile-only

# Compatibility wrapper around ct-capture-global-clear.
word meta-capture-global-clear ct-capture-global-clear end
compile-only

# Compatibility wrapper around ct-capture-freeze.
word meta-capture-freeze ct-capture-freeze end
compile-only

# Compatibility wrapper around ct-capture-thaw.
word meta-capture-thaw ct-capture-thaw end
compile-only

# Compatibility wrapper around ct-capture-mutable?.
word meta-capture-mutable ct-capture-mutable? end
compile-only

# Compatibility wrapper around ct-capture-schema-put.
word meta-capture-schema-put ct-capture-schema-put end
compile-only

# Compatibility wrapper around ct-capture-schema-get.
word meta-capture-schema-get ct-capture-schema-get end
compile-only

# Compatibility wrapper around ct-capture-schema-validate.
word meta-capture-schema-validate ct-capture-schema-validate end
compile-only

# Compatibility wrapper around ct-capture-taint-set.
word meta-capture-taint-set ct-capture-taint-set end
compile-only

# Compatibility wrapper around ct-capture-taint-get.
word meta-capture-taint-get ct-capture-taint-get end
compile-only

# Compatibility wrapper around ct-capture-tainted?.
word meta-capture-tainted ct-capture-tainted? end
compile-only

# Compatibility wrapper around ct-capture-serialize.
word meta-capture-serialize ct-capture-serialize end
compile-only

# Compatibility wrapper around ct-capture-deserialize.
word meta-capture-deserialize ct-capture-deserialize end
compile-only

# Compatibility wrapper around ct-capture-compress.
word meta-capture-compress ct-capture-compress end
compile-only

# Compatibility wrapper around ct-capture-decompress.
word meta-capture-decompress ct-capture-decompress end
compile-only

# Compatibility wrapper around ct-capture-hash.
word meta-capture-hash ct-capture-hash end
compile-only

# Compatibility wrapper around ct-capture-diff.
word meta-capture-diff ct-capture-diff end
compile-only

# Compatibility wrapper around ct-capture-replay-log.
word meta-capture-replay-log ct-capture-replay-log end
compile-only

# Compatibility wrapper around ct-capture-replay-clear.
word meta-capture-replay-clear ct-capture-replay-clear end
compile-only

# Compatibility wrapper around ct-list-pattern-macros.
word meta-macro-pattern-list ct-list-pattern-macros end
compile-only

# Compatibility wrapper around ct-set-pattern-macro-enabled.
word meta-macro-pattern-enabled-set ct-set-pattern-macro-enabled end
compile-only

# Compatibility wrapper around ct-get-pattern-macro-enabled.
word meta-macro-pattern-enabled-get ct-get-pattern-macro-enabled end
compile-only

# Compatibility wrapper around ct-set-pattern-macro-priority.
word meta-macro-pattern-priority-set ct-set-pattern-macro-priority end
compile-only

# Compatibility wrapper around ct-get-pattern-macro-priority.
word meta-macro-pattern-priority-get ct-get-pattern-macro-priority end
compile-only

# Compatibility wrapper around ct-get-pattern-macro-clauses.
word meta-macro-pattern-clauses ct-get-pattern-macro-clauses end
compile-only

# Compatibility wrapper around ct-get-pattern-macro-clause-details.
word meta-macro-pattern-clause-details ct-get-pattern-macro-clause-details end
compile-only

# Compatibility wrapper around ct-set-pattern-macro-group.
word meta-macro-pattern-group-set ct-set-pattern-macro-group end
compile-only

# Compatibility wrapper around ct-get-pattern-macro-group.
word meta-macro-pattern-group-get ct-get-pattern-macro-group end
compile-only

# Compatibility wrapper around ct-set-pattern-macro-scope.
word meta-macro-pattern-scope-set ct-set-pattern-macro-scope end
compile-only

# Compatibility wrapper around ct-get-pattern-macro-scope.
word meta-macro-pattern-scope-get ct-get-pattern-macro-scope end
compile-only

# Compatibility wrapper around ct-set-pattern-group-active.
word meta-macro-pattern-group-active-set ct-set-pattern-group-active end
compile-only

# Compatibility wrapper around ct-set-pattern-scope-active.
word meta-macro-pattern-scope-active-set ct-set-pattern-scope-active end
compile-only

# Compatibility wrapper around ct-list-active-pattern-groups.
word meta-macro-pattern-groups-active ct-list-active-pattern-groups end
compile-only

# Compatibility wrapper around ct-list-active-pattern-scopes.
word meta-macro-pattern-scopes-active ct-list-active-pattern-scopes end
compile-only

# Compatibility wrapper around ct-set-pattern-macro-clause-guard.
word meta-macro-pattern-clause-guard-set ct-set-pattern-macro-clause-guard end
compile-only

# Compatibility wrapper around ct-detect-pattern-conflicts.
word meta-macro-pattern-conflicts ct-detect-pattern-conflicts end
compile-only

# Compatibility wrapper around ct-detect-pattern-conflicts-named.
word meta-macro-pattern-conflicts-named ct-detect-pattern-conflicts-named end
compile-only

# Compatibility wrapper around ct-get-rewrite-specificity.
word meta-rewrite-specificity ct-get-rewrite-specificity end
compile-only

# Compatibility wrapper around ct-set-rewrite-pipeline.
word meta-rewrite-pipeline-set ct-set-rewrite-pipeline end
compile-only

# Compatibility wrapper around ct-get-rewrite-pipeline.
word meta-rewrite-pipeline-get ct-get-rewrite-pipeline end
compile-only

# Compatibility wrapper around ct-set-rewrite-pipeline-active.
word meta-rewrite-pipeline-active-set ct-set-rewrite-pipeline-active end
compile-only

# Compatibility wrapper around ct-list-rewrite-active-pipelines.
word meta-rewrite-pipelines-active ct-list-rewrite-active-pipelines end
compile-only

# Compatibility wrapper around ct-rebuild-rewrite-index.
word meta-rewrite-index-rebuild ct-rebuild-rewrite-index end
compile-only

# Compatibility wrapper around ct-get-rewrite-index-stats.
word meta-rewrite-index-stats ct-get-rewrite-index-stats end
compile-only

# Compatibility wrapper around ct-rewrite-txn-begin.
word meta-rewrite-txn-begin ct-rewrite-txn-begin end
compile-only

# Compatibility wrapper around ct-rewrite-txn-commit.
word meta-rewrite-txn-commit ct-rewrite-txn-commit end
compile-only

# Compatibility wrapper around ct-rewrite-txn-rollback.
word meta-rewrite-txn-rollback ct-rewrite-txn-rollback end
compile-only

# Compatibility wrapper around ct-export-rewrite-pack.
word meta-rewrite-pack-export ct-export-rewrite-pack end
compile-only

# Compatibility wrapper around ct-import-rewrite-pack.
word meta-rewrite-pack-import ct-import-rewrite-pack end
compile-only

# Compatibility wrapper around ct-import-rewrite-pack-replace.
word meta-rewrite-pack-import-replace ct-import-rewrite-pack-replace end
compile-only

# Compatibility wrapper around ct-get-rewrite-provenance.
word meta-rewrite-provenance ct-get-rewrite-provenance end
compile-only

# Compatibility wrapper around ct-rewrite-dry-run.
word meta-rewrite-dry-run ct-rewrite-dry-run end
compile-only

# Compatibility wrapper around ct-rewrite-generate-fixture.
word meta-rewrite-fixture ct-rewrite-generate-fixture end
compile-only

# Compatibility wrapper around ct-set-rewrite-saturation.
word meta-rewrite-saturation-set ct-set-rewrite-saturation end
compile-only

# Compatibility wrapper around ct-get-rewrite-saturation.
word meta-rewrite-saturation-get ct-get-rewrite-saturation end
compile-only

# Compatibility wrapper around ct-set-rewrite-max-steps.
word meta-rewrite-max-steps-set ct-set-rewrite-max-steps end
compile-only

# Compatibility wrapper around ct-get-rewrite-max-steps.
word meta-rewrite-max-steps-get ct-get-rewrite-max-steps end
compile-only

# Compatibility wrapper around ct-set-rewrite-loop-detection.
word meta-rewrite-loop-detect-set ct-set-rewrite-loop-detection end
compile-only

# Compatibility wrapper around ct-get-rewrite-loop-detection.
word meta-rewrite-loop-detect-get ct-get-rewrite-loop-detection end
compile-only

# Compatibility wrapper around ct-get-rewrite-loop-reports.
word meta-rewrite-loop-reports ct-get-rewrite-loop-reports end
compile-only

# Compatibility wrapper around ct-clear-rewrite-loop-reports.
word meta-rewrite-loop-reports-clear ct-clear-rewrite-loop-reports end
compile-only

# Compatibility wrapper around ct-set-rewrite-trace.
word meta-rewrite-trace-set ct-set-rewrite-trace end
compile-only

# Compatibility wrapper around ct-get-rewrite-trace.
word meta-rewrite-trace-get ct-get-rewrite-trace end
compile-only

# Compatibility wrapper around ct-get-rewrite-trace-log.
word meta-rewrite-trace-log ct-get-rewrite-trace-log end
compile-only

# Compatibility wrapper around ct-clear-rewrite-trace-log.
word meta-rewrite-trace-clear ct-clear-rewrite-trace-log end
compile-only

# Compatibility wrapper around ct-get-rewrite-profile.
word meta-rewrite-profile-get ct-get-rewrite-profile end
compile-only

# Compatibility wrapper around ct-clear-rewrite-profile.
word meta-rewrite-profile-clear ct-clear-rewrite-profile end
compile-only

# Compatibility wrapper around ct-rewrite-compatibility-matrix.
word meta-rewrite-compatibility ct-rewrite-compatibility-matrix end
compile-only

# Compatibility wrapper around ct-unregister-word.
word meta-word-remove ct-unregister-word end
compile-only

# Compatibility wrapper around ct-list-words.
word meta-word-list ct-list-words end
compile-only

# Compatibility wrapper around ct-word-exists?.
word meta-word-exists ct-word-exists? end
compile-only

# Compatibility wrapper around ct-get-word-body.
word meta-word-body ct-get-word-body end
compile-only

# Compatibility wrapper around ct-get-word-asm.
word meta-word-asm ct-get-word-asm end
compile-only

# Compatibility wrapper around ct-current-token.
word meta-current-token ct-current-token end
compile-only

# Compatibility wrapper around ct-parser-pos.
word meta-parser-pos ct-parser-pos end
compile-only

# Compatibility wrapper around ct-parser-remaining.
word meta-parser-remaining ct-parser-remaining end
compile-only

# Compatibility wrapper around inject-tokens.
word meta-inject-tokens inject-tokens end
compile-only

# ---------------------------------------------------------------------------
#  Value / list constructors
# ---------------------------------------------------------------------------

# Build a one-element list from the top stack value.
word meta-list1
	list-new swap list-append
end
compile-only

# Build a two-element list preserving argument order.
word meta-list2
	list-new swap list-append swap list-append
end
compile-only

# Build a three-element list preserving argument order.
word meta-list3
	list-new swap list-append swap list-append swap list-append
end
compile-only

# Create a token from a lexeme using a nil template location.
word meta-token
	nil token-from-lexeme
end
compile-only

# Consume the next parser token and return its lexeme.
word meta-next-lexeme
	next-token token-lexeme
end
compile-only

# Read the next parser token lexeme without consuming it.
word meta-peek-lexeme
	peek-token token-lexeme
end
compile-only

# Collect lexemes until stop token is reached (excluding the stop token).
# [* | stopLexeme] -> [* | tokens]
word meta-collect-until
	>r
	list-new
	next-token
	dup token-lexeme r@ string= not
	while do
		list-append
		next-token
		dup token-lexeme r@ string= not
	end
	drop
	rdrop
end
compile-only

# Collect lexemes until stop token is reached (including the stop token).
# [* | stopLexeme] -> [* | tokens]
word meta-collect-until-including
	>r
	list-new
	next-token
	dup token-lexeme r@ string= not
	while do
		list-append
		next-token
		dup token-lexeme r@ string= not
	end
	list-append
	rdrop
end
compile-only

# ---------------------------------------------------------------------------
#  Rewrite convenience API
# ---------------------------------------------------------------------------

# Register a named reader rewrite that replaces one lexeme with another.
# [*, name, from | to] -> [* | name]
word meta-reader-replace-word
	meta-list1
	swap
	meta-list1
	swap
	meta-reader-add-named
end
compile-only

# Register a named grammar rewrite that replaces one lexeme with another.
# [*, name, from | to] -> [* | name]
word meta-grammar-replace-word
	meta-list1
	swap
	meta-list1
	swap
	meta-grammar-add-named
end
compile-only

# Upsert a reader rewrite by removing any existing rule with this name first.
# [*, name, from | to] -> [* | name]
word meta-reader-upsert-word
	>r >r
	dup meta-reader-remove drop
	r> r>
	meta-reader-replace-word
end
compile-only

# Upsert a grammar rewrite by removing any existing rule with this name first.
# [*, name, from | to] -> [* | name]
word meta-grammar-upsert-word
	>r >r
	dup meta-grammar-remove drop
	r> r>
	meta-grammar-replace-word
end
compile-only

# Enable a reader rewrite rule by name.
word meta-reader-enable
	1 meta-reader-enabled-set
end
compile-only

# Disable a reader rewrite rule by name.
word meta-reader-disable
	0 meta-reader-enabled-set
end
compile-only

# Enable a grammar rewrite rule by name.
word meta-grammar-enable
	1 meta-grammar-enabled-set
end
compile-only

# Disable a grammar rewrite rule by name.
word meta-grammar-disable
	0 meta-grammar-enabled-set
end
compile-only

# Create a reader-stage alias from alias lexeme to target lexeme.
# [*, alias | target] -> [* | name]
word meta-reader-alias
	over swap
	meta-reader-replace-word
end
compile-only

# Create a grammar-stage alias from alias lexeme to target lexeme.
# [*, alias | target] -> [* | name]
word meta-grammar-alias
	over swap
	meta-grammar-replace-word
end
compile-only

# ---------------------------------------------------------------------------
#  Macro authoring helpers
# ---------------------------------------------------------------------------

# Register a zero-argument text macro from an expansion token list.
# [*, name | expansionList] -> [*]
word meta-macro-register-0
	list-new swap
	meta-macro-register-signature
end
compile-only

# Register a zero-argument macro that expands to a single target token.
# [*, name | target] -> [*]
word meta-macro-register-alias
	meta-list1
	meta-macro-register-0
end
compile-only

# Build one pattern-macro clause list in [pattern replacement] form.
# [*, pattern | replacement] -> [* | clause]
word meta-macro-clause
	>r
	list-new
	swap list-append
	r> list-append
end
compile-only

# Append one [pattern replacement] clause to an existing clause list.
# [*, clauses, pattern | replacement] -> [* | clauses]
word meta-macro-clauses-append
	meta-macro-clause
	list-append
end
compile-only

# Register a binary-op macro that expands to "$lhs $rhs <op>".
# [*, name | op] -> [*]
word meta-macro-register-binary-op
	>r
	list-new "lhs" list-append "rhs" list-append
	list-new "$lhs" list-append "$rhs" list-append r> list-append
	meta-macro-register-signature
end
compile-only

# ---------------------------------------------------------------------------
#  Dictionary and parser convenience
# ---------------------------------------------------------------------------

# Remove a word definition if it exists and return whether removal happened.
word meta-word-drop-if-exists
	dup meta-word-exists
	if
		meta-word-remove
	else
		drop 0
	end
end
compile-only

# Assert that a word exists, otherwise raise a descriptive parse error.
word meta-word-require
	dup meta-word-exists
	if
		drop
	else
		"required word missing: " swap string-append parse-error
	end
end
compile-only

# Emit a constant word definition from name and value tokens.
# [*, name | value] -> [*]
word meta-define-const
	>r
	ct-current-token
	swap
	r>
	meta-list1
	emit-definition
end
compile-only

# Emit an alias word definition from name and target word token.
# [*, name | targetWord] -> [*]
word meta-define-alias
	>r
	ct-current-token
	swap
	r>
	meta-list1
	emit-definition
end
compile-only

# ---------------------------------------------------------------------------
#  High-level authoring helpers
# ---------------------------------------------------------------------------

# Inject one lexeme token at the current parser position.
# [* | lexeme] -> [*]
word meta-inject-lexeme
	meta-token
	meta-list1
	meta-inject-tokens
end
compile-only

# Inject one word-call lexeme at the current parser position.
# [* | wordName] -> [*]
word meta-inject-word-call
	meta-inject-lexeme
end
compile-only

# Inject two lexeme tokens in order at the current parser position.
# [*, first | second] -> [*]
word meta-inject-lexeme-pair
	meta-token
	swap
	meta-token
	list-new
	swap list-append
	swap list-append
	meta-inject-tokens
end
compile-only

# Upsert a named reader rewrite using full pattern/replacement lists.
# [*, name, patternList | replacementList] -> [* | name]
word meta-reader-upsert-seq
	>r >r
	dup meta-reader-remove drop
	r> r>
	meta-reader-add-named
end
compile-only

# Upsert a named grammar rewrite using full pattern/replacement lists.
# [*, name, patternList | replacementList] -> [* | name]
word meta-grammar-upsert-seq
	>r >r
	dup meta-grammar-remove drop
	r> r>
	meta-grammar-add-named
end
compile-only

# Remove a reader rewrite by name when present and return a removed flag.
# [* | name] -> [* | removed]
word meta-reader-remove-if-exists
	dup meta-reader-remove
	if
		drop 1
	else
		drop 0
	end
end
compile-only

# Remove a grammar rewrite by name when present and return a removed flag.
# [* | name] -> [* | removed]
word meta-grammar-remove-if-exists
	dup meta-grammar-remove
	if
		drop 1
	else
		drop 0
	end
end
compile-only

# Register a unary-op macro that expands to "$x <op>".
# [*, name | op] -> [*]
word meta-macro-register-unary-op
	>r
	list-new "x" list-append
	list-new "$x" list-append r> list-append
	meta-macro-register-signature
end
compile-only

# Register a zero-argument macro that always expands to one value token.
# [*, name | valueToken] -> [*]
word meta-macro-register-const
	meta-list1
	meta-macro-register-0
end
compile-only

# Define a constant word only when the target name is currently missing.
# [*, name | value] -> [*]
word meta-define-const-if-missing
	>r
	dup meta-word-exists
	if
		drop
		rdrop
	else
		r>
		meta-define-const
	end
end
compile-only

# Define an alias word only when the target name is currently missing.
# [*, name | targetWord] -> [*]
word meta-define-alias-if-missing
	>r
	dup meta-word-exists
	if
		drop
		rdrop
	else
		r>
		meta-define-alias
	end
end
compile-only

# Return 1 when the parser has no remaining unread tokens.
# [*] -> [* | flag]
word meta-parser-at-end?
	meta-parser-remaining 0 ==
end
compile-only

# Return the source line of the current parser token.
# [*] -> [* | line]
word meta-current-line
	ct-current-token token-line
end
compile-only

# Return the source column of the current parser token.
# [*] -> [* | column]
word meta-current-column
	ct-current-token token-column
end
compile-only

# Consume the next lexeme and assert it is a valid identifier.
# [*] -> [* | identifierLexeme]
word meta-next-ident-lexeme
	meta-next-lexeme
	dup identifier?
	if
		# keep lexeme
	else
		"expected identifier, got: " swap string-append parse-error
	end
end
compile-only

# Consume the next lexeme and assert it matches an expected token.
# [* | expected] -> [*]
word meta-expect-lexeme
	>r
	meta-next-lexeme
	dup r@ string=
	if
		drop
		rdrop
	else
		"expected token: " r@ string-append
		", got: " string-append
		swap string-append
		parse-error
	end
end
compile-only

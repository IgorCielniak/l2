# Compile-time metaprogramming toolkit: higher-level helpers for common
# macro and rewrite workflows, built on the ct-* words (it adds no aliases).
#
# Contents (each word has its stack effect next to its definition):
#   Source of definitions   meta-runtime-get-source, meta-runtime-get-source-embedded,
#                           ct-executing?
#   Lists and tokens        meta-list1/2/3, meta-token, meta-tokens
#   Reading the input       meta-next-lexeme, meta-peek-lexeme, meta-next-ident-lexeme,
#                           meta-expect-lexeme, meta-collect-until(-including),
#                           meta-parser-at-end?, meta-current-line, meta-current-column
#   Rewrites                meta-reader/grammar-replace-word, -upsert-word, -upsert-seq,
#                           -alias, meta-rewrite-drop-previous
#   Generating definitions  meta-define-const, meta-define-alias (and -if-missing),
#                           meta-macro-register-unary-op, meta-macro-register-binary-op,
#                           meta-word-drop-if-exists, meta-word-require
#   Injecting input         meta-inject-lexeme, meta-inject-lexeme-pair
#
# Retired (replaced by newer features): meta-emit-lexemes (use a quotation with
# $( ... ) and emit), meta-macro-register-0 (a one-quotation macro),
# meta-macro-register-alias and meta-macro-register-const (`const`, or
# `const $(name) $(value)` emitted from compile-time code), and
# meta-inject-word-call (meta-inject-lexeme).

# ---------------------------------------------------------------------------
#  Runtime helpers
# ---------------------------------------------------------------------------

# Runtime lookup in the embedded catalog of selected-definition sources
# (built with --source-embed).  Exact name bytes; NULL/0 when unavailable.
# C signature: const char *l2_source_catalog_lookup(const char *name, long name_len, long *out_len)
extern const char *l2_source_catalog_lookup(const char *name, long name_len, long *out_len)

#meta-runtime-get-source-embedded [*, name_ptr | name_len] -> [*, src_ptr | src_len]
word meta-runtime-get-source-embedded
	8 alloc >r
	r@ l2_source_catalog_lookup
	dup 0 ==
	if
		drop
		r> 8 free
		0 0
	else
		r@ @
		r> 8 free
	end
end

# Runtime body of the compiler's internal source intrinsic; at compile time
# the intrinsic answers from the selected definition's retained source.
word __meta-runtime-source
	meta-runtime-get-source-embedded
end

# Provide a runtime body for ct-executing? for codegen paths that reference it.
# Compile-time/runtime intrinsics still override behavior in VM execution modes.
word ct-executing?
	0
end

# meta-runtime-get-source
# at compile time: [* | name] -> [*, src | src_len]   ("" 0 when unavailable)
# at runtime:      [*, name_ptr | name_len] -> [*, src_ptr | src_len]   (0 0 when unavailable)
# Both answer for the final selected definition only; the runtime lookup
# needs --source-embed.
word meta-runtime-get-source
	CT 1 ==
	if
		__meta-runtime-source
	else
		meta-runtime-get-source-embedded
	end
end


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

# Create a token from a lexeme anchored at the current parser token.
word meta-token
	ct-parser-current swap token-with-lexeme
end
compile-only

# Tokens for a list of lexemes, all anchored at the current parser token
# (so errors in generated code point at the user's code).
# [* | lexemes] -> [* | tokens]
word meta-tokens
	list-new swap                       # out lexemes
	begin
		dup list-empty? if drop exit end
		list-pop-front drop meta-token  # out lexemes' token
		rot swap list-append swap       # out' lexemes'
	again
end
compile-only

# Consume the next parser token and return its lexeme.
word meta-next-lexeme
	ct-parser-consume token-lexeme
end
compile-only

# Read the next parser token lexeme without consuming it.
word meta-peek-lexeme
	0 ct-parser-peek token-lexeme
end
compile-only

# Collect lexemes until stop token is reached (excluding the stop token).
# [* | stopLexeme] -> [* | tokens]
word meta-collect-until
	>r
	list-new
	ct-parser-consume
	dup token-lexeme r@ string-equal? not
	while do
		list-append
		ct-parser-consume
		dup token-lexeme r@ string-equal? not
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
	ct-parser-consume
	dup token-lexeme r@ string-equal? not
	while do
		list-append
		ct-parser-consume
		dup token-lexeme r@ string-equal? not
	end
	list-append
	rdrop
end
compile-only

# ---------------------------------------------------------------------------
#  Rewrite convenience API
# ---------------------------------------------------------------------------

# Register a named reader rewrite that replaces one lexeme with another.
# [*, name, from | to] -> [* | handle]
word meta-reader-replace-word
	meta-list1
	swap
	meta-list1
	swap
	ct-rewrite-add-reader-named
end
compile-only

# Register a named grammar rewrite that replaces one lexeme with another.
# [*, name, from | to] -> [* | handle]
word meta-grammar-replace-word
	meta-list1
	swap
	meta-list1
	swap
	ct-rewrite-add-grammar-named
end
compile-only

# Remove a previous registration (a RewriteHandle, or nil for none).
# [* | handle-or-nil] -> [*]
word meta-rewrite-drop-previous
	dup nil? if drop exit end
	ct-rewrite-remove drop
end
compile-only

# Replace a previously registered reader rewrite (handle or nil) with a
# one-lexeme rule.
# [*, previous, name, from | to] -> [* | handle]
word meta-reader-upsert-word
	>r >r >r
	meta-rewrite-drop-previous
	r> r> r>
	meta-reader-replace-word
end
compile-only

# Replace a previously registered grammar rewrite (handle or nil) with a
# one-lexeme rule.
# [*, previous, name, from | to] -> [* | handle]
word meta-grammar-upsert-word
	>r >r >r
	meta-rewrite-drop-previous
	r> r> r>
	meta-grammar-replace-word
end
compile-only

# Create a reader-stage alias from alias lexeme to target lexeme.
# [*, alias | target] -> [* | handle]
word meta-reader-alias
	over swap
	meta-reader-replace-word
end
compile-only

# Create a grammar-stage alias from alias lexeme to target lexeme.
# [*, alias | target] -> [* | handle]
word meta-grammar-alias
	over swap
	meta-grammar-replace-word
end
compile-only

# ---------------------------------------------------------------------------
#  Macro authoring helpers
# ---------------------------------------------------------------------------

# Define a two-parameter macro that expands to "$lhs $rhs <op>".
# [*, name | op] -> [*]
word meta-macro-register-binary-op
	`macro $(over meta-token) ( lhs rhs ) \` \$lhs \$rhs $(dup meta-token) \` emit ;` emit
	drop drop
end
compile-only

# ---------------------------------------------------------------------------
#  Dictionary and parser convenience
# ---------------------------------------------------------------------------

# Remove a word definition if it exists and return whether removal happened.
word meta-word-drop-if-exists
	dup ct-definition-exists?
	if
		ct-definition-unregister-by-name
	else
		drop 0
	end
end
compile-only

# Assert that a word exists, otherwise raise a descriptive parse error.
word meta-word-require
	dup ct-definition-exists?
	if
		drop
	else
		"required word missing: " swap string-append ct-parser-error
	end
end
compile-only

# Emit the definition `word <name> <value> end`.
# [*, name | value] -> [*]
word meta-define-const
	`word $(over meta-token) $(dup meta-token) end` emit
	drop drop
end
compile-only

# Emit the alias definition `word <name> <target> end`.
# [*, name | targetWord] -> [*]
word meta-define-alias
	meta-define-const
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
	ct-parser-inject
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
	ct-parser-inject
end
compile-only

# Replace a previously registered reader rewrite (handle or nil) with a
# rule built from full pattern/replacement lists.
# [*, previous, name, patternList | replacementList] -> [* | handle]
word meta-reader-upsert-seq
	>r >r >r
	meta-rewrite-drop-previous
	r> r> r>
	ct-rewrite-add-reader-named
end
compile-only

# Replace a previously registered grammar rewrite (handle or nil) with a
# rule built from full pattern/replacement lists.
# [*, previous, name, patternList | replacementList] -> [* | handle]
word meta-grammar-upsert-seq
	>r >r >r
	meta-rewrite-drop-previous
	r> r> r>
	ct-rewrite-add-grammar-named
end
compile-only

# Define a one-parameter macro that expands to "$x <op>".
# [*, name | op] -> [*]
word meta-macro-register-unary-op
	`macro $(over meta-token) ( x ) \` \$x $(dup meta-token) \` emit ;` emit
	drop drop
end
compile-only

# Define a constant word only when the target name is currently missing.
# [*, name | value] -> [*]
word meta-define-const-if-missing
	>r
	dup ct-definition-exists?
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
	dup ct-definition-exists?
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
	ct-parser-remaining 0 ==
end
compile-only

# Return the source line of the current parser token.
# [*] -> [* | line]
word meta-current-line
	ct-parser-current token-line
end
compile-only

# Return the source column of the current parser token.
# [*] -> [* | column]
word meta-current-column
	ct-parser-current token-column
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
		"expected identifier, got: " swap string-append ct-parser-error
	end
end
compile-only

# Consume the next lexeme and assert it matches an expected token.
# [* | expected] -> [*]
word meta-expect-lexeme
	>r
	meta-next-lexeme
	dup r@ string-equal?
	if
		drop
		rdrop
	else
		"expected token: " r@ string-append
		", got: " string-append
		swap string-append
		ct-parser-error
	end
end
compile-only

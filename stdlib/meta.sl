# Compile-time metaprogramming toolkit.
#
# This library keeps compatibility wrappers for raw ct-* words while also
# providing higher-level helpers for common macro/rewrite workflows.

# ---------------------------------------------------------------------------
#  Base wrappers (compatibility)
# ---------------------------------------------------------------------------

word meta-reader-add ct-add-reader-rewrite end
compile-only

word meta-reader-add-named ct-add-reader-rewrite-named end
compile-only

word meta-reader-add-priority ct-add-reader-rewrite-priority end
compile-only

word meta-reader-remove ct-remove-reader-rewrite end
compile-only

word meta-reader-clear ct-clear-reader-rewrites end
compile-only

word meta-reader-list ct-list-reader-rewrites end
compile-only

word meta-reader-enabled-set ct-set-reader-rewrite-enabled end
compile-only

word meta-reader-enabled-get ct-get-reader-rewrite-enabled end
compile-only

word meta-reader-priority-set ct-set-reader-rewrite-priority end
compile-only

word meta-reader-priority-get ct-get-reader-rewrite-priority end
compile-only

word meta-grammar-add ct-add-grammar-rewrite end
compile-only

word meta-grammar-add-named ct-add-grammar-rewrite-named end
compile-only

word meta-grammar-add-priority ct-add-grammar-rewrite-priority end
compile-only

word meta-grammar-remove ct-remove-grammar-rewrite end
compile-only

word meta-grammar-clear ct-clear-grammar-rewrites end
compile-only

word meta-grammar-list ct-list-grammar-rewrites end
compile-only

word meta-grammar-enabled-set ct-set-grammar-rewrite-enabled end
compile-only

word meta-grammar-enabled-get ct-get-grammar-rewrite-enabled end
compile-only

word meta-grammar-priority-set ct-set-grammar-rewrite-priority end
compile-only

word meta-grammar-priority-get ct-get-grammar-rewrite-priority end
compile-only

word meta-macro-limit-set ct-set-macro-expansion-limit end
compile-only

word meta-macro-limit-get ct-get-macro-expansion-limit end
compile-only

word meta-macro-preview-set ct-set-macro-preview end
compile-only

word meta-macro-preview-get ct-get-macro-preview end
compile-only

word meta-macro-register ct-register-text-macro end
compile-only

word meta-macro-register-signature ct-register-text-macro-signature end
compile-only

word meta-word-remove ct-unregister-word end
compile-only

word meta-word-list ct-list-words end
compile-only

word meta-word-exists ct-word-exists? end
compile-only

word meta-word-body ct-get-word-body end
compile-only

word meta-word-asm ct-get-word-asm end
compile-only

word meta-current-token ct-current-token end
compile-only

word meta-parser-pos ct-parser-pos end
compile-only

word meta-parser-remaining ct-parser-remaining end
compile-only

word meta-inject-tokens inject-tokens end
compile-only

# ---------------------------------------------------------------------------
#  Value / list constructors
# ---------------------------------------------------------------------------

word meta-list1
	list-new swap list-append
end
compile-only

word meta-list2
	list-new swap list-append swap list-append
end
compile-only

word meta-list3
	list-new swap list-append swap list-append swap list-append
end
compile-only

word meta-token
	nil token-from-lexeme
end
compile-only

word meta-next-lexeme
	next-token token-lexeme
end
compile-only

word meta-peek-lexeme
	peek-token token-lexeme
end
compile-only

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

# [*, name, from | to] -> [* | name]
word meta-reader-replace-word
	meta-list1
	swap
	meta-list1
	swap
	meta-reader-add-named
end
compile-only

# [*, name, from | to] -> [* | name]
word meta-grammar-replace-word
	meta-list1
	swap
	meta-list1
	swap
	meta-grammar-add-named
end
compile-only

# [*, name, from | to] -> [* | name]
word meta-reader-upsert-word
	>r >r
	dup meta-reader-remove drop
	r> r>
	meta-reader-replace-word
end
compile-only

# [*, name, from | to] -> [* | name]
word meta-grammar-upsert-word
	>r >r
	dup meta-grammar-remove drop
	r> r>
	meta-grammar-replace-word
end
compile-only

word meta-reader-enable
	1 meta-reader-enabled-set
end
compile-only

word meta-reader-disable
	0 meta-reader-enabled-set
end
compile-only

word meta-grammar-enable
	1 meta-grammar-enabled-set
end
compile-only

word meta-grammar-disable
	0 meta-grammar-enabled-set
end
compile-only

# [*, alias | target] -> [* | name]
word meta-reader-alias
	over swap
	meta-reader-replace-word
end
compile-only

# [*, alias | target] -> [* | name]
word meta-grammar-alias
	over swap
	meta-grammar-replace-word
end
compile-only

# ---------------------------------------------------------------------------
#  Macro authoring helpers
# ---------------------------------------------------------------------------

# [*, name | expansionList] -> [*]
word meta-macro-register-0
	list-new swap
	meta-macro-register-signature
end
compile-only

# [*, name | target] -> [*]
word meta-macro-register-alias
	meta-list1
	meta-macro-register-0
end
compile-only

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

word meta-word-drop-if-exists
	dup meta-word-exists
	if
		meta-word-remove
	else
		drop 0
	end
end
compile-only

word meta-word-require
	dup meta-word-exists
	if
		drop
	else
		"required word missing: " swap string-append parse-error
	end
end
compile-only

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

# [* | lexeme] -> [*]
word meta-inject-lexeme
	meta-token
	meta-list1
	meta-inject-tokens
end
compile-only

# [* | wordName] -> [*]
word meta-inject-word-call
	meta-inject-lexeme
end
compile-only

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

# [*, name, patternList | replacementList] -> [* | name]
word meta-reader-upsert-seq
	>r >r
	dup meta-reader-remove drop
	r> r>
	meta-reader-add-named
end
compile-only

# [*, name, patternList | replacementList] -> [* | name]
word meta-grammar-upsert-seq
	>r >r
	dup meta-grammar-remove drop
	r> r>
	meta-grammar-add-named
end
compile-only

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

# [*, name | op] -> [*]
word meta-macro-register-unary-op
	>r
	list-new "x" list-append
	list-new "$x" list-append r> list-append
	meta-macro-register-signature
end
compile-only

# [*, name | valueToken] -> [*]
word meta-macro-register-const
	meta-list1
	meta-macro-register-0
end
compile-only

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

# [*] -> [* | flag]
word meta-parser-at-end?
	meta-parser-remaining 0 ==
end
compile-only

# [*] -> [* | line]
word meta-current-line
	ct-current-token token-line
end
compile-only

# [*] -> [* | column]
word meta-current-column
	ct-current-token token-column
end
compile-only

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

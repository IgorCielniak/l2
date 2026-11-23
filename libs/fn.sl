import ct_store.sl

# fn-error [* | message] -> (raises)
# While a statement is being compiled, errors point at its first token
#; otherwise at the parser's current input.
word fn-error
	"fn.statement" ct-store-get if
		dup token-line swap token-column ct-parser-error-at
	else
		drop ct-parser-error
	end
end
compile-only

# fn-is? [*, element | text] -> [* | flag]
# Body elements are tokens (or, for generated code, strings).
word fn-is?
	swap lexeme string-equal?
end
compile-only

# fn-contains? [*, list | text] -> [* | flag]
word fn-contains?
	>r list-clone                     # list'     (r: text)
begin
	dup list-empty? if drop rdrop 0 exit end
	list-pop-front drop               # list' element
	r@ fn-is? if drop rdrop 1 exit end
again
end
compile-only

# fn-mark-statement [*, ctx | stmt] -> [*, ctx | stmt]
# The statement's first token becomes the position fn-error reports at.
word fn-mark-statement
	dup list-empty? if exit end
	dup 0 list-get "fn.statement" swap ct-store-set
end
compile-only

# Emit a parsed call: a macro becomes `name arg1 arg2 ...` (an argument of
# several tokens is wrapped in a quotation so it stays one value); any other
# word becomes postfix `args... name`.
#call-syntax-emit [*, fnameTok | groups] -> [*]
word call-syntax-emit
	over token-lexeme ct-macro-get-by-name swap drop if
		list-new 2 pick list-append       # fname groups out
		swap                              # fname out groups
		begin
			dup list-empty? if drop break end
			list-pop-front drop           # fname out groups' group
			dup list-length 1 == if
				0 list-get rot swap list-append swap
			else
				>r >r                     # fname out (r: group groups')
				over "`" token-with-lexeme list-append
				r> r> swap >r             # fname out group (r: groups')
				list-extend
				over "`" token-with-lexeme list-append
				r>                        # fname out' groups'
			end
		again
		swap drop                         # out
	else
		list-new swap                     # fname out groups
		begin
			dup list-empty? if drop break end
			list-pop-front drop           # fname out groups' group
			rot swap list-extend swap     # fname out' groups'
		again
		swap list-append                  # out
	end
	ct-parser-inject
end
compile-only

#call-syntax-rewrite [* | fnameToken] -> [* | handled]
word call-syntax-rewrite
	dup token-lexeme identifier? 0 == if drop 0 exit end
	0 ct-parser-peek dup nil? if drop drop 0 exit end
	dup token-lexeme "(" string-equal? 0 == if drop drop 0 exit end
	drop                    # discard peeked '('
	ct-parser-consume drop  # consume '('
	list-new list-new       # fnameTok groups cur
	begin
		ct-parser-eof? if "unterminated call expression" fn-error end
		ct-parser-consume
		dup token-lexeme ")" string-equal? if
			drop list-append break
		end
		dup token-lexeme "," string-equal? if
			drop list-append list-new continue
		end
		list-append
	again
	call-syntax-emit
	1
end
immediate
compile-only

# Remove the fn.dsl pack's token hooks (through their handles).
word fn-dsl-drop-token-hooks       # fn-dsl-drop-token-hooks [*] -> [*]
	fn-dsl-lang-name ct-lang-status drop "token_hooks" map-get drop
	begin
		dup list-empty? if drop exit end
		list-pop-front drop
		"handle" map-get drop ct-lang-remove-token-hook drop
	again
end
compile-only

#extend-syntax [*] -> [*]
word extend-syntax
	fn-dsl-lang-ensure
	fn-dsl-drop-token-hooks
	fn-dsl-lang-name "call-syntax-rewrite" ct-lang-add-token-hook drop
end
immediate
compile-only


word fn-dsl-lang-name
	"fn.dsl"
end
compile-only

word fn-dsl-lang-ensure
	fn-dsl-lang-name ct-lang-create drop
	fn-dsl-lang-name ct-lang-activate drop
end
compile-only

word fn-dsl-lang-status
	fn-dsl-lang-name ct-lang-status
end
compile-only


word fn-op-prec
	lexeme
	dup "+" string-equal? if drop 1 exit end
	dup "-" string-equal? if drop 1 exit end
	dup "*" string-equal? if drop 2 exit end
	dup "/" string-equal? if drop 2 exit end
	dup "%" string-equal? if drop 2 exit end
	drop 0
end
compile-only

# Infix to postfix over a list of token lexemes (shunting-yard). + and -
# bind looser than *, /, and %; all are left-associative; ( and ) group and
# are dropped. Only string elements and balanced parentheses are checked:
# operand/operator placement is left to the caller. (Was the `shunt`
# compiler built-in.)
word fn-infix>postfix              # fn-infix>postfix [* | tokens] -> [* | postfix]
	list-new list-new rot          # out ops tokens
	begin
		dup list-empty? if drop break end
		list-pop-front drop            # out ops tokens' tok
		dup lexeme drop                # elements must be tokens or strings
		swap >r                        # out ops tok          (r: tokens')
		dup "(" fn-is? if
			list-append r> continue
		end
		dup ")" fn-is? if
			drop                           # out ops
			begin
				dup list-empty? if "fn-infix>postfix: unmatched ')'" fn-error end
				list-pop drop                  # out ops' top
				dup "(" fn-is? if drop break end
				rot swap list-append swap      # out' ops'
			again
			r> continue
		end
		dup fn-op-prec 0 == if             # an operand
			rot swap list-append swap      # out' ops
			r> continue
		end
		begin                              # move operators of >= precedence
			over list-empty? if break end
			over list-last                 # out ops tok top
			dup "(" fn-is? if drop break end
			fn-op-prec over fn-op-prec < if break end
			>r list-pop drop rot swap list-append swap r>
		again
		list-append                        # out ops'
		r>
	again
	begin                                  # out ops
		dup list-empty? if drop break end
		list-pop drop
		dup "(" fn-is? if "fn-infix>postfix: unmatched '('" fn-error end
		rot swap list-append swap
	again
end
compile-only

word fn-operator?
	fn-op-prec 0 >
end
compile-only

# fn-check-dup [*, params | name] -> [*, params | name]
# Raises if `name` is already in `params`.
word fn-check-dup
	>r                              # params          (r: name)
	0                               # params idx
begin
	over list-length over <= if     # params idx      (every element checked)
		drop r> exit
	end
	2dup list-get                   # params idx elem
	r@ string-equal? if "duplicate parameter names in fn definition" fn-error end
	1 +                             # params idx+1
again
end
compile-only

# fn-next-lexeme [*] -> [* | lexeme]
word fn-next-lexeme
	ct-parser-eof? if "unexpected end of input in fn definition" fn-error end
	ct-parser-consume token-lexeme
end
compile-only

# fn-params [*] -> [* | params]
# Reads `type name, ...)` after the opening '(' of a parameter list.
word fn-params
	list-new                          # params
begin
	fn-next-lexeme                    # params lex
	dup ")" string-equal? if drop exit end
	dup identifier? 0 == if "invalid parameter type in fn definition" fn-error end
	drop                              # params
	fn-next-lexeme                    # params pname
	dup identifier? 0 == if "invalid parameter name in fn definition" fn-error end
	fn-check-dup                      # params pname
	list-append                       # params
	fn-next-lexeme                    # params sep
	dup "," string-equal? if drop continue end
	dup ")" string-equal? if drop exit end
	"expected ',' or ')' in parameter list" fn-error
again
end
compile-only

# fn-expect [* | lexeme] -> [*]
# Consumes the next token, which must be `lexeme`.
word fn-expect
	dup ct-parser-expected if
		drop drop
		ct-parser-consume drop
		exit
	end
	drop
	"expected '" swap string-append "' in fn definition" string-append fn-error
end
compile-only

# fn-collect-body [*] -> [* | bodyTokens]
# Consumes `{ ... }` and returns the tokens between the braces.
word fn-collect-body
	"{" fn-expect
	"{" "}" ct-parser-collect-balanced
	0 == if "unterminated fn body: missing '}'" fn-error end
end
compile-only

# fn-literal-lexeme? [* | lexeme] -> [* | flag]
word fn-literal-lexeme?
	dup "\"" string-starts-with? if drop 1 exit end
	"'" string-starts-with?
end
compile-only

# fn-split-tokens [* | tokens] -> [* | pieces]
# The default reader keeps operators such as `a+b` in one token; fn bodies
# split them.  Each token's text is re-tokenized with the fn operators and
# punctuation as separators, and every piece is cut from its token with
# token-slice, so it keeps its exact source position.  String and character
# literals stay whole.
word fn-split-tokens
	ct-tokenizer-get-config >r                     # tokens     (r: saved config)
	map-new "separators" list-new
		"+" list-append "-" list-append "*" list-append "/" list-append "%" list-append
		";" list-append "," list-append "(" list-append ")" list-append
		"{" list-append "}" list-append
	map-set ct-tokenizer-set-config
	list-new swap list-clone                       # out tokens
begin
	dup list-empty? if drop r> ct-tokenizer-set-config exit end
	list-pop-front drop                            # out tokens' tok
	dup token-lexeme fn-literal-lexeme? if
		rot swap list-append swap                  # out' tokens'
		continue
	end
	dup token-lexeme "<fn>" ct-tokenizer-run       # out tokens' tok pieces
	begin
		dup list-empty? if drop drop break end
		list-pop-front drop                        # out tokens' tok pieces' piece
		dup token-origin source-origin-start
		swap token-origin source-origin-end        # out tokens' tok pieces' start end
		3 pick rot rot token-slice                 # out tokens' tok pieces' pieceTok
		4 pick swap list-append drop               # out tokens' tok pieces'
	again
again
end
compile-only

word fn-body->postfix-program      # fn-body->postfix-program [* | bodyLexemes] -> [* | postfix]
	list-new swap list-new swap      # out cur body
begin
	dup list-empty? if
		drop                          # out cur
		dup list-length 0 > if
			fn-infix>postfix                     # out postfix
			list-extend              # out'
			exit
		end
		drop                          # out
		dup list-length 0 == if "empty function body" fn-error end
		list-pop drop                 # out' tok
		dup "drop" fn-is? 0 == if "invalid trailing function statement" fn-error end
		drop                          # out'
		exit
	end

	list-pop-front drop              # out cur body' tok
	swap >r                          # out cur tok      (r: body')

	dup "return" fn-is? if
		drop
		r>
		continue
	end

	dup ";" fn-is? if
		drop
		dup list-length 0 == if
			r>
			continue
		end
		fn-infix>postfix                          # out postfix
		list-extend                    # out'
		"drop" list-append             # out''
		list-new                       # out'' cur'
		r>
		continue
	end

	list-append                      # out cur'
	r>
	continue
again
end
compile-only


word fn-body->tokens                # fn-body->tokens [* | bodyLexemes] -> [* | tokens]
	fn-body->postfix-program
end
compile-only

word fn-emit-prologue             # fn-emit-prologue [*, params | out] -> [*, params | out]
	over list-length              # params out n
begin
	dup 0 > if
		1 -                      # params out n-1
		>r                       # params out      (r: n-1)
		">r" list-append         # params out'
		r>                       # params out' n-1
		continue
	end
	drop                         # params out
	exit
again
end
compile-only

word fn-emit-epilogue             # fn-emit-epilogue [*, params | out] -> [* | out]
	over list-length >r           # params out   (r: n)
begin
	r> dup 0 > if
		1 - >r
		"rdrop" list-append
		continue
	end
	drop                          # drop counter
	swap drop                     # out
	exit
again
end
compile-only

word fn-translate-prologue-loop   # fn-translate-prologue-loop [* | count] -> [*]
	dup 0 > if
		1 -
		0 rpick ">r" list-append drop
		fn-translate-prologue-loop
		end
	drop
end
compile-only

word fn-translate-epilogue-loop   # fn-translate-epilogue-loop [* | count] -> [*]
	dup 0 > if
		1 -
		0 rpick "rdrop" list-append drop
		fn-translate-epilogue-loop
		end
	drop
end
compile-only

word fn-param-index                # fn-param-index [*, params | name] -> [*, params, idx | flag]
	>r                             # params        (r: name)
	0                              # params idx

begin
	over list-length over swap >= if    # params idx flag (idx >= len?)
		drop                      # params
		r> drop                   # drop name
		-1 0 exit                 # params -1 0
	end                        # params idx
	over over list-get            # params idx elem
	0 rpick string-equal?               # params idx flag
	if
		r> drop                   # drop name
		1 exit                    # params idx 1
	end
	drop                          # params idx
	1 +                           # params idx+1
again
end
compile-only


word fn-build-param-map            # fn-build-param-map [* | params] -> [*, params | map]
	map-new                         # params map
	0                               # params map idx
	begin
		2 pick list-length            # params map idx len
		over swap >= if               # params map idx flag
			drop                        # params map
			exit
		end                          # params map idx
		2 pick over list-get          # params map idx name
		swap                          # params map name idx
		dup >r                        # params map name idx   (r: idx)
		map-set                       # params map'
		r> 1 +                         # params map' idx'
		continue
	again
end
compile-only


word fn-build-param-type-map       # fn-build-param-type-map [* | params] -> [* | typeMap]
	map-new                         # params typeMap
	0                               # params typeMap idx
	begin
		2 pick list-length            # params typeMap idx len
		over swap >= if               # params typeMap idx flag
			drop                        # params typeMap
			swap drop                   # typeMap
			exit
		end                          # params typeMap idx
		2 pick over list-get          # params typeMap idx name
		"number"                      # params typeMap idx name type
		rot >r                        # params typeMap name type   (r: idx)
		map-set                       # params typeMap'
		r> 1 +                        # params typeMap' idx'
		continue
	again
end
compile-only


word fn-map-increment-values       # fn-map-increment-values [* | map] -> [* | map]
	dup map-clone                   # map original clone
	over map-keys                   # map original clone keys
begin
	dup list-empty? if
		drop
		swap drop
		exit
	end
	dup list-pop-front drop         # map original clone keys keys' key
	rot drop                        # map original clone keys' key
	dup                             # map original clone keys' key key
	4 pick swap map-get             # map original clone keys' key value ok
	0 == if "internal fn variable map corruption" fn-error end
	1 +                             # map original clone keys' key value+1
	3 pick                          # map original clone keys' key value+1 clone
	rot                             # map original clone keys' value+1 clone key
	rot                             # map original clone keys' clone key value+1
	map-set                         # map original clone keys' clone'
	rot                             # map original keys' clone' clone
	drop                            # map original keys' clone'
	swap                            # map original clone' keys'
	continue
again
end
compile-only


word fn-vars-push-binding          # fn-vars-push-binding [*, vars | name] -> [* | vars']
	swap
	fn-map-increment-values
	swap
	0 map-set
end
compile-only


word fn-list-copy                  # fn-list-copy [* | list] -> [* | copy]
	list-new swap list-extend
end
compile-only


word fn-call-close-index           # fn-call-close-index [* | expr] -> [*, closeIdx | found]
	2 0                               # expr idx depth
begin
	2 pick list-length               # expr idx depth len
	2 pick over >= if                # expr idx depth len flag
		drop
		drop
		drop
		drop
		-1 0 exit
	end
	drop                             # expr idx depth

	>r                               # expr idx            (r: depth)
	over over list-get               # expr idx tok
	r>                               # expr idx tok depth
	swap                             # expr idx depth tok
	dup "(" fn-is? if
		drop
		1 +                            # expr idx depth+1
		swap 1 + swap                  # expr idx+1 depth+1
		continue
	end

	dup ")" fn-is? if
		drop
		dup 0 == if
			drop
			swap drop
			1 exit
		end
		1 -                            # expr idx depth-1
		swap 1 + swap                  # expr idx+1 depth-1
		continue
	end

	drop
	swap 1 + swap                    # expr idx+1 depth
again
end
compile-only


word fn-call-syntax-expr?          # fn-call-syntax-expr? [* | expr] -> [* | flag]
	dup list-length 3 < if drop 0 exit end
	dup 0 list-get identifier? 0 == if drop 0 exit end
	dup 1 list-get "(" fn-is? 0 == if drop 0 exit end

	dup fn-call-close-index
	if
		over list-length swap 1 + ==
		swap drop
		exit
	end

	drop
	drop
	0
end
compile-only


word fn-call-args-split            # fn-call-args-split [* | inner] -> [* | args]
	list-new swap list-new swap 0 swap   # args cur depth inner
begin
	dup list-empty? if
		drop
		drop
		dup list-empty? if
			drop
		else
			over swap list-append
			swap drop
		end
		exit
	end

	list-pop-front drop               # args cur depth inner' tok
	swap >r                           # args cur depth tok   (r: inner')

	dup "(" fn-is? if
		swap >r                          # args cur tok         (r: depth inner')
		list-append                      # args cur'
		r> 1 +                           # args cur' depth+1
		r>
		continue
	end

	dup ")" fn-is? if
		swap                             # args cur tok depth
		dup 0 > if
			1 - >r                         # args cur tok         (r: depth-1 inner')
			list-append                    # args cur'
			r>
			r>
			continue
		end
		drop                             # args cur tok
		list-append                      # args cur'
		0                                # args cur' depth
		r>
		continue
	end

	dup "," fn-is? if
		swap                             # args cur tok depth
		dup 0 == if
			drop                            # args cur tok
			drop                            # args cur
			over swap list-append           # args'
			list-new                        # args' cur'
			0                               # args' cur' depth
			r>
			continue
		end
		>r                               # args cur tok         (r: depth inner')
		list-append                      # args cur'
		r>
		r>
		continue
	end

	swap                               # args cur tok depth
	>r                                 # args cur tok         (r: depth inner')
	list-append                        # args cur'
	r>
	r>
	continue
again
end
compile-only


word fn-rewrite-call-expr          # fn-rewrite-call-expr [* | expr] -> [*, expr | postfix_flag]
	dup list-length 3 < if 0 exit end
	dup 0 list-get identifier? 0 == if 0 exit end
	dup 1 list-get "(" fn-is? 0 == if 0 exit end
	dup "," fn-contains? if 0 exit end
	dup "+" fn-contains? if 0 exit end
	dup "-" fn-contains? if 0 exit end
	dup "*" fn-contains? if 0 exit end
	dup "/" fn-contains? if 0 exit end
	dup "%" fn-contains? if 0 exit end
	fn-infix>postfix
	list-reverse
	1
end
compile-only


word fn-list-drop-front-n          # fn-list-drop-front-n [*, list | n] -> [* | list]
	>r                                # list      (r: n)
begin
	r> dup 0 > if                     # list n
		1 - >r                          # list      (r: n-1)
		dup list-empty? if "invalid fn statement shape" fn-error end
		list-pop-front drop drop        # list'
		continue
	end
	drop                              # list
	exit
again
end
compile-only


word fn-stmt-expr-from             # fn-stmt-expr-from [*, stmt | n] -> [* | expr]
	swap
	fn-list-copy
	swap
	fn-list-drop-front-n
end
compile-only


word fn-valid-type?
	dup "int" string-equal? if drop 1 exit end
	dup "number" string-equal? if drop 1 exit end
	dup "float" string-equal? if drop 1 exit end
	drop 0
end
compile-only


word fn-stmt-decl?
	dup list-length 5 < if drop 0 exit end
	dup 0 list-get "let" fn-is? 0 == if drop 0 exit end
	dup 1 list-get identifier? 0 == if drop 0 exit end
	dup 2 list-get identifier? 0 == if drop 0 exit end
	dup 3 list-get "=" fn-is? 0 == if drop 0 exit end
	drop 1
end
compile-only


word fn-stmt-assign?
	dup list-length 3 < if drop 0 exit end
	dup 0 list-get identifier? 0 == if drop 0 exit end
	dup 1 list-get "=" fn-is? 0 == if drop 0 exit end
	drop 1
end
compile-only


word fn-expr-has-operator?         # fn-expr-has-operator? [* | expr] -> [* | flag]
	fn-list-copy
	begin
		dup list-empty? if
			drop
			0 exit
		end
		list-pop-front drop           # expr' tok
		swap >r                       # tok      (r: expr')
		fn-operator? if
			rdrop
			1 exit
		end
		r>
		continue
	again
end
compile-only


word fn-token-type                 # fn-token-type [*, typeMap | tok] -> [* | type]
	lexeme
	dup try-string>int
	if
		drop
		drop
		drop
		"int" exit
	end
	drop
	map-get                           # type ok
	if
		exit
	end
	drop
	"number"
end
compile-only


word fn-expr-type                  # fn-expr-type [*, expr | typeMap] -> [* | type]
	>r                                # expr          (r: typeMap)
	dup fn-expr-has-operator? if
		drop
		r> drop
		"number" exit
	end
	dup list-length 1 == if
		dup 0 list-get                  # expr tok
		r>                              # expr tok typeMap
		swap                            # expr typeMap tok
		fn-token-type                   # expr type
		swap drop
		exit
	end
	drop
	r> drop
	"number"
end
compile-only


word fn-type-compatible?           # fn-type-compatible? [*, targetType | exprType] -> [* | flag]
	2dup string-equal? if
		drop
		drop
		1 exit
	end
	swap
	dup "number" string-equal? if
		drop
		"int" string-equal?
		exit
	end
	drop
	drop
	0
end
compile-only


word fn-parse-let-stmt             # fn-parse-let-stmt [* | stmt] -> [*, type, name | expr]
	dup fn-stmt-decl? 0 == if "invalid let statement in fn body" fn-error end
	dup 1 list-get lexeme
	over 2 list-get lexeme
	rot 4 fn-stmt-expr-from
end
compile-only


word fn-parse-assign-stmt          # fn-parse-assign-stmt [* | stmt] -> [*, name | expr]
	dup fn-stmt-assign? 0 == if "invalid assignment statement in fn body" fn-error end
	dup 0 list-get lexeme
	swap 2 fn-stmt-expr-from
end
compile-only


word fn-split-statements           # fn-split-statements [* | bodyLexemes] -> [* | statements]
	list-new swap list-new swap      # statements cur body
begin
	dup list-empty? if
		drop                          # statements cur
		dup list-length 0 > if
			over swap list-append
			swap drop
		else
			drop
		end
		dup list-empty? if "empty function body" fn-error end
		exit
	end

	list-pop-front drop              # statements cur body' tok
	swap >r                          # statements cur tok   (r: body')

	dup "return" fn-is? if
		drop
		r>
		continue
	end

	dup ";" fn-is? if
		drop
		dup list-length 0 == if
			r>
			continue
		end
		over swap list-append
		swap drop
		list-new
		r>
		continue
	end

	list-append                      # statements cur'
	r>
	continue
again
end
compile-only


word fn-out-append-repeat          # fn-out-append-repeat [*, out, count | lexeme] -> [* | out]
	>r                                # out count   (r: lexeme)
begin
	dup 0 > if
		1 -
		swap r@ list-append swap
		continue
	end
	drop
	rdrop
	exit
again
end
compile-only


word fn-ctx-get                    # fn-ctx-get [*, ctx | key] -> [* | value]
	map-get
	if
		exit
	end
	drop
	"internal fn compiler context key is missing" fn-error
end
compile-only


word fn-ctx-set-out                # fn-ctx-set-out [*, ctx | out] -> [* | ctx]
	swap "out" rot map-set
end
compile-only


word fn-ctx-set-vars               # fn-ctx-set-vars [*, ctx | vars] -> [* | ctx]
	swap "vars" rot map-set
end
compile-only


word fn-ctx-set-types              # fn-ctx-set-types [*, ctx | types] -> [* | ctx]
	swap "types" rot map-set
end
compile-only


word fn-ctx-set-locals             # fn-ctx-set-locals [*, ctx | n] -> [* | ctx]
	swap "locals" rot map-set
end
compile-only


word fn-ctx-set-params-count       # fn-ctx-set-params-count [*, ctx | n] -> [* | ctx]
	swap "params_count" rot map-set
end
compile-only


word fn-ctx-set-out-vars           # fn-ctx-set-out-vars [*, ctx, out | vars] -> [* | ctx]
	>r                                # ctx out      (r: vars)
	fn-ctx-set-out                    # ctx'
	r>
	fn-ctx-set-vars
end
compile-only


word fn-ctx-inc-locals             # fn-ctx-inc-locals [* | ctx] -> [* | ctx]
	dup "locals" fn-ctx-get
	1 +
	fn-ctx-set-locals
end
compile-only


word fn-ctx-out-vars               # fn-ctx-out-vars [* | ctx] -> [*, ctx, out | vars]
	dup "out" fn-ctx-get
	over "vars" fn-ctx-get
end
compile-only


word fn-append-expression          # fn-append-expression [*, out, vars | expr] -> [*, out | vars]
	fn-rewrite-call-expr
	if
	else
		fn-infix>postfix
	end
	rot
	swap
	fn-translate-postfix-loop
	swap
end
compile-only


word fn-compile-expr-stmt          # fn-compile-expr-stmt [*, ctx, stmt | isLast] -> [* | ctx]
	>r                                # ctx stmt    (r: isLast)
	dup list-empty? if "empty function statement in fn body" fn-error end
	over fn-ctx-out-vars              # ctx stmt ctx out vars
	3 pick                            # ctx stmt ctx out vars stmt
	fn-append-expression              # ctx stmt ctx out' vars'
	fn-ctx-set-out-vars               # ctx stmt ctx'
	swap drop
	swap drop                         # ctx'

	r> if
		exit
	end

	dup "out" fn-ctx-get
	"drop" list-append
	fn-ctx-set-out
end
compile-only


word fn-compile-let-stmt           # fn-compile-let-stmt [*, ctx, stmt | isLast] -> [* | ctx]
	if "invalid trailing function statement in fn body" fn-error end
	fn-parse-let-stmt                 # ctx type name expr

	dup list-empty? if "missing initializer expression in let statement" fn-error end
	2 pick fn-valid-type? 0 == if "unsupported fn variable type in let statement" fn-error end

	3 pick "types" fn-ctx-get
	2 pick map-get if "duplicate variable in let statement" fn-error end
	drop

	dup 4 pick "types" fn-ctx-get fn-expr-type   # ctx type name expr exprType
	3 pick swap fn-type-compatible? 0 == if "fn type error: incompatible let initializer" fn-error end

	swap >r swap >r                  # ctx expr      (r: type name)

	over fn-ctx-out-vars              # ctx expr ctx out vars
	3 pick                            # ctx expr ctx out vars expr
	fn-append-expression              # ctx expr ctx out' vars'
	fn-ctx-set-out-vars               # ctx expr ctx'
	swap drop
	swap drop                         # ctx'

	dup "out" fn-ctx-get
	">r" list-append
	fn-ctx-set-out                    # ctx'

	r> r>                             # ctx' type name

	2 pick "vars" fn-ctx-get          # ctx' type name vars
	1 pick fn-vars-push-binding       # ctx' type name vars'
	swap >r                           # ctx' type vars'   (r: name)
	swap >r                           # ctx' vars'        (r: type name)
	fn-ctx-set-vars                   # ctx''
	r> r>                             # ctx'' type name

	2 pick "types" fn-ctx-get         # ctx'' type name types
	swap
	rot                               # ctx'' types name type
	map-set                           # ctx'' types'
	fn-ctx-set-types

	fn-ctx-inc-locals
end
compile-only


word fn-compile-assign-stmt        # fn-compile-assign-stmt [*, ctx, stmt | isLast] -> [* | ctx]
	if "invalid trailing function statement in fn body" fn-error end
	fn-parse-assign-stmt              # ctx name expr

	dup list-empty? if "missing expression in assignment statement" fn-error end

	2 pick "types" fn-ctx-get
	2 pick map-get
	0 == if "assignment to undeclared variable in fn body" fn-error end

	1 pick 4 pick "types" fn-ctx-get fn-expr-type   # ctx name expr targetType exprType
	1 pick swap fn-type-compatible? 0 == if "fn type error: incompatible assignment expression" fn-error end
	drop

	swap >r                           # ctx expr      (r: name)

	over fn-ctx-out-vars              # ctx expr ctx out vars
	3 pick                            # ctx expr ctx out vars expr
	fn-append-expression              # ctx expr ctx out' vars'
	fn-ctx-set-out-vars               # ctx expr ctx'
	swap drop
	swap drop                         # ctx'

	dup "out" fn-ctx-get
	">r" list-append
	fn-ctx-set-out                    # ctx'

	r>                                # ctx' name
	over "vars" fn-ctx-get
	swap fn-vars-push-binding         # ctx' vars'
	fn-ctx-set-vars

	fn-ctx-inc-locals
end
compile-only


word fn-compile-statement          # fn-compile-statement [*, ctx, stmt | isLast] -> [* | ctx]
	1 pick fn-stmt-decl? if
		fn-compile-let-stmt
		exit
	end
	1 pick fn-stmt-assign? if
		fn-compile-assign-stmt
		exit
	end
	fn-compile-expr-stmt
end
compile-only


word fn-compile-statements         # fn-compile-statements [*, ctx | statements] -> [* | ctx]
begin
	dup list-empty? if
		drop
		exit
	end
	list-pop-front drop               # ctx statements' stmt
	swap >r                           # ctx stmt    (r: statements')
	fn-mark-statement
	0 rpick list-empty? if
		1
	else
		0
	end
	fn-compile-statement              # ctx'
	r>
	continue
again
end
compile-only


# fn-translate-token [*, out, map | tok] -> [*, out | map]
# A number keeps its token; a variable becomes `N rpick`, both tokens
# anchored at the variable itself; anything else keeps its token.
word fn-translate-token
	dup lexeme try-string>int nip if   # out map tok
		swap >r list-append r> exit
	end
	over over lexeme map-get if        # out map tok idx
		over swap int>string token-with-lexeme   # out map tok idxTok
		swap "rpick" token-with-lexeme           # out map idxTok rpickTok
		>r >r swap r> list-append r> list-append swap
		exit
	end
	drop                               # out map tok
	swap >r list-append r>
end
compile-only


word fn-translate-postfix-loop     # fn-translate-postfix-loop [*, map, out | postfix] -> [*, map | out]
	begin
		dup list-empty? if
			drop
			exit
		end
		list-pop-front drop          # map out postfix' tok
		swap >r                      # map out tok   (r: postfix')
		>r swap r>                   # out map tok   (r: postfix')
		fn-translate-token           # out map
		swap                         # map out
		r>                           # map out postfix'
		continue
	again
end
compile-only


word fn-translate-postfix          # fn-translate-postfix [*, postfix | params] -> [* | out]
	swap                             # params postfix
	list-new                         # params postfix out

	# prologue: stash args on return stack (emit ">r")
	swap >r                          # params out       (r: postfix)
	fn-emit-prologue                  # params out
	r> swap                           # params postfix out

	# build param map (name -> index)
	2 pick fn-build-param-map         # params postfix out params map
	>r drop r>                        # params postfix out map
	# reorder to: params map out postfix
	swap >r swap r> swap              # params map out postfix

	# translate tokens
	fn-translate-postfix-loop          # params map out
	# drop map, emit epilogue
	swap drop                         # params out
	fn-emit-epilogue                   # out
end
compile-only

word fn-build-body                # fn-build-body [*, bodyLexemes | params] -> [* | body]
	swap >r                         # params        (r: bodyLexemes)

	list-new
	fn-emit-prologue                # params out

	over fn-build-param-map         # params out params vars
	swap drop                       # params out vars
	2 pick fn-build-param-type-map  # params out vars types
	3 pick list-length              # params out vars types paramsCount

	>r >r >r >r                    # params         (r: paramsCount types vars out bodyLexemes)

	map-new                         # params ctx
	r> fn-ctx-set-out               # params ctx
	r> fn-ctx-set-vars              # params ctx
	r> fn-ctx-set-types             # params ctx
	0 fn-ctx-set-locals             # params ctx
	r> fn-ctx-set-params-count      # params ctx

	swap drop                       # ctx
	r>                              # ctx bodyLexemes
	fn-split-statements             # ctx statements
	fn-compile-statements           # ctx

	dup "params_count" fn-ctx-get
	over "locals" fn-ctx-get
	+                               # ctx totalDropCount
	over "out" fn-ctx-get          # ctx totalDropCount out
	swap                            # ctx out totalDropCount
	"rdrop" fn-out-append-repeat    # ctx out'
	swap drop                       # out'
end
compile-only

# fn-emit-definition [*, body, nameTok | paramCount] -> [*]
# Emit `word <name> <body...> end stack-effect <name> <params> 1`.  Body
# elements are tokens, which keep their exact source origins, or generated
# lexeme strings (`>r`, `rdrop`, `drop`), anchored at the token before them
# (the name at the start).  The declared effect (the parameters in, one value
# out) lets the stack checker verify the function and its callers.
word fn-emit-definition
	>r dup >r                                                 # body nameTok   (r: count nameTok)
	dup "word" token-with-lexeme list-new swap list-append   # body nameTok out
	over list-append                                          # body nameTok out
	rot list-clone rot                                        # out body anchor
begin
	over list-empty? if break end
	over list-pop-front drop nip                              # out body anchor elem
	dup token? 0 == if over swap token-with-lexeme end        # out body anchor tok
	nip                                                       # out body tok
	2 pick over list-append drop
again
	"end" token-with-lexeme                                   # out body endTok
	rot swap list-append nip                                  # out
	r> r> >r                                                  # out nameTok   (r: count)
	dup "stack-effect" token-with-lexeme                      # out nameTok effectTok
	rot swap list-append                                      # nameTok out
	over list-append                                          # nameTok out
	over r> int>string token-with-lexeme list-append          # nameTok out
	swap "1" token-with-lexeme list-append                    # out
	list>syntax emit
end
compile-only

word fn
	ct-parser-eof? if "missing function name after 'fn'" fn-error end
	ct-parser-consume                 # nameTok
	dup token-lexeme identifier? 0 == if "invalid function name for 'fn'" fn-error end
	>r                                # (r: nameTok)
	"(" fn-expect                     # consume '('
	fn-params                         # params
	dup list-length >r                # (r: nameTok paramCount)
	fn-collect-body                   # params bodyTokens
	fn-split-tokens                   # params pieces
	"fn.statement" ct-store-delete
	swap                              # pieces params
	fn-build-body                     # body
	"fn.statement" ct-store-delete
	r> r> swap fn-emit-definition     # body nameTok paramCount
end
immediate
compile-only


# High-level DSL convenience aliases.
macro defn `fn` emit ;

macro function `fn` emit ;

# The macro's metadata map (empty when it has none or is not a macro).
word fn-dsl-macro-metadata         # fn-dsl-macro-metadata [* | name] -> [* | metadata]
	ct-macro-get-by-name if "metadata" map-get drop else drop map-new end
end
compile-only

# Store one key in a macro's metadata.
word fn-dsl-set-metadata           # fn-dsl-set-metadata [*, name, key | value] -> [*]
	>r >r dup fn-dsl-macro-metadata     # name metadata (r: value key)
	r> r> map-set                       # name metadata'
	>r map-new "metadata" r> map-set    # name patch
	ct-macro-set-by-name drop
end
compile-only

word fn-dsl-set-doc                # fn-dsl-set-doc [*, name | doc] -> [*]
	"doc" swap fn-dsl-set-metadata
end
compile-only

word fn-dsl-set-attrs              # fn-dsl-set-attrs [*, name | kind] -> [*]
	over "category" "fn.dsl" fn-dsl-set-metadata
	"kind" swap fn-dsl-set-metadata
end
compile-only

word fn-dsl-rewrite-run-scoped    # fn-dsl-rewrite-run-scoped [*, stage | token-list] -> [*, token-list | patches]
	ct-rewrite-scope-push drop
	ct-rewrite-run-on-list
	ct-rewrite-scope-pop static-assert
end
compile-only

word fn-dsl-token-lexemes         # fn-dsl-token-lexemes [* | tokens] -> [* | lexemes]
	list-new swap
begin
	dup list-empty? if
		drop
		exit
	end
	list-pop-front drop
	swap >r
	token-lexeme
	list-append
	r>
	continue
again
end
compile-only

word fn-dsl-parser-tail-lexemes   # fn-dsl-parser-tail-lexemes [*] -> [* | list]
	ct-parser-tail fn-dsl-token-lexemes
end
compile-only

word fn-dsl-parser-diff-lexemes   # fn-dsl-parser-diff-lexemes [*, start | end] -> [* | list]
	ct-parser-slice fn-dsl-token-lexemes
end
compile-only

# Library macros.  fn-dsl-register-macros adopts them into the fn.dsl
# language pack and records their documentation as macro metadata.
# thread-first: pipe(value, func) => value func
macro pipe (value func) `$value $func` emit ;
# alias of pipe
macro thread (value func) `$value $func` emit ;
# thread-first with one extra argument
macro pipe2 (value arg func) `$value $arg $func` emit ;
# thread-first with two extra arguments
macro pipe3 (value arg1 arg2 func) `$value $arg1 $arg2 $func` emit ;
# invoke side-effect function and keep value
macro tap (value func) `$value dup $func` emit ;
# expression-level conditional
macro when (cond expr) `$cond if $expr else 0 end` emit ;
# inverse expression-level conditional
macro unless (cond expr) `$cond if 0 else $expr end` emit ;
# expression-level if/else
macro ifelse (cond on_true on_false) `$cond if $on_true else $on_false end` emit ;
# compose two unary functions
macro compose2 (f g x) `$x $g $f` emit ;
# compose three unary functions
macro compose3 (f g h x) `$x $h $g $f` emit ;
# variadic thread-first pipeline
macro chain (value funcs) `$value $funcs` emit ;
# apply function to variadic args
macro invoke (func args) `$args $func` emit ;
# tap with one function argument
macro tapn (value func args) `$value dup $args $func drop` emit ;
# apply binary function
macro apply2 (func a b) `$a $b $func` emit ;
# apply ternary function
macro apply3 (func a b c) `$a $b $c $func` emit ;
# thread-first with three extra arguments
macro pipe4 (value arg1 arg2 arg3 func) `$value $arg1 $arg2 $arg3 $func` emit ;
# thread-first with four extra arguments
macro pipe5 (value arg1 arg2 arg3 arg4 func) `$value $arg1 $arg2 $arg3 $arg4 $func` emit ;
# apply quaternary function
macro apply4 (func a b c d) `$a $b $c $d $func` emit ;
# apply 5-arity function
macro apply5 (func a b c d e) `$a $b $c $d $e $func` emit ;
# compose four unary functions
macro compose4 (f g h k x) `$x $k $h $g $f` emit ;
# compose five unary functions
macro compose5 (f g h k m x) `$x $m $k $h $g $f` emit ;
# apply two unary functions to same input
macro juxt2 (f g x) `$x $f $x $g` emit ;
# apply three unary functions to same input
macro juxt3 (f g h x) `$x $f $x $g $x $h` emit ;
# reverse first two arguments before call
macro flip2 (func a b) `$b $a $func` emit ;
# expression guard with explicit fallback
macro guard (cond on_true on_false) `$cond if $on_true else $on_false end` emit ;
# evaluate expression when condition is false
macro when-not (cond expr) `$cond if 0 else $expr end` emit ;
# alias of pipe/thread-first
macro thrush (value func) `$value $func` emit ;
# return value unchanged
macro identity (value) `$value` emit ;
# always return first argument
macro const (value _ignored) `$value` emit ;
# return fallback only when value is falsey
macro default (value fallback) `$value dup if else drop $fallback end` emit ;
# branch on value presence while preserving happy-path value
macro maybe (value on_some on_none) `$value dup if $on_some else drop $on_none end` emit ;
# alias of tap
macro keep (value func) `$value dup $func` emit ;
# conditionally apply function and keep original value otherwise
macro pipe-if (value cond func) `$cond if $value $func else $value end` emit ;
# inverse conditional pipeline
macro pipe-unless (value cond func) `$cond if $value else $value $func end` emit ;
# thread-last with one fixed argument
macro pipe-last2 (value arg func) `$arg $value $func` emit ;
# thread-last with two fixed arguments
macro pipe-last3 (value arg1 arg2 func) `$arg1 $arg2 $value $func` emit ;
# thread-last with three fixed arguments
macro pipe-last4 (value arg1 arg2 arg3 func) `$arg1 $arg2 $arg3 $value $func` emit ;
# thread-first with five extra arguments
macro pipe6 (value arg1 arg2 arg3 arg4 arg5 func) `$value $arg1 $arg2 $arg3 $arg4 $arg5 $func` emit ;
# apply 6-arity function
macro apply6 (func a b c d e f) `$a $b $c $d $e $f $func` emit ;
# compose six unary functions
macro compose6 (f g h k m n x) `$x $n $m $k $h $g $f` emit ;
# apply four unary functions to the same input
macro juxt4 (f g h k x) `$x $f $x $g $x $h $x $k` emit ;
# apply five unary functions to the same input
macro juxt5 (f g h k m x) `$x $f $x $g $x $h $x $k $x $m` emit ;
# apply six unary functions to the same input
macro juxt6 (f g h k m n x) `$x $f $x $g $x $h $x $k $x $m $x $n` emit ;
# tap through two side-effect functions while preserving value
macro tap2 (value f g) `$value dup $f dup $g` emit ;
# tap through three side-effect functions while preserving value
macro tap3 (value f g h) `$value dup $f dup $g dup $h` emit ;
# statement-oriented conditional block
macro when-do (cond body) `$cond if $body end` emit ;
# inverse statement-oriented conditional block
macro unless-do (cond body) `$cond if else $body end` emit ;

word fn-dsl-register-macros
	fn-dsl-lang-name "pipe" ct-lang-register-macro drop
	"pipe" "thread-first: pipe(value, func) => value func" fn-dsl-set-doc
	"pipe" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "thread" ct-lang-register-macro drop
	"thread" "alias of pipe" fn-dsl-set-doc
	"thread" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "pipe2" ct-lang-register-macro drop
	"pipe2" "thread-first with one extra argument" fn-dsl-set-doc
	"pipe2" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "pipe3" ct-lang-register-macro drop
	"pipe3" "thread-first with two extra arguments" fn-dsl-set-doc
	"pipe3" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "tap" ct-lang-register-macro drop
	"tap" "invoke side-effect function and keep value" fn-dsl-set-doc
	"tap" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "when" ct-lang-register-macro drop
	"when" "expression-level conditional" fn-dsl-set-doc
	"when" "control" fn-dsl-set-attrs
	fn-dsl-lang-name "unless" ct-lang-register-macro drop
	"unless" "inverse expression-level conditional" fn-dsl-set-doc
	"unless" "control" fn-dsl-set-attrs
	fn-dsl-lang-name "ifelse" ct-lang-register-macro drop
	"ifelse" "expression-level if/else" fn-dsl-set-doc
	"ifelse" "control" fn-dsl-set-attrs
	fn-dsl-lang-name "compose2" ct-lang-register-macro drop
	"compose2" "compose two unary functions" fn-dsl-set-doc
	"compose2" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "compose3" ct-lang-register-macro drop
	"compose3" "compose three unary functions" fn-dsl-set-doc
	"compose3" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "chain" ct-lang-register-macro drop
	"chain" "variadic thread-first pipeline" fn-dsl-set-doc
	"chain" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "invoke" ct-lang-register-macro drop
	"invoke" "apply function to variadic args" fn-dsl-set-doc
	"invoke" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "tapn" ct-lang-register-macro drop
	"tapn" "tap with one function argument" fn-dsl-set-doc
	"tapn" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "apply2" ct-lang-register-macro drop
	"apply2" "apply binary function" fn-dsl-set-doc
	"apply2" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "apply3" ct-lang-register-macro drop
	"apply3" "apply ternary function" fn-dsl-set-doc
	"apply3" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "pipe4" ct-lang-register-macro drop
	"pipe4" "thread-first with three extra arguments" fn-dsl-set-doc
	"pipe4" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "pipe5" ct-lang-register-macro drop
	"pipe5" "thread-first with four extra arguments" fn-dsl-set-doc
	"pipe5" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "apply4" ct-lang-register-macro drop
	"apply4" "apply quaternary function" fn-dsl-set-doc
	"apply4" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "apply5" ct-lang-register-macro drop
	"apply5" "apply 5-arity function" fn-dsl-set-doc
	"apply5" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "compose4" ct-lang-register-macro drop
	"compose4" "compose four unary functions" fn-dsl-set-doc
	"compose4" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "compose5" ct-lang-register-macro drop
	"compose5" "compose five unary functions" fn-dsl-set-doc
	"compose5" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "juxt2" ct-lang-register-macro drop
	"juxt2" "apply two unary functions to same input" fn-dsl-set-doc
	"juxt2" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "juxt3" ct-lang-register-macro drop
	"juxt3" "apply three unary functions to same input" fn-dsl-set-doc
	"juxt3" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "flip2" ct-lang-register-macro drop
	"flip2" "reverse first two arguments before call" fn-dsl-set-doc
	"flip2" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "guard" ct-lang-register-macro drop
	"guard" "expression guard with explicit fallback" fn-dsl-set-doc
	"guard" "control" fn-dsl-set-attrs
	fn-dsl-lang-name "when-not" ct-lang-register-macro drop
	"when-not" "evaluate expression when condition is false" fn-dsl-set-doc
	"when-not" "control" fn-dsl-set-attrs
	fn-dsl-lang-name "thrush" ct-lang-register-macro drop
	"thrush" "alias of pipe/thread-first" fn-dsl-set-doc
	"thrush" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "identity" ct-lang-register-macro drop
	"identity" "return value unchanged" fn-dsl-set-doc
	"identity" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "const" ct-lang-register-macro drop
	"const" "always return first argument" fn-dsl-set-doc
	"const" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "default" ct-lang-register-macro drop
	"default" "return fallback only when value is falsey" fn-dsl-set-doc
	"default" "control" fn-dsl-set-attrs
	fn-dsl-lang-name "maybe" ct-lang-register-macro drop
	"maybe" "branch on value presence while preserving happy-path value" fn-dsl-set-doc
	"maybe" "control" fn-dsl-set-attrs
	fn-dsl-lang-name "keep" ct-lang-register-macro drop
	"keep" "alias of tap" fn-dsl-set-doc
	"keep" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "pipe-if" ct-lang-register-macro drop
	"pipe-if" "conditionally apply function and keep original value otherwise" fn-dsl-set-doc
	"pipe-if" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "pipe-unless" ct-lang-register-macro drop
	"pipe-unless" "inverse conditional pipeline" fn-dsl-set-doc
	"pipe-unless" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "pipe-last2" ct-lang-register-macro drop
	"pipe-last2" "thread-last with one fixed argument" fn-dsl-set-doc
	"pipe-last2" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "pipe-last3" ct-lang-register-macro drop
	"pipe-last3" "thread-last with two fixed arguments" fn-dsl-set-doc
	"pipe-last3" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "pipe-last4" ct-lang-register-macro drop
	"pipe-last4" "thread-last with three fixed arguments" fn-dsl-set-doc
	"pipe-last4" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "pipe6" ct-lang-register-macro drop
	"pipe6" "thread-first with five extra arguments" fn-dsl-set-doc
	"pipe6" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "apply6" ct-lang-register-macro drop
	"apply6" "apply 6-arity function" fn-dsl-set-doc
	"apply6" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "compose6" ct-lang-register-macro drop
	"compose6" "compose six unary functions" fn-dsl-set-doc
	"compose6" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "juxt4" ct-lang-register-macro drop
	"juxt4" "apply four unary functions to the same input" fn-dsl-set-doc
	"juxt4" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "juxt5" ct-lang-register-macro drop
	"juxt5" "apply five unary functions to the same input" fn-dsl-set-doc
	"juxt5" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "juxt6" ct-lang-register-macro drop
	"juxt6" "apply six unary functions to the same input" fn-dsl-set-doc
	"juxt6" "functional" fn-dsl-set-attrs
	fn-dsl-lang-name "tap2" ct-lang-register-macro drop
	"tap2" "tap through two side-effect functions while preserving value" fn-dsl-set-doc
	"tap2" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "tap3" ct-lang-register-macro drop
	"tap3" "tap through three side-effect functions while preserving value" fn-dsl-set-doc
	"tap3" "pipeline" fn-dsl-set-attrs
	fn-dsl-lang-name "when-do" ct-lang-register-macro drop
	"when-do" "statement-oriented conditional block" fn-dsl-set-doc
	"when-do" "control" fn-dsl-set-attrs
	fn-dsl-lang-name "unless-do" ct-lang-register-macro drop
	"unless-do" "inverse statement-oriented conditional block" fn-dsl-set-doc
	"unless-do" "control" fn-dsl-set-attrs
end
compile-only

# Arithmetic simplification: fn_simplify followed by one of these shapes
# is replaced by its simplified value; anything else passes through.
macro fn_simplify
	match
		[$x:int, +, 0] => `$x` emit
		[0, +, $x:int] => `$x` emit
		[$x:int, -, 0] => `$x` emit
		[$x:int, \*, 1] => `$x` emit
		[1, \*, $x:int] => `$x` emit
		[$x:int, /, 1] => `$x` emit
		[$x:int, \*, 0] => `0` emit
		[0, \*, $x:int] => `0` emit
		[_] =>
	end
;

word fn-dsl-install-pattern-macros
	fn-dsl-lang-name "fn_simplify" ct-lang-register-macro drop
end
compile-only

word fn-dsl-ct-capabilities
	map-new
	"ct_total" "ct-" ct-definition-list-names-prefix list-length map-set
	"ct_parser" "ct-parser-" ct-definition-list-names-prefix list-length map-set
	"ct_rewrite" "ct-rewrite-" ct-definition-list-names-prefix list-length map-set
	"ct_reader" "ct-rewrite-add-reader" ct-definition-list-names-prefix list-length map-set
	"ct_grammar" "ct-rewrite-add-grammar" ct-definition-list-names-prefix list-length map-set
	"ct_macro" "ct-macro-" ct-definition-list-names-prefix list-length map-set
	"ct_rng" "ct-rng-" ct-definition-list-names-prefix list-length map-set
	"ct_word_introspection" "ct-definition-" ct-definition-list-names-prefix list-length map-set
end
compile-only

word fn-dsl-assert-ct-surface
	fn-dsl-ct-capabilities
	dup "ct_total" map-get drop 120 >= static-assert
	dup "ct_parser" map-get drop 8 >= static-assert
	dup "ct_rewrite" map-get drop 8 >= static-assert
	dup "ct_rng" map-get drop 3 >= static-assert
	drop
end
compile-only

# Remove the fn.dsl pack's own grammar rewrite registrations named `name`
# (through their handles, never by global name).
word fn-dsl-drop-owned-rule        # fn-dsl-drop-owned-rule [* | name] -> [*]
	>r
	fn-dsl-lang-name ct-lang-status drop "grammar_rewrites" map-get drop
	begin
		dup list-empty? if drop rdrop exit end
		list-pop-front drop
		dup "name" map-get drop r@ string-equal? if
			"handle" map-get drop ct-rewrite-remove drop
		else
			drop
		end
	again
end
compile-only

# Install (or reinstall) a pack-owned grammar rule on the fn.dsl pipeline.
word fn-dsl-upsert-grammar-rule    # fn-dsl-upsert-grammar-rule [*, name, pattern | replacement] -> [*]
	>r >r
	dup fn-dsl-drop-owned-rule
	fn-dsl-lang-name swap r> r>
	ct-lang-add-grammar-rewrite-named
	"fn.dsl" ct-rewrite-rule-set-pipeline drop
end
compile-only

word fn-dsl-upsert-grammar-alias   # fn-dsl-upsert-grammar-alias [*, name, alias | target] -> [*]
	>r >r
	list-new r> list-append
	list-new r> list-append
	fn-dsl-upsert-grammar-rule
end
compile-only

word fn-dsl-install-operators
	"fn.dsl.pipeop"
	list-new "$x" list-append "|>" list-append "$f" list-append
	list-new "$x" list-append "$f" list-append
	fn-dsl-upsert-grammar-rule

	"fn.dsl.rpipeop"
	list-new "$f" list-append "<|" list-append "$x" list-append
	list-new "$x" list-append "$f" list-append
	fn-dsl-upsert-grammar-rule

	"fn.dsl.andand"
	list-new "$a" list-append "&&" list-append "$b" list-append
	list-new "$a" list-append "if" list-append "$b" list-append "else" list-append "0" list-append "end" list-append
	fn-dsl-upsert-grammar-rule

	"fn.dsl.oror"
	list-new "$a" list-append "||" list-append "$b" list-append
	list-new "$a" list-append "if" list-append "1" list-append "else" list-append "$b" list-append "end" list-append
	fn-dsl-upsert-grammar-rule

	"fn.dsl.arrow"
	list-new "$x" list-append "->" list-append "$f" list-append
	list-new "$x" list-append "$f" list-append
	fn-dsl-upsert-grammar-rule

	"fn.dsl.larrow"
	list-new "$f" list-append "<-" list-append "$x" list-append
	list-new "$x" list-append "$f" list-append
	fn-dsl-upsert-grammar-rule
end
compile-only

word fn-dsl-active?
	"grammar" ct-rewrite-list-active-pipelines
	"fn.dsl" list-contains?
end
compile-only

word fn-dsl-mode-fast
	4096 ct-rewrite-set-max-steps
	0 ct-rewrite-set-loop-detection
end
compile-only

word fn-dsl-mode-safe
	100000 ct-rewrite-set-max-steps
	1 ct-rewrite-set-loop-detection
end
compile-only

word fn-dsl-install-rewrites
	"fn.dsl.fun" "fun" "fn" fn-dsl-upsert-grammar-alias
	"fn.dsl.def" "def" "fn" fn-dsl-upsert-grammar-alias
	"fn.dsl.fnc" "fnc" "fn" fn-dsl-upsert-grammar-alias
	"fn.dsl.func" "func" "fn" fn-dsl-upsert-grammar-alias
	"fn.dsl.fnx" "fnx" "fn" fn-dsl-upsert-grammar-alias
	"fn.dsl.method" "method" "fn" fn-dsl-upsert-grammar-alias
	"fn.dsl.defun" "defun" "fn" fn-dsl-upsert-grammar-alias

	"grammar" "fn.dsl" 1 ct-rewrite-set-pipeline-active
end
compile-only

word fn-dsl-enable
	fn-dsl-lang-ensure
	fn-dsl-register-macros
	fn-dsl-install-rewrites
	fn-dsl-install-operators
	fn-dsl-install-pattern-macros
end
immediate
compile-only

word fn-dsl-enable-calls
	extend-syntax
end
immediate
compile-only

word fn-dsl-disable
	"grammar" "fn.dsl" 0 ct-rewrite-set-pipeline-active
	fn-dsl-lang-name ct-lang-deactivate drop
end
immediate
compile-only

macro use-fn-dsl `fn-dsl-enable` emit ;

macro use-fn-calls `fn-dsl-enable-calls` emit ;

macro use-fn-superlang `fn-dsl-enable fn-dsl-enable-calls` emit ;

macro use-fn-full `use-fn-superlang` emit ;

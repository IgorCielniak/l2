#call-syntax-rewrite [* | fnameToken] -> [* | handled]
word call-syntax-rewrite
	dup token-lexeme identifier? 0 == if drop 0 exit end
	peek-token dup nil? if drop drop 0 exit end
	dup token-lexeme "(" string= 0 == if drop drop 0 exit end
	swap >r                 # stash fnameTok
	drop                    # discard peeked '('
	next-token drop         # consume '('
	list-new                # out
	list-new                # out cur
begin
	next-token dup nil? if "unterminated call expression" parse-error end
	dup token-lexeme ")" string= if
		drop
		# flush current arg
		list-extend           # out'
		r> list-append        # out''
		inject-tokens
		1 exit
		end
	dup token-lexeme "," string= if
		drop
		list-extend            # out'
		list-new               # out' cur
		continue
	end
	# default: append tok to cur
	list-append
again
end
immediate
compile-only

#extend-syntax [*] -> [*]
word extend-syntax
	"call-syntax-rewrite" set-token-hook
end
immediate
compile-only


word fn-op-prec
	dup "+" string= if drop 1 exit end
	dup "-" string= if drop 1 exit end
	dup "*" string= if drop 2 exit end
	dup "/" string= if drop 2 exit end
	dup "%" string= if drop 2 exit end
	drop 0
end
compile-only

word fn-operator?
	fn-op-prec 0 >
end
compile-only

word fn-check-dup
	>r                              # params          (r: name)
	0                               # params idx
begin
	over list-length swap >= if     # params flag
		r> exit
	end
	dup >r                          # params idx   (r: idx name)
	over swap list-get              # params elem
	1 rpick string= if "duplicate parameter names in fn definition" parse-error end
	drop                            # drop comparison flag when no error
	r> 1 +                          # params idx+1
again
end
compile-only

word fn-params
	list-new             # lexer params
	swap                 # params lexer
	>r                   # params            (r: lexer)
begin
	0 rpick lexer-pop token-lexeme   # params lex
	swap drop                        # params lex   (drop returned lexer)
	dup ")" string= if drop r> exit end
	dup identifier? 0 == if "invalid parameter type in fn definition" parse-error end
	drop                              # params
	0 rpick lexer-pop token-lexeme    # params lexer pname
	swap drop                         # params pname
	dup identifier? 0 == if "invalid parameter name in fn definition" parse-error end
	fn-check-dup                      # params pname
	list-append                       # params
	0 rpick lexer-pop token-lexeme    # params lexer sep
	swap drop                         # params sep
	dup "," string= if drop continue end
	dup ")" string= if drop r> exit end
	"expected ',' or ')' in parameter list" parse-error
again
end
compile-only

word fn-collect-body
	"{" lexer-expect drop          # consume opening brace, keep lexer
	lexer-collect-brace             # lexer bodyTokens
	swap drop                       # bodyTokens
end
compile-only

word fn-lexemes-from-tokens
	>r                   # (r: tokens)
	list-new             # acc
begin
	0 rpick list-empty? if
		rdrop exit
	end
	0 rpick list-pop-front     # acc tokens' first
	rdrop                      # acc tokens'
	swap                       # acc first tokens'
	>r                         # acc first   (r: tokens')
	token-lexeme          # acc lex
	list-append           # acc'
again
end
compile-only

word fn-body->postfix-program      # bodyLexemes -- postfix
	list-new swap list-new swap      # out cur body
begin
	dup list-empty? if
		drop                          # out cur
		dup list-length 0 > if
			shunt                     # out postfix
			list-extend              # out'
			exit
		end
		drop                          # out
		dup list-length 0 == if "empty function body" parse-error end
		list-pop                      # out' tok
		dup "drop" string= 0 == if "invalid trailing function statement" parse-error end
		drop                          # out'
		exit
	end

	list-pop-front                   # out cur body' tok
	swap >r                          # out cur tok      (r: body')

	dup "return" string= if
		drop
		r>
		continue
	end

	dup ";" string= if
		drop
		dup list-length 0 == if
			r>
			continue
		end
		shunt                          # out postfix
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


word fn-body->tokens                # bodyLexemes -- tokens
	fn-body->postfix-program
end
compile-only

word fn-emit-prologue             # params out -- params out
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

word fn-emit-epilogue             # params out -- out
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

word fn-translate-prologue-loop   # count --
	dup 0 > if
		1 -
		0 rpick ">r" list-append drop
		fn-translate-prologue-loop
		end
	drop
end
compile-only

word fn-translate-epilogue-loop   # count --
	dup 0 > if
		1 -
		0 rpick "rdrop" list-append drop
		fn-translate-epilogue-loop
		end
	drop
end
compile-only

word fn-param-index                # params name -- params idx flag
	>r                             # params        (r: name)
	0                              # params idx

begin
	over list-length over swap >= if    # params idx flag (idx >= len?)
		drop                      # params
		r> drop                   # drop name
		-1 0 exit                 # params -1 0
	end                        # params idx
	over over list-get            # params idx elem
	0 rpick string=               # params idx flag
	if
		r> drop                   # drop name
		1 exit                    # params idx 1
	end
	drop                          # params idx
	1 +                           # params idx+1
again
end
compile-only


word fn-build-param-map            # params -- params map
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


word fn-translate-token            # out map tok -- out map
	# number?
	dup string>number              # out map tok num ok
	if
		# (out map tok num) -> (out' map)
		>r                           # out map tok        (r: num)
		drop                         # out map
		r>                           # out map num
		swap >r                      # out num            (r: map)
		list-append                  # out'
		r>                           # out' map
		exit
	end
	drop                           # out map tok

	# param?
	dup >r                         # out map tok        (r: tok)
	map-get                        # out map idx|nil ok
	if
		# append idx
		swap >r                      # out idx            (r: map tok)
		list-append                  # out'
		r>                           # out' map
		# append "rpick"
		"rpick" swap >r             # out' "rpick"      (r: map tok)
		list-append                  # out''
		r>                           # out'' map
		# drop saved tok
		r> drop                      # out'' map
		exit
	end
	# not a param: drop idx|nil, append original tok
	drop                           # out map
	r>                             # out map tok
	swap >r                        # out tok            (r: map)
	list-append                    # out'
	r>                             # out' map
end
compile-only


word fn-translate-postfix-loop     # map out postfix -- map out
	begin
		dup list-empty? if
			drop
			exit
		end
		list-pop-front               # map out postfix' tok
		swap >r                      # map out tok   (r: postfix')
		>r swap r>                   # out map tok   (r: postfix')
		fn-translate-token           # out map
		swap                         # map out
		r>                           # map out postfix'
		continue
	again
end
compile-only


word fn-translate-postfix          # postfix params -- out
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

word fn-build-body
	fn-translate-postfix   # words
end
compile-only

word fn
	"(),{};+-*/%," lexer-new         # lexer
	dup lexer-pop                     # lexer nameTok
	dup >r                            # save nameTok
	token-lexeme                      # lexer name
	dup identifier? 0 == if "invalid function name for 'fn'" parse-error end
	>r                                # save name string
	drop                              # leave lexer only for params
	"(" lexer-expect drop            # consume '(' keep lexer
	fn-params                         # params lexer
	fn-collect-body                   # params bodyTokens
	swap >r                           # bodyTokens (r: params)
	fn-lexemes-from-tokens            # lexemes
	fn-body->tokens                   # tokens
	r>                                # postfix params
	fn-build-body                     # body
	r> drop                           # drop name string
	r>                                # name token
	swap emit-definition
end
immediate
compile-only


# High-level DSL convenience aliases.
macro defn 0
	fn
;

macro function 0
	fn
;

word fn-dsl-set-doc                # name doc --
	ct-macro-doc-set drop
end
compile-only

word fn-dsl-set-attrs              # name kind --
	map-new
	"category" "fn.dsl" map-set
	swap
	"kind" swap map-set
	"source" "ct-register-text-macro-signature" map-set
	ct-macro-attrs-set drop
end
compile-only

word fn-dsl-parser-session-begin
	ct-parser-session-begin
end
compile-only

word fn-dsl-parser-session-commit
	ct-parser-session-commit
end
compile-only

word fn-dsl-parser-session-rollback
	ct-parser-session-rollback
end
compile-only

word fn-dsl-parser-collect-until   # delimiter -- tokens found
	ct-parser-collect-until
end
compile-only

word fn-dsl-parser-collect-balanced   # open close -- tokens found
	ct-parser-collect-balanced
end
compile-only

word fn-dsl-parser-mark
	ct-parser-mark
end
compile-only

word fn-dsl-parser-diff
	ct-parser-diff
end
compile-only

word fn-dsl-parser-expected
	ct-parser-expected
end
compile-only

word fn-dsl-token-clone
	token-clone
end
compile-only

word fn-dsl-token-rename          # token lexeme -- token
	token-with-lexeme
end
compile-only

word fn-dsl-token-shift-column    # token delta -- token
	token-shift-column
end
compile-only

word fn-dsl-rewrite-scope-push
	ct-rewrite-scope-push
end
compile-only

word fn-dsl-rewrite-scope-pop
	ct-rewrite-scope-pop
end
compile-only

word fn-dsl-rewrite-run           # stage token-list -- token-list patches
	ct-rewrite-run-on-list
end
compile-only

word fn-dsl-rewrite-run-scoped    # stage token-list -- token-list patches
	fn-dsl-rewrite-scope-push drop
	fn-dsl-rewrite-run
	fn-dsl-rewrite-scope-pop static_assert
end
compile-only

word fn-dsl-parser-tail-lexemes   # -- list
	ct-parser-tail
	list-new swap
begin
	dup list-empty? if
		drop
		exit
	end
	list-pop-front
	swap >r
	token-lexeme
	list-append
	r>
	continue
again
end
compile-only

word fn-dsl-parser-diff-lexemes   # start end -- list
	fn-dsl-parser-diff
	"lexemes" map-get
end
compile-only

word fn-dsl-register-macros
	"pipe"
	list-new "value" list-append "func" list-append
	list-new "$value" list-append "$func" list-append
	ct-register-text-macro-signature
	"pipe" "thread-first: pipe(value, func) => value func" fn-dsl-set-doc
	"pipe" "pipeline" fn-dsl-set-attrs

	"thread"
	list-new "value" list-append "func" list-append
	list-new "$value" list-append "$func" list-append
	ct-register-text-macro-signature
	"thread" "alias of pipe" fn-dsl-set-doc
	"thread" "pipeline" fn-dsl-set-attrs

	"pipe2"
	list-new "value" list-append "arg" list-append "func" list-append
	list-new "$value" list-append "$arg" list-append "$func" list-append
	ct-register-text-macro-signature
	"pipe2" "thread-first with one extra argument" fn-dsl-set-doc
	"pipe2" "pipeline" fn-dsl-set-attrs

	"pipe3"
	list-new "value" list-append "arg1" list-append "arg2" list-append "func" list-append
	list-new "$value" list-append "$arg1" list-append "$arg2" list-append "$func" list-append
	ct-register-text-macro-signature
	"pipe3" "thread-first with two extra arguments" fn-dsl-set-doc
	"pipe3" "pipeline" fn-dsl-set-attrs

	"tap"
	list-new "value" list-append "func" list-append
	list-new "$value" list-append "dup" list-append "$func" list-append
	ct-register-text-macro-signature
	"tap" "invoke side-effect function and keep value" fn-dsl-set-doc
	"tap" "pipeline" fn-dsl-set-attrs

	"when"
	list-new "cond" list-append "expr" list-append
	list-new "$cond" list-append "if" list-append "$expr" list-append "else" list-append "0" list-append "end" list-append
	ct-register-text-macro-signature
	"when" "expression-level conditional" fn-dsl-set-doc
	"when" "control" fn-dsl-set-attrs

	"unless"
	list-new "cond" list-append "expr" list-append
	list-new "$cond" list-append "if" list-append "0" list-append "else" list-append "$expr" list-append "end" list-append
	ct-register-text-macro-signature
	"unless" "inverse expression-level conditional" fn-dsl-set-doc
	"unless" "control" fn-dsl-set-attrs

	"ifelse"
	list-new "cond" list-append "on_true" list-append "on_false" list-append
	list-new "$cond" list-append "if" list-append "$on_true" list-append "else" list-append "$on_false" list-append "end" list-append
	ct-register-text-macro-signature
	"ifelse" "expression-level if/else" fn-dsl-set-doc
	"ifelse" "control" fn-dsl-set-attrs

	"compose2"
	list-new "f" list-append "g" list-append "x" list-append
	list-new "$x" list-append "$g" list-append "$f" list-append
	ct-register-text-macro-signature
	"compose2" "compose two unary functions" fn-dsl-set-doc
	"compose2" "functional" fn-dsl-set-attrs

	"compose3"
	list-new "f" list-append "g" list-append "h" list-append "x" list-append
	list-new "$x" list-append "$h" list-append "$g" list-append "$f" list-append
	ct-register-text-macro-signature
	"compose3" "compose three unary functions" fn-dsl-set-doc
	"compose3" "functional" fn-dsl-set-attrs

	"chain"
	list-new "value" list-append "*funcs" list-append
	list-new "$value" list-append "$*funcs" list-append
	ct-register-text-macro-signature
	"chain" "variadic thread-first pipeline" fn-dsl-set-doc
	"chain" "pipeline" fn-dsl-set-attrs

	"invoke"
	list-new "func" list-append "*args" list-append
	list-new "$*args" list-append "$func" list-append
	ct-register-text-macro-signature
	"invoke" "apply function to variadic args" fn-dsl-set-doc
	"invoke" "functional" fn-dsl-set-attrs

	"tapn"
	list-new "value" list-append "func" list-append "*args" list-append
	list-new "$value" list-append "dup" list-append "$*args" list-append "$func" list-append "drop" list-append
	ct-register-text-macro-signature
	"tapn" "tap with variadic function arguments" fn-dsl-set-doc
	"tapn" "pipeline" fn-dsl-set-attrs

	"apply2"
	list-new "func" list-append "a" list-append "b" list-append
	list-new "$a" list-append "$b" list-append "$func" list-append
	ct-register-text-macro-signature
	"apply2" "apply binary function" fn-dsl-set-doc
	"apply2" "functional" fn-dsl-set-attrs

	"apply3"
	list-new "func" list-append "a" list-append "b" list-append "c" list-append
	list-new "$a" list-append "$b" list-append "$c" list-append "$func" list-append
	ct-register-text-macro-signature
	"apply3" "apply ternary function" fn-dsl-set-doc
	"apply3" "functional" fn-dsl-set-attrs

	"pipe4"
	list-new "value" list-append "arg1" list-append "arg2" list-append "arg3" list-append "func" list-append
	list-new "$value" list-append "$arg1" list-append "$arg2" list-append "$arg3" list-append "$func" list-append
	ct-register-text-macro-signature
	"pipe4" "thread-first with three extra arguments" fn-dsl-set-doc
	"pipe4" "pipeline" fn-dsl-set-attrs

	"pipe5"
	list-new "value" list-append "arg1" list-append "arg2" list-append "arg3" list-append "arg4" list-append "func" list-append
	list-new "$value" list-append "$arg1" list-append "$arg2" list-append "$arg3" list-append "$arg4" list-append "$func" list-append
	ct-register-text-macro-signature
	"pipe5" "thread-first with four extra arguments" fn-dsl-set-doc
	"pipe5" "pipeline" fn-dsl-set-attrs

	"apply4"
	list-new "func" list-append "a" list-append "b" list-append "c" list-append "d" list-append
	list-new "$a" list-append "$b" list-append "$c" list-append "$d" list-append "$func" list-append
	ct-register-text-macro-signature
	"apply4" "apply quaternary function" fn-dsl-set-doc
	"apply4" "functional" fn-dsl-set-attrs

	"apply5"
	list-new "func" list-append "a" list-append "b" list-append "c" list-append "d" list-append "e" list-append
	list-new "$a" list-append "$b" list-append "$c" list-append "$d" list-append "$e" list-append "$func" list-append
	ct-register-text-macro-signature
	"apply5" "apply 5-arity function" fn-dsl-set-doc
	"apply5" "functional" fn-dsl-set-attrs

	"compose4"
	list-new "f" list-append "g" list-append "h" list-append "k" list-append "x" list-append
	list-new "$x" list-append "$k" list-append "$h" list-append "$g" list-append "$f" list-append
	ct-register-text-macro-signature
	"compose4" "compose four unary functions" fn-dsl-set-doc
	"compose4" "functional" fn-dsl-set-attrs

	"compose5"
	list-new "f" list-append "g" list-append "h" list-append "k" list-append "m" list-append "x" list-append
	list-new "$x" list-append "$m" list-append "$k" list-append "$h" list-append "$g" list-append "$f" list-append
	ct-register-text-macro-signature
	"compose5" "compose five unary functions" fn-dsl-set-doc
	"compose5" "functional" fn-dsl-set-attrs

	"juxt2"
	list-new "f" list-append "g" list-append "x" list-append
	list-new "$x" list-append "$f" list-append "$x" list-append "$g" list-append
	ct-register-text-macro-signature
	"juxt2" "apply two unary functions to same input" fn-dsl-set-doc
	"juxt2" "functional" fn-dsl-set-attrs

	"juxt3"
	list-new "f" list-append "g" list-append "h" list-append "x" list-append
	list-new "$x" list-append "$f" list-append "$x" list-append "$g" list-append "$x" list-append "$h" list-append
	ct-register-text-macro-signature
	"juxt3" "apply three unary functions to same input" fn-dsl-set-doc
	"juxt3" "functional" fn-dsl-set-attrs

	"flip2"
	list-new "func" list-append "a" list-append "b" list-append
	list-new "$b" list-append "$a" list-append "$func" list-append
	ct-register-text-macro-signature
	"flip2" "reverse first two arguments before call" fn-dsl-set-doc
	"flip2" "functional" fn-dsl-set-attrs

	"guard"
	list-new "cond" list-append "on_true" list-append "on_false" list-append
	list-new "$cond" list-append "if" list-append "$on_true" list-append "else" list-append "$on_false" list-append "end" list-append
	ct-register-text-macro-signature
	"guard" "expression guard with explicit fallback" fn-dsl-set-doc
	"guard" "control" fn-dsl-set-attrs

	"when-not"
	list-new "cond" list-append "expr" list-append
	list-new "$cond" list-append "if" list-append "0" list-append "else" list-append "$expr" list-append "end" list-append
	ct-register-text-macro-signature
	"when-not" "evaluate expression when condition is false" fn-dsl-set-doc
	"when-not" "control" fn-dsl-set-attrs

	"thrush"
	list-new "value" list-append "func" list-append
	list-new "$value" list-append "$func" list-append
	ct-register-text-macro-signature
	"thrush" "alias of pipe/thread-first" fn-dsl-set-doc
	"thrush" "pipeline" fn-dsl-set-attrs

	"identity"
	list-new "value" list-append
	list-new "$value" list-append
	ct-register-text-macro-signature
	"identity" "return value unchanged" fn-dsl-set-doc
	"identity" "functional" fn-dsl-set-attrs

	"const"
	list-new "value" list-append "_ignored" list-append
	list-new "$value" list-append
	ct-register-text-macro-signature
	"const" "always return first argument" fn-dsl-set-doc
	"const" "functional" fn-dsl-set-attrs

	"default"
	list-new "value" list-append "fallback" list-append
	list-new "$value" list-append "dup" list-append "if" list-append "else" list-append "drop" list-append "$fallback" list-append "end" list-append
	ct-register-text-macro-signature
	"default" "return fallback only when value is falsey" fn-dsl-set-doc
	"default" "control" fn-dsl-set-attrs

	"maybe"
	list-new "value" list-append "on_some" list-append "on_none" list-append
	list-new "$value" list-append "dup" list-append "if" list-append "$on_some" list-append "else" list-append "drop" list-append "$on_none" list-append "end" list-append
	ct-register-text-macro-signature
	"maybe" "branch on value presence while preserving happy-path value" fn-dsl-set-doc
	"maybe" "control" fn-dsl-set-attrs

	"keep"
	list-new "value" list-append "func" list-append
	list-new "$value" list-append "dup" list-append "$func" list-append
	ct-register-text-macro-signature
	"keep" "alias of tap" fn-dsl-set-doc
	"keep" "pipeline" fn-dsl-set-attrs

	"pipe-if"
	list-new "value" list-append "cond" list-append "func" list-append
	list-new "$cond" list-append "if" list-append "$value" list-append "$func" list-append "else" list-append "$value" list-append "end" list-append
	ct-register-text-macro-signature
	"pipe-if" "conditionally apply function and keep original value otherwise" fn-dsl-set-doc
	"pipe-if" "pipeline" fn-dsl-set-attrs

	"pipe-unless"
	list-new "value" list-append "cond" list-append "func" list-append
	list-new "$cond" list-append "if" list-append "$value" list-append "else" list-append "$value" list-append "$func" list-append "end" list-append
	ct-register-text-macro-signature
	"pipe-unless" "inverse conditional pipeline" fn-dsl-set-doc
	"pipe-unless" "pipeline" fn-dsl-set-attrs

	"pipe-last2"
	list-new "value" list-append "arg" list-append "func" list-append
	list-new "$arg" list-append "$value" list-append "$func" list-append
	ct-register-text-macro-signature
	"pipe-last2" "thread-last with one fixed argument" fn-dsl-set-doc
	"pipe-last2" "pipeline" fn-dsl-set-attrs

	"pipe-last3"
	list-new "value" list-append "arg1" list-append "arg2" list-append "func" list-append
	list-new "$arg1" list-append "$arg2" list-append "$value" list-append "$func" list-append
	ct-register-text-macro-signature
	"pipe-last3" "thread-last with two fixed arguments" fn-dsl-set-doc
	"pipe-last3" "pipeline" fn-dsl-set-attrs

	"pipe-last4"
	list-new "value" list-append "arg1" list-append "arg2" list-append "arg3" list-append "func" list-append
	list-new "$arg1" list-append "$arg2" list-append "$arg3" list-append "$value" list-append "$func" list-append
	ct-register-text-macro-signature
	"pipe-last4" "thread-last with three fixed arguments" fn-dsl-set-doc
	"pipe-last4" "pipeline" fn-dsl-set-attrs

	"pipe6"
	list-new "value" list-append "arg1" list-append "arg2" list-append "arg3" list-append "arg4" list-append "arg5" list-append "func" list-append
	list-new "$value" list-append "$arg1" list-append "$arg2" list-append "$arg3" list-append "$arg4" list-append "$arg5" list-append "$func" list-append
	ct-register-text-macro-signature
	"pipe6" "thread-first with five extra arguments" fn-dsl-set-doc
	"pipe6" "pipeline" fn-dsl-set-attrs

	"apply6"
	list-new "func" list-append "a" list-append "b" list-append "c" list-append "d" list-append "e" list-append "f" list-append
	list-new "$a" list-append "$b" list-append "$c" list-append "$d" list-append "$e" list-append "$f" list-append "$func" list-append
	ct-register-text-macro-signature
	"apply6" "apply 6-arity function" fn-dsl-set-doc
	"apply6" "functional" fn-dsl-set-attrs

	"compose6"
	list-new "f" list-append "g" list-append "h" list-append "k" list-append "m" list-append "n" list-append "x" list-append
	list-new "$x" list-append "$n" list-append "$m" list-append "$k" list-append "$h" list-append "$g" list-append "$f" list-append
	ct-register-text-macro-signature
	"compose6" "compose six unary functions" fn-dsl-set-doc
	"compose6" "functional" fn-dsl-set-attrs

	"juxt4"
	list-new "f" list-append "g" list-append "h" list-append "k" list-append "x" list-append
	list-new "$x" list-append "$f" list-append "$x" list-append "$g" list-append "$x" list-append "$h" list-append "$x" list-append "$k" list-append
	ct-register-text-macro-signature
	"juxt4" "apply four unary functions to the same input" fn-dsl-set-doc
	"juxt4" "functional" fn-dsl-set-attrs

	"juxt5"
	list-new "f" list-append "g" list-append "h" list-append "k" list-append "m" list-append "x" list-append
	list-new "$x" list-append "$f" list-append "$x" list-append "$g" list-append "$x" list-append "$h" list-append "$x" list-append "$k" list-append "$x" list-append "$m" list-append
	ct-register-text-macro-signature
	"juxt5" "apply five unary functions to the same input" fn-dsl-set-doc
	"juxt5" "functional" fn-dsl-set-attrs

	"juxt6"
	list-new "f" list-append "g" list-append "h" list-append "k" list-append "m" list-append "n" list-append "x" list-append
	list-new "$x" list-append "$f" list-append "$x" list-append "$g" list-append "$x" list-append "$h" list-append "$x" list-append "$k" list-append "$x" list-append "$m" list-append "$x" list-append "$n" list-append
	ct-register-text-macro-signature
	"juxt6" "apply six unary functions to the same input" fn-dsl-set-doc
	"juxt6" "functional" fn-dsl-set-attrs

	"tap2"
	list-new "value" list-append "f" list-append "g" list-append
	list-new "$value" list-append "dup" list-append "$f" list-append "dup" list-append "$g" list-append
	ct-register-text-macro-signature
	"tap2" "tap through two side-effect functions while preserving value" fn-dsl-set-doc
	"tap2" "pipeline" fn-dsl-set-attrs

	"tap3"
	list-new "value" list-append "f" list-append "g" list-append "h" list-append
	list-new "$value" list-append "dup" list-append "$f" list-append "dup" list-append "$g" list-append "dup" list-append "$h" list-append
	ct-register-text-macro-signature
	"tap3" "tap through three side-effect functions while preserving value" fn-dsl-set-doc
	"tap3" "pipeline" fn-dsl-set-attrs

	"when-do"
	list-new "cond" list-append "*body" list-append
	list-new "$cond" list-append "if" list-append "$*body" list-append "end" list-append
	ct-register-text-macro-signature
	"when-do" "statement-oriented conditional block" fn-dsl-set-doc
	"when-do" "control" fn-dsl-set-attrs

	"unless-do"
	list-new "cond" list-append "*body" list-append
	list-new "$cond" list-append "if" list-append "else" list-append "$*body" list-append "end" list-append
	ct-register-text-macro-signature
	"unless-do" "inverse statement-oriented conditional block" fn-dsl-set-doc
	"unless-do" "control" fn-dsl-set-attrs
end
compile-only

word fn-dsl-pattern-append-clause   # clauses pattern replacement -- clauses'
	>r
	list-new
	swap
	list-append
	r>
	list-append
	list-append
end
compile-only

word fn-dsl-install-pattern-macros
	"fn_simplify"
	list-new

	list-new "$x:int" list-append "+" list-append "0" list-append
	list-new "$x" list-append
	fn-dsl-pattern-append-clause

	list-new "0" list-append "+" list-append "$x:int" list-append
	list-new "$x" list-append
	fn-dsl-pattern-append-clause

	list-new "$x:int" list-append "-" list-append "0" list-append
	list-new "$x" list-append
	fn-dsl-pattern-append-clause

	list-new "$x:int" list-append "*" list-append "1" list-append
	list-new "$x" list-append
	fn-dsl-pattern-append-clause

	list-new "1" list-append "*" list-append "$x:int" list-append
	list-new "$x" list-append
	fn-dsl-pattern-append-clause

	list-new "$x:int" list-append "/" list-append "1" list-append
	list-new "$x" list-append
	fn-dsl-pattern-append-clause

	list-new "$x:int" list-append "*" list-append "0" list-append
	list-new "0" list-append
	fn-dsl-pattern-append-clause

	list-new "0" list-append "*" list-append "$x:int" list-append
	list-new "0" list-append
	fn-dsl-pattern-append-clause

	ct-register-pattern-macro

	"fn_simplify" "fn.dsl.optim" ct-set-pattern-macro-group static_assert
	"fn_simplify" "fn.dsl.scope" ct-set-pattern-macro-scope static_assert
	"fn_simplify" 20 ct-set-pattern-macro-priority static_assert
	"fn.dsl.optim" 1 ct-set-pattern-group-active static_assert
	"fn.dsl.scope" 1 ct-set-pattern-scope-active static_assert
end
compile-only

word fn-dsl-pattern-enable
	"fn_simplify" 1 ct-set-pattern-macro-enabled
end
compile-only

word fn-dsl-pattern-disable
	"fn_simplify" 0 ct-set-pattern-macro-enabled
end
compile-only

word fn-dsl-pattern-enabled
	"fn_simplify" ct-get-pattern-macro-enabled
end
compile-only

word fn-dsl-pattern-clauses
	"fn_simplify" ct-get-pattern-macro-clauses
end
compile-only

word fn-dsl-pattern-conflicts
	"fn_simplify" ct-detect-pattern-conflicts-named
end
compile-only

word fn-dsl-ct-call-policy-open
	"off" ct-set-ct-call-sandbox-mode
	"raise" ct-set-ct-call-exception-policy
	1 ct-set-ct-call-memo
	0 ct-set-ct-call-side-effects
	64 ct-set-ct-call-recursion-limit
	0 ct-set-ct-call-timeout-ms
end
compile-only

word fn-dsl-ct-call-policy-safe
	"compile-only" ct-set-ct-call-sandbox-mode
	list-new
	"ct-capture-args" list-append
	"ct-capture-get" list-append
	"ct-capture-has?" list-append
	"ct-capture-shape" list-append
	"ct-capture-coerce-tokens" list-append
	"ct-capture-coerce-string" list-append
	"ct-capture-coerce-number" list-append
	"ct-capture-normalize" list-append
	"ct-capture-pretty" list-append
	"ct-capture-clone" list-append
	ct-set-ct-call-sandbox-allowlist drop
	"raise" ct-set-ct-call-exception-policy
	1 ct-set-ct-call-memo
	1 ct-set-ct-call-side-effects
	64 ct-set-ct-call-recursion-limit
	250 ct-set-ct-call-timeout-ms
	1337 ct-ctrand-seed
end
compile-only

word fn-dsl-ct-call-status
	map-new
	"sandbox_mode" ct-get-ct-call-sandbox-mode map-set
	"exception_policy" ct-get-ct-call-exception-policy map-set
	"memo_enabled" ct-get-ct-call-memo map-set
	"memo_size" ct-get-ct-call-memo-size map-set
	"side_effects_enabled" ct-get-ct-call-side-effects map-set
	"side_effect_log" ct-get-ct-call-side-effect-log map-set
	"recursion_limit" ct-get-ct-call-recursion-limit map-set
	"timeout_ms" ct-get-ct-call-timeout-ms map-set
	"sandbox_allowlist" ct-get-ct-call-sandbox-allowlist map-set
	"sandbox_allowlist_size" ct-get-ct-call-sandbox-allowlist list-length map-set
end
compile-only

word fn-dsl-ct-call-reset
	ct-clear-ct-call-memo drop
	ct-clear-ct-call-side-effect-log drop
end
compile-only

word fn-dsl-words-prefix
	ct-list-words-prefix
end
compile-only

word fn-dsl-ct-capabilities
	map-new
	"ct_total" "ct-" fn-dsl-words-prefix list-length map-set
	"ct_capture" "ct-capture-" fn-dsl-words-prefix list-length map-set
	"ct_parser" "ct-parser-" fn-dsl-words-prefix list-length map-set
	"ct_rewrite" "ct-rewrite-" fn-dsl-words-prefix list-length map-set
	"ct_reader" "ct-add-reader-" fn-dsl-words-prefix list-length map-set
	"ct_grammar" "ct-add-grammar-" fn-dsl-words-prefix list-length map-set
	"ct_macro" "ct-macro-" fn-dsl-words-prefix list-length map-set
	"ct_pattern_set" "ct-set-pattern-" fn-dsl-words-prefix list-length map-set
	"ct_pattern_get" "ct-get-pattern-" fn-dsl-words-prefix list-length map-set
	"ct_call_get" "ct-get-ct-call-" fn-dsl-words-prefix list-length map-set
	"ct_call_set" "ct-set-ct-call-" fn-dsl-words-prefix list-length map-set
	"ct_word_introspection" "ct-get-word-" fn-dsl-words-prefix list-length map-set
end
compile-only

word fn-dsl-assert-ct-surface
	fn-dsl-ct-capabilities
	dup "ct_total" map-get 120 >= static_assert
	dup "ct_capture" map-get 20 >= static_assert
	dup "ct_parser" map-get 8 >= static_assert
	dup "ct_rewrite" map-get 8 >= static_assert
	dup "ct_call_get" map-get 5 >= static_assert
	dup "ct_call_set" map-get 5 >= static_assert
	drop
end
compile-only

word fn-dsl-upsert-grammar-alias   # name alias target --
	>r >r
	dup ct-remove-grammar-rewrite drop
	list-new r> list-append
	list-new r> list-append
	ct-add-grammar-rewrite-named drop
end
compile-only

word fn-dsl-bind-rewrite-pipeline  # name --
	"grammar" swap "fn.dsl" ct-set-rewrite-pipeline drop
end
compile-only

word fn-dsl-upsert-grammar-rule    # name pattern replacement --
	>r >r
	dup ct-remove-grammar-rewrite drop
	r> r>
	ct-add-grammar-rewrite-named drop
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

	"fn.dsl.pipeop" fn-dsl-bind-rewrite-pipeline
	"fn.dsl.rpipeop" fn-dsl-bind-rewrite-pipeline
	"fn.dsl.andand" fn-dsl-bind-rewrite-pipeline
	"fn.dsl.oror" fn-dsl-bind-rewrite-pipeline
	"fn.dsl.arrow" fn-dsl-bind-rewrite-pipeline
	"fn.dsl.larrow" fn-dsl-bind-rewrite-pipeline
end
compile-only

word fn-dsl-sync-rewrite-index
	"grammar" ct-rebuild-rewrite-index drop
end
compile-only

word fn-dsl-active?
	"grammar" ct-list-rewrite-active-pipelines
	"fn.dsl" list-contains?
end
compile-only

word fn-dsl-stats
	"grammar" ct-get-rewrite-index-stats
end
compile-only

word fn-dsl-compat
	"grammar" ct-rewrite-compatibility-matrix
end
compile-only

word fn-dsl-trace-on
	1 ct-set-rewrite-trace
end
compile-only

word fn-dsl-trace-off
	0 ct-set-rewrite-trace
end
compile-only

word fn-dsl-trace-log
	ct-get-rewrite-trace-log
end
compile-only

word fn-dsl-trace-clear
	ct-clear-rewrite-trace-log
end
compile-only

word fn-dsl-mode-fast
	"first" ct-set-rewrite-saturation
	4096 ct-set-rewrite-max-steps
	0 ct-set-rewrite-loop-detection
end
compile-only

word fn-dsl-mode-safe
	"specificity" ct-set-rewrite-saturation
	100000 ct-set-rewrite-max-steps
	1 ct-set-rewrite-loop-detection
end
compile-only

word fn-dsl-pack-export
	ct-export-rewrite-pack
end
compile-only

word fn-dsl-pack-import
	ct-import-rewrite-pack
end
compile-only

word fn-dsl-pack-replace
	ct-import-rewrite-pack-replace
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

	"fn.dsl.fun" fn-dsl-bind-rewrite-pipeline
	"fn.dsl.def" fn-dsl-bind-rewrite-pipeline
	"fn.dsl.fnc" fn-dsl-bind-rewrite-pipeline
	"fn.dsl.func" fn-dsl-bind-rewrite-pipeline
	"fn.dsl.fnx" fn-dsl-bind-rewrite-pipeline
	"fn.dsl.method" fn-dsl-bind-rewrite-pipeline
	"fn.dsl.defun" fn-dsl-bind-rewrite-pipeline

	"grammar" "fn.dsl" 1 ct-set-rewrite-pipeline-active
	fn-dsl-sync-rewrite-index
end
compile-only

word fn-dsl-enable
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
	"grammar" "fn.dsl" 0 ct-set-rewrite-pipeline-active
end
immediate
compile-only

macro use-fn-dsl 0
	fn-dsl-enable
;

macro use-fn-calls 0
	fn-dsl-enable-calls
;

macro use-fn-superlang 0
	fn-dsl-enable
	fn-dsl-enable-calls
	fn-dsl-ct-call-policy-safe
;

macro use-fn-full 0
	use-fn-superlang
;

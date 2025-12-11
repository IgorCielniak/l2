: call-syntax-rewrite            # ( fnameToken -- handled )
	dup token-lexeme identifier? 0 == if drop 0 exit then
	peek-token dup nil? if drop drop 0 exit then
	dup token-lexeme "(" string= 0 == if drop drop 0 exit then
	swap >r                 # stash fnameTok
	drop                    # discard peeked '('
	next-token drop         # consume '('
	list-new                # out
	list-new                # out cur
begin
	next-token dup nil? if "unterminated call expression" parse-error then
	dup token-lexeme ")" string= if
		drop
		# flush current arg
		list-extend           # out'
		r> list-append        # out''
		inject-tokens
		1 exit
		then
	dup token-lexeme "," string= if
		drop
		list-extend            # out'
		list-new               # out' cur
		continue
	then
	# default: append tok to cur
	list-append
again
;
immediate
compile-only

: extend-syntax
	"call-syntax-rewrite" set-token-hook
;
immediate
compile-only


: fn-op-prec
	dup "+" string= if drop 1 exit then
	dup "-" string= if drop 1 exit then
	dup "*" string= if drop 2 exit then
	dup "/" string= if drop 2 exit then
	dup "%" string= if drop 2 exit then
	drop 0
;
compile-only

: fn-operator?
	fn-op-prec 0 >
;
compile-only

: fn-check-dup
	>r                              # params          (r: name)
	0                               # params idx
begin
	over list-length swap >= if     # params flag
		r> exit
	then
	dup >r                          # params idx   (r: idx name)
	over swap list-get              # params elem
	1 rpick string= if "duplicate parameter names in fn definition" parse-error then
	drop                            # drop comparison flag when no error
	r> 1 +                          # params idx+1
again
;
compile-only

: fn-params
	list-new             # lexer params
	swap                 # params lexer
	>r                   # params            (r: lexer)
begin
	0 rpick lexer-pop token-lexeme   # params lex
	swap drop                        # params lex   (drop returned lexer)
	dup ")" string= if drop r> exit then
	dup "int" string= 0 == if "only 'int' parameters are supported in fn definitions" parse-error then
	drop                              # params
	0 rpick lexer-pop token-lexeme    # params lexer pname
	swap drop                         # params pname
	dup identifier? 0 == if "invalid parameter name in fn definition" parse-error then
	fn-check-dup                      # params pname
	list-append                       # params
	0 rpick lexer-pop token-lexeme    # params lexer sep
	swap drop                         # params sep
	dup "," string= if drop continue then
	dup ")" string= if drop r> exit then
	"expected ',' or ')' in parameter list" parse-error
again
;
compile-only

: fn-collect-body
	"{" lexer-expect drop          # consume opening brace, keep lexer
	lexer-collect-brace             # lexer bodyTokens
	swap drop                       # bodyTokens
;
compile-only

: fn-lexemes-from-tokens
	list-new >r                 # tokens    (r: acc)
	0                           # tokens idx
begin
	over list-length over swap >= if   # stop when idx >= len
		drop drop                # drop idx and tokens (flag consumed by if)
		r> exit                  # return acc
	then
	over over list-get token-lexeme   # tokens idx lex
	r> swap list-append >r            # tokens idx
	1 +                               # tokens idx+1
again
;
compile-only

: fn-validate-body
	dup list-length 0 == if "empty function body" parse-error then
	dup >r 0 r> swap list-get "return" string= 0 == if "function body must start with 'return'" parse-error then
	dup list-last ";" string= 0 == if "function body must terminate with ';'" parse-error then
	list-clone                     # work on a copy
	list-pop drop                  # drop trailing ';'
	list-pop-front drop            # drop leading 'return'
	dup list-length 0 == if "missing return expression" parse-error then
;
compile-only

: fn-build-body
	fn-translate-postfix   # words
;
compile-only

: fn
	"(),{};+-*/%," lexer-new         # lexer
	dup lexer-pop                     # lexer nameTok
	dup >r                            # save nameTok
	token-lexeme                      # lexer name
	dup identifier? 0 == if "invalid function name for 'fn'" parse-error then
	>r                                # save name string
	drop                              # leave lexer only for params
	"(" lexer-expect drop            # consume '(' keep lexer
	fn-params                         # params lexer
	fn-collect-body                   # params bodyTokens
	swap >r                           # bodyTokens (r: params)
	fn-lexemes-from-tokens            # lexemes
	fn-validate-body                  # expr
	shunt                             # postfix
	r>                                # postfix params
	fn-build-body                     # body
	r> drop                           # drop name string
	r>                                # name token
	swap emit-definition
;
immediate
compile-only

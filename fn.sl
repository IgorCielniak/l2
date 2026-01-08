word call-syntax-rewrite            # ( fnameToken -- handled )
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
	dup "int" string= 0 == if "only 'int' parameters are supported in fn definitions" parse-error end
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

word fn-validate-body
	dup list-length 0 == if "empty function body" parse-error end
	dup 0 list-get token-lexeme "return" string= 0 == if "function body must start with 'return'" parse-error end
	dup list-last ";" string= 0 == if "function body must terminate with ';'" parse-error end
	list-clone                     # body body'
	list-pop drop                  # body expr' (trim trailing ';')
	list-pop-front drop            # body expr  (trim leading 'return')
	dup list-length 0 == if "missing return expression" parse-error end
end
compile-only


word fn-filter-raw-body             # bodyLexemes -- tokens
	list-new swap                   # out body
begin
	dup list-empty? if
		drop                           # out
		exit
	end
	list-pop-front                  # out body' tok
	swap >r                         # out tok           (r: body')
	dup "return" string= if
		drop
		r>
		continue
	end
	dup ";" string= if
		drop
		r>
		continue
	end
	list-append                     # out'
	r>                              # out' body'
	continue
again
end
compile-only


word fn-body->tokens                # bodyLexemes -- tokens
	dup list-length 0 == if "empty function body" parse-error end
	dup 0 list-get token-lexeme "return" string= if
		fn-validate-body              # expr
		shunt                         # postfix
		exit
	end
	fn-filter-raw-body
	dup list-length 0 == if "empty function body" parse-error end
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

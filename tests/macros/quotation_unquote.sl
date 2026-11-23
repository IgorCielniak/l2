import stdlib.sl

# `$(expr)` splices the value of compile-time code into a quotation.

# getter NAME VALUE: defines get-NAME pushing VALUE * 2.
word getter
  ct-parser-consume ct-parser-consume token-lexeme try-string>int drop
  `word get-$(over token-lexeme) $(dup 2 *) end` emit
  drop drop
end
immediate compile-only

getter answer 21
getter small 4

macro scaled (x) `$x $( `3` ) *` emit ;

word checks
  `a $( `b c` ) d` syntax>list list-length 4 == static-assert
  `$( list-new "x" list-append "y" list-append )` syntax>list list-length 2 == static-assert
  `pre$("mid")post` syntax>list 0 list-get token-lexeme "premidpost" string-equal? static-assert
  `$(1 2 +)` syntax>list 0 list-get token-lexeme "3" string-equal? static-assert
end
compile-time checks

word main
  get-answer puti cr
  get-small puti cr
  scaled 7 puti cr
end

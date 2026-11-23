# Variadic macro arguments: a macro body takes the rest of its input up
# to a terminator, as one Syntax value.
#
#   import macro_variadic.sl
#
#   # sum-of 1 2 3 end-sum   ->   1 2 + 3 +
#   macro sum-of ()
#     "end-sum" variadic-rest syntax>list
#     list-pop-front drop `$( )` emit
#     dup list-empty? 0 == while do
#       list-pop-front drop `$( ) +` emit
#       dup list-empty? 0 ==
#     end
#     drop
#   ;
#
#   # count( a ( b c ) d ) -> 6: the tokens between the outer parentheses
#   macro count ()
#     "(" ")" variadic-balanced variadic-count `$( )` emit
#   ;
#
# Fixed parameters still come first: `macro m (a b)` takes a and b, and the
# body can then take the variadic rest.

# The input up to (not including) `terminator`, which is consumed.  Missing
# terminator is an error at the macro call.
# [* | terminator] -> [* | syntax]
word variadic-rest
	dup ct-parser-collect-until             # terminator tokens found
	if
		nip list>syntax
	else
		drop "variadic argument list is missing its terminator '" swap string-append "'" string-append
		ct-parser-error
	end
end
compile-only

# `open` (which must come next) through the matching `close`, nested pairs
# included; returns the tokens between them.
# [*, open | close] -> [* | syntax]
word variadic-balanced
	over ct-parser-expected nip 0 == if
		drop "expected '" swap string-append "' to start the variadic arguments" string-append
		ct-parser-error
	end
	ct-parser-consume drop                  # the opener
	2dup ct-parser-collect-balanced         # open close tokens found
	if
		nip nip list>syntax
	else
		drop nip "variadic argument list is missing its closing '" swap string-append "'" string-append
		ct-parser-error
	end
end
compile-only

# The number of tokens in a variadic argument Syntax.
# [* | syntax] -> [* | count]
word variadic-count
	syntax-length
end
compile-only

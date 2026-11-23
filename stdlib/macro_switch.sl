# Macro expansion switch: between `macros-off` and `macros-on`, a
# macro name is compiled as an ordinary word call instead of being expanded.
#
#   import macro_switch.sl
#   macros-off
#   word f FD_STDOUT end      # calls a word named FD_STDOUT (not the constant)
#   macros-on
#
# Useful when a program defines runtime words that share names with macros,
# or to see what a region compiles to without its macros.  Only macro calls
# are affected: rewrites, token hooks, and immediate words still run.
#
# It is a language pack, `macro-switch`, with one token hook; the pack is
# active only while macros are off, so it costs nothing otherwise.

# [* | token] -> [* | handled]
word __macro-switch-hook
	dup token-lexeme "macros-on" string-equal? if
		drop "macro-switch" ct-lang-deactivate drop 1 exit
	end
	dup token-lexeme ct-macro-get-by-name nip if
		token-lexeme "word" swap ct-op-emit 1
	else
		drop 0
	end
end
compile-only

word macros-off
	"macro-switch" ct-lang-exists? 0 == if
		"macro-switch" ct-lang-create drop
		"macro-switch" "__macro-switch-hook" ct-lang-add-token-hook drop
	end
	"macro-switch" ct-lang-activate drop
end
immediate
compile-only

# Outside a `macros-off` region this does nothing (inside one, the token hook
# handles it before it is looked up).
word macros-on
end
immediate
compile-only

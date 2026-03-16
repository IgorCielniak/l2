# Optional control-structure overrides for L2 parser defaults.
# Import this file when you want custom compile-time implementations of
# if/else/for/while/do instead of the built-in Python parser behavior.

word ct-if-open
	"if_false" ct-new-label
	dup "branch_zero" swap ct-emit-op
	"if" ct-control-frame-new
	swap "false" swap ct-control-set
	nil "end" swap ct-control-set
	dup "false" ct-control-get "label" swap ct-control-add-close-op
	ct-control-push
end
compile-only

word ct-if-open-with-end
	"if_false" ct-new-label
	dup "branch_zero" swap ct-emit-op
	"if" ct-control-frame-new
	swap "false" swap ct-control-set
	swap "end" swap ct-control-set
	dup "false" ct-control-get "label" swap ct-control-add-close-op
	dup "end" ct-control-get "label" swap ct-control-add-close-op
	ct-control-push
end
compile-only

word if-base ct-if-open end
immediate
compile-only

word if
	ct-control-depth 0 > if-base
		ct-control-peek
		dup "type" ct-control-get "else" string= if-base
			dup "line" ct-control-get ct-last-token-line == if-base
				drop
				ct-control-pop >r
				r@ "end" ct-control-get dup nil? if-base
					drop "if_end" ct-new-label
				end
				ct-if-open-with-end
				r> drop
				exit
			end
		end
		drop
	end
	ct-if-open
end
immediate
compile-only

word else
	ct-control-pop >r
	r@ "end" ct-control-get dup nil? if-base
		drop "if_end" ct-new-label
	end
	dup "jump" swap ct-emit-op
	r@ "false" ct-control-get "label" swap ct-emit-op
	"else" ct-control-frame-new
	swap "end" swap ct-control-set
	dup "end" ct-control-get "label" swap ct-control-add-close-op
	ct-control-push
	r> drop
end
immediate
compile-only

word for
	"for_loop" ct-new-label
	"for_end" ct-new-label
	map-new
	"loop" 3 pick map-set
	"end" 2 pick map-set
	"for_begin" swap ct-emit-op
	"for" ct-control-frame-new
	swap "end" swap ct-control-set
	swap "loop" swap ct-control-set
	dup "end" ct-control-get >r
	dup "loop" ct-control-get >r
	map-new
	"loop" r> map-set
	"end" r> map-set
	"for_end" swap ct-control-add-close-op
	ct-control-push
end
immediate
compile-only

word while
	"begin" ct-new-label
	"end" ct-new-label
	over "label" swap ct-emit-op
	"while_open" ct-control-frame-new
	swap "end" swap ct-control-set
	swap "begin" swap ct-control-set
	ct-control-push
end
immediate
compile-only

word do
	ct-control-pop >r
	r@ "end" ct-control-get "branch_zero" swap ct-emit-op
	"while" ct-control-frame-new
	r@ "begin" ct-control-get "begin" swap ct-control-set
	r@ "end" ct-control-get "end" swap ct-control-set
	dup "begin" ct-control-get "jump" swap ct-control-add-close-op
	dup "end" ct-control-get "label" swap ct-control-add-close-op
	r> drop
	ct-control-push
end
immediate
compile-only

word block-opener
	next-token token-lexeme ct-register-block-opener
end
immediate
compile-only

word control-override
	next-token token-lexeme ct-register-control-override
end
immediate
compile-only

block-opener if
block-opener for
block-opener while

control-override if
control-override else
control-override for
control-override while
control-override do

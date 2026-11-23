# Optional control-structure overrides for L2 parser defaults.
# Import this file when you want custom compile-time implementations of
# if/else/for/while/do instead of the built-in Python parser behavior.

word ct-if-open
	"if_false" ct-label-new
	dup "branch_zero" swap ct-op-emit
	"if" ct-control-frame-new
	swap "false" swap ct-control-set
	nil "end" swap ct-control-set
	dup "false" ct-control-get? "label" swap ct-control-add-close-op
	ct-control-push
end
compile-only

word ct-if-open-with-end
	"if_false" ct-label-new
	dup "branch_zero" swap ct-op-emit
	"if" ct-control-frame-new
	swap "false" swap ct-control-set
	swap "end" swap ct-control-set
	dup "false" ct-control-get? "label" swap ct-control-add-close-op
	dup "end" ct-control-get? "label" swap ct-control-add-close-op
	ct-control-push
end
compile-only

word if-base ct-if-open end
immediate
compile-only

word if
	ct-control-depth 0 > if-base
		ct-control-peek
		dup "type" ct-control-get? "else" string-equal? if-base
			dup "line" ct-control-get? ct-parser-last-line == if-base
				drop
				ct-control-pop >r
				r@ "end" ct-control-get? dup nil? if-base
					drop "if_end" ct-label-new
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
	r@ "end" ct-control-get? dup nil? if-base
		drop "if_end" ct-label-new
	end
	dup "jump" swap ct-op-emit
	r@ "false" ct-control-get? "label" swap ct-op-emit
	"else" ct-control-frame-new
	swap "end" swap ct-control-set
	dup "end" ct-control-get? "label" swap ct-control-add-close-op
	ct-control-push
	r> drop
end
immediate
compile-only

word for
	"for_loop" ct-label-new
	"for_end" ct-label-new
	map-new
	"loop" 3 pick map-set
	"end" 2 pick map-set
	"for_begin" swap ct-op-emit
	"for" ct-control-frame-new
	swap "end" swap ct-control-set
	swap "loop" swap ct-control-set
	dup "end" ct-control-get? >r
	dup "loop" ct-control-get? >r
	map-new
	"loop" r> map-set
	"end" r> map-set
	"for_end" swap ct-control-add-close-op
	ct-control-push
end
immediate
compile-only

word while
	"begin" ct-label-new
	"end" ct-label-new
	over "label" swap ct-op-emit
	"while_open" ct-control-frame-new
	swap "end" swap ct-control-set
	swap "begin" swap ct-control-set
	ct-control-push
end
immediate
compile-only

word do
	ct-control-pop >r
	r@ "end" ct-control-get? "branch_zero" swap ct-op-emit
	"while" ct-control-frame-new
	r@ "begin" ct-control-get? "begin" swap ct-control-set
	r@ "end" ct-control-get? "end" swap ct-control-set
	dup "begin" ct-control-get? "jump" swap ct-control-add-close-op
	dup "end" ct-control-get? "label" swap ct-control-add-close-op
	r> drop
	ct-control-push
end
immediate
compile-only

word block-opener
	ct-parser-consume token-lexeme ct-parser-register-block-opener
end
immediate
compile-only

word control-override
	ct-parser-consume token-lexeme ct-control-register-override
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

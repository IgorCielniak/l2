import stdlib.sl
import io.sl

# dump takes the firts element from the stack
# and prints that much consequent elements
# from the stack while not modifying it

#dump [* | n] -> [*]
word dump
	1 swap
	for
		dup pick
		puti cr
		1 +
	end
	drop
end

#rdump [* | n] -> [*]
# dump return stack
word rdump
	1 swap
	for
		dup rpick
		puti cr
		1 +
	end
	drop
end

#fdump [* | n] -> [*]
#dump the stack with additional formatting
word fdump
	"[*, " write_buf
	1 swap 1 +
	while dup 3 > do
		dup pick
		puti
		1 -
		", " write_buf
	end
	1 - pick puti
	" | " write_buf
	1 - pick puti
	"]\n" write_buf
end

#frdump [* | n] -> [*]
#dump the return stack with additional formatting
word frdump
	"[*, " write_buf
	1 swap 1 -
	while dup 2 > do
		dup rpick
		puti
		1 -
		", " write_buf
	end
	rpick puti
	", " write_buf
	rpick puti
	" | " write_buf
	rpick puti
	"]\n" write_buf
end

#exec_word_ptr [* | ptr] -> [*]
:asm exec_word_ptr {
	mov rax, [r12]
	add r12, 8
	lea rbx, [rel .ret]
	push rbx
	jmp rax
.ret:
}
;

#trace [* | ptr] -> [*]
word trace
	>r
	depth >r

	"trace before depth: " write_buf
	r@ puti cr
	"trace before elements:\n" write_buf
	r@ dup 0 > if
		dump
	else
		drop
		"<empty>\n" write_buf
	end

	1 rpick exec_word_ptr

	depth dup >r
	"trace after depth: " write_buf
	dup puti cr
	"trace after elements:\n" write_buf
	dup 0 > if
		dump
	else
		drop
		"<empty>\n" write_buf
	end

	r> r>
	2dup -
	"trace delta: " write_buf
	dup 0 >= if
		"+" write_buf
	end
	dup puti
	" (before " write_buf
	1 pick puti
	", after " write_buf
	2 pick puti
	")\n" write_buf
	drop drop drop
	rdrop
end

#int3 [*] -> [*]
:asm int3 {
	int3
}
;

#abort [*] -> [*]
word abort
	"abort" eputs
	1 exit
end

#abort_msg [* | msg] -> [*]
word abort_msg
	eputs
	1 exit
end

#assert [* | cond] -> [*]
word assert
	if
	else
		"assertion failed" abort_msg
	end
end

#assert_msg [*, msg | cond] -> [*]
word assert_msg
	if
		2drop
	else
		abort_msg
	end
end


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


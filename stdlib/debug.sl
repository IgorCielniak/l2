import stdlib.sl
import io.sl

#dump [* | n] -> [*]

# dump takes the firts element from the stack
# and prints that much consequent elements
# from the stack while not modifying it
# all variations have the same stack effect

word dump
	1 swap
	for
		dup pick
		puti cr
		1 +
	end
	drop
end

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


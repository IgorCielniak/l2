# : dump ( n -- )

# dump takes the firts element from the stack
# and prints that much consequent elements
# from the stack while not modifying it

word dump
	1 swap
	for
		dup pick
		puti cr
		1 +
	end
	drop
end

# : int3 ( -- )
:asm int3 {
	int3
}
;


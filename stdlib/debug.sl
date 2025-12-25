# : dump ( n -- )

# dump takes the firts element from the stack
# and prints that much consequent elements
# from the stack while not modifying it

: dump
    1 swap
	for
        dup pick
		puti cr
        1 +
	next
    drop
;

# : int3 ( -- )
:asm int3 {
	int3
}
;


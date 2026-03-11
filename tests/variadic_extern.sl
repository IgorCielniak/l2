import stdlib.sl

# Test variadic extern declarations.
# For variadic externs, the TOS literal before the call is the number of
# extra variadic arguments.  The compiler consumes it (not passed to C).
# String literals push (ptr, len) — use drop to discard the length for C.

# printf: 1 fixed param (fmt), variadic args via TOS count
extern int printf(const char *fmt, ...)
extern int fflush(long stream)

# Custom C variadic: sums args until sentinel -1 is seen
extern long va_sum_sentinel(long first, ...)

# Non-variadic extern for comparison
extern long add_two(long a, long b)

word main
    # Test 1: non-variadic add_two
    10 20 add_two puti cr

    # Test 2: printf with 0 variadic args (just format string)
    "hello\n" drop 0 printf drop
    0 fflush drop

    # Test 3: printf with 2 variadic args
    "%d %d\n" drop 42 99 2 printf drop
    0 fflush drop

    # Test 4: va_sum_sentinel(10, 20, 30, -1) = 60
    10 20 30 -1 3 va_sum_sentinel puti cr

    0
end

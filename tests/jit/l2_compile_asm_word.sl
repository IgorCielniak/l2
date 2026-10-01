extern l2_compile 2 1
extern l2_release 1 0

import stdlib/stdlib.sl

# Exercise l2_compile with snippets that rely on :asm words from
# stdlib (dup, swap, over, +, *).  Locks down that compiled snippets
# behave identically to source executed at compile time.

word main
    "1 2 dup + +" l2_compile   # 1 + (2 + 2) = 5
    dup call puti cr
    l2_release

    "5 3 swap - " l2_compile   # 3 - 5 = -2
    dup call puti cr
    l2_release

    "10 20 over + swap drop" l2_compile   # ( 10 20 ) over -> ( 10 20 10 ), + -> ( 10 30 ), swap drop -> ( 30 )
    dup call puti cr
    l2_release

    "6 7 * 1 +" l2_compile     # 6 * 7 + 1 = 43
    dup call puti cr
    l2_release

    "asm word ok" puts cr
    0
end

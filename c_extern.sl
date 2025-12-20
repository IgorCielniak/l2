import stdlib/io.sl

# C-style externs (auto ABI handling)
extern long labs(long n)
extern void exit(int status)

: main
    # Test C-style extern with implicit ABI handling
    -10 labs puti cr
    
    # Test extern void
    0 exit
;

import ./stdlib/stdlib.sl

extern eval 2 0
extern eval_env 3 0
extern eval_program 2 1

word main
    # eval: define and use words dynamically
    "word sq dup * end" eval
    "7 sq" eval puti cr

    # eval: plain arithmetic expression
    "1 2 + 10 *" eval puti cr

    # eval: return multiple values and consume them in order
    "1 2 3" eval
    dup puti cr
    swap puti cr
    swap puti cr

    # eval_env: bridge runtime stack into eval context
    "7 8 9" get_stack_top eval_env
    dup puti cr
    swap puti cr
    swap puti cr

    # eval_env: snippet can drop values and preserve caller data
    123 "1 drop" get_stack_top eval_env puti cr

    # eval_env: string result export path
    "\"env-ok\"" get_stack_top eval_env write_buf cr

    # eval_program: isolated program execution with output
    "import stdlib.sl word main 42 puti cr end" eval_program drop
    "import stdlib.sl word triple 3 * end word main 9 triple puti cr end" eval_program drop

    "all-eval-variants-ok" puts cr
    0
end

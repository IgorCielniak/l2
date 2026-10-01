import stdlib.sl

extern eval_env 3 0

# Main program defines regular words.
word add_ten
    10 +
end

word times_two
    2 *
end

word main
    # 1. eval'd code calls a main-program word.
    "5 add_ten" get_stack_top eval_env
    puti cr

    # 2. eval'd code defines a new word (persists in eval env).
    "word triple 3 * end" get_stack_top eval_env

    # 3. Subsequent eval_env can use the newly defined word.
    "7 triple" get_stack_top eval_env
    puti cr

    # 4. Main program calls the eval-defined word (with runtime arg).
    9 "triple" get_stack_top eval_env
    puti cr

    # 5. eval-defined word composed with main-program words.
    "4 triple add_ten times_two" get_stack_top eval_env
    puti cr

    # 6. eval defines a word that itself calls a main-program word.
    "word plus_twenty add_ten add_ten end" get_stack_top eval_env
    "3 plus_twenty" get_stack_top eval_env
    puti cr

    # 7. Chaining calls: main calls eval-defined word, which calls
    # main-defined word, which is composed with an eval-defined word.
    2 "plus_twenty triple" get_stack_top eval_env
    puti cr

    # 8. Multi-value stack sharing.
    10 20 "+ triple" get_stack_top eval_env
    puti cr

    "interop-ok" puts cr
    0
end

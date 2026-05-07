extern void eval_env(const char *source, long source_len, long stack_top_addr)

import stdlib.sl
import debug.sl

:asm get_stack_top {
    mov rax, r12
    sub r12, 8
    mov [r12], rax
    ret
}
;

word assert_eq
    == assert
end

word test_basic
    "eval_env basic" puts
    depth >r
    111 222
    "1 2 +" get_stack_top eval_env
    dup 3 assert_eq
    1 pick 222 assert_eq
    2 pick 111 assert_eq
    depth r> 3 + assert_eq
    drop drop drop
end

word test_no_result
    "eval_env no-result" puts
    depth >r
    123
    "1 drop" get_stack_top eval_env
    dup 123 assert_eq
    depth r> 1 + assert_eq
    drop
end

word test_multi
    "eval_env multi" puts
    depth >r
    777
    "10 20 30" get_stack_top eval_env
    dup 30 assert_eq
    1 pick 20 assert_eq
    2 pick 10 assert_eq
    3 pick 777 assert_eq
    depth r> 4 + assert_eq
    drop drop drop drop
end

word test_string
    "eval_env string" puts
    depth >r
    "\"hello\"" get_stack_top eval_env
    dup 5 assert_eq
    over 0 != assert
    2dup write_buf cr
    depth r> 2 + assert_eq
    2drop
end

word test_mixed
    "eval_env mixed" puts
    depth >r
    888
    "1 \"hi\" 2" get_stack_top eval_env
    dup 2 assert_eq
    1 pick 2 assert_eq
    2 pick 0 != assert
    3 pick 1 assert_eq
    4 pick 888 assert_eq
    depth r> 5 + assert_eq
    drop drop drop drop drop
end

word test_chained
    "eval_env chained" puts
    depth >r
    999
    "1 2" get_stack_top eval_env
    "3 4 +" get_stack_top eval_env
    dup 7 assert_eq
    1 pick 2 assert_eq
    2 pick 1 assert_eq
    3 pick 999 assert_eq
    depth r> 4 + assert_eq
    drop drop drop drop
end

word main
    test_basic
    test_no_result
    test_multi
    test_string
    test_mixed
    test_chained
    "eval_env ok" puts
    0 exit
end
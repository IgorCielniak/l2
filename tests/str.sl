import stdlib.sl

word main
    "ggggggggh" "ggggggggh"
    strcmp
    puti cr

    "ggggggggh" "ggggggggd"
    strcmp
    puti cr

    "hello world hello world hello " "world hello world hello world"
    strconcat
    2dup
    puts
    free

    "hello world hello" "world" splitby
    for puts end
    "hello world hello world" "world" splitby
    for puts end
    "hello world hello world hello" "l" splitby
    for puts end

    "    f    " 2dup 2dup
    124 putc ltrim write_buf 124 putc cr
    124 putc rtrim write_buf 124 putc cr
    124 putc trim  write_buf 124 putc cr
end

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
end

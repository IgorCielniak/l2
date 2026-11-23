import stdlib/stdlib.sl
word observe_compile_error
    drop
end on-event compiler.error

word main
    missing_without_repair
end
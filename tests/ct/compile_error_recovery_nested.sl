import stdlib/stdlib.sl

word repair_compile_error
    drop "word nested_missing_word 42 end" ct-definition-register-source drop ct-recovery-resume drop
end on-event compiler.error

word compile_time_caller
    nested_missing_word
end immediate

word main
    compile_time_caller
    nested_missing_word putu cr
end
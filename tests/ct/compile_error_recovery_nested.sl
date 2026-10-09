import stdlib/stdlib.sl

word repair_compile_error
    "word nested_missing_word 42 end" ct-error-define ct-error-resume
end on-event compile.error

word compile_time_caller
    nested_missing_word
end immediate

word main
    compile_time_caller
    nested_missing_word putu cr
end
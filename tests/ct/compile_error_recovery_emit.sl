import stdlib/stdlib.sl

word repair_compile_error
    ct-error-restore-points list-length 1 >= static_assert
    get-event-payload
    "restore_point_id" map-get drop dup
    ct-error-restore-point drop
    ct-error-jump
    "word repaired_word 42 end" ct-error-define drop
    ct-error-resume
end on-event compile.error

word main
    repaired_word putu cr
end
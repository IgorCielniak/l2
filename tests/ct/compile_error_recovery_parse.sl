import stdlib/stdlib.sl

word repair_compile_error
    ct-error-restore-points list-length 1 >= static_assert
    get-event-payload
    "restore_point_id" map-get drop dup
    ct-error-restore-point drop
    ct-error-jump
    "word missing_ct_word 42 end" ct-error-define drop
    ct-error-resume
end on-event compile.error

word trigger_ct
    missing_ct_word
end
compile-time trigger_ct

word main
    missing_ct_word putu cr
end
import stdlib/stdlib.sl

word repair_compile_error
    ct-recovery-list-restore-points list-length 1 >= static-assert
    event-payload
    "restore-point-id" map-get drop dup
    ct-recovery-get-restore-point drop
    ct-recovery-jump
    "word missing_ct_word 42 end" ct-definition-register-source drop
    ct-recovery-resume drop
end on-event compiler.error

word trigger_ct
    missing_ct_word
end
compile-time trigger_ct

word main
    missing_ct_word putu cr
end
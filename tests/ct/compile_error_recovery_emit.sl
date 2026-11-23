import stdlib/stdlib.sl

word repair_compile_error
    ct-recovery-list-restore-points list-length 1 >= static-assert
    event-payload
    "restore-point-id" map-get drop dup
    ct-recovery-get-restore-point drop
    ct-recovery-jump
    "word repaired_word 42 end" ct-definition-register-source drop
    ct-recovery-resume drop
end on-event compiler.error

word main
    repaired_word putu cr
end
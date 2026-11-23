import stdlib/stdlib.sl

word configure_compile_error_retry
    event-payload "recovery" map-get drop
    dup "attempt" map-get drop 1 == if
        drop 2 ct-recovery-set-retry-limit drop
    else
        # The chain keeps the limit it started with; the new limit applies
        # to later chains.
        "limit" map-get drop 8 == static-assert
        ct-recovery-get-retry-limit 2 == static-assert
        "word configured_word 42 end" ct-definition-register-source drop
    end
    ct-recovery-resume drop
end on-event compiler.error

word trigger_ct
    configured_word
end
compile-time trigger_ct

word main
    configured_word putu cr
end
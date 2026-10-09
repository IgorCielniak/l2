import stdlib/stdlib.sl

word configure_compile_error_retry
    get-event-payload "attempt" map-get drop swap drop 1 == if
        2 ct-error-set-retry-limit drop
    else
        get-event-payload "attempt_limit" map-get drop swap drop 2 == static_assert
        "word configured_word 42 end" ct-error-define drop
    end
    ct-error-resume
end on-event compile.error

word trigger_ct
    configured_word
end
compile-time trigger_ct

word main
    configured_word putu cr
end
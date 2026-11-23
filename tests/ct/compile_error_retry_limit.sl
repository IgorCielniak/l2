import stdlib/stdlib.sl
word retry_compile_error
    # Resume every recoverable attempt; the terminal error is not resumable.
    event-payload "recoverable" map-get drop if ct-recovery-resume drop end
end on-event compiler.error

word trigger_ct
    missing_ct_word
end
compile-time trigger_ct

word main
    0
end
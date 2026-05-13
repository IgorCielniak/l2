import stdlib.sl
import meta.sl

word source_probe
    1 2 +
end

word ct_dispatch_probe
    "source_probe" meta-runtime-get-source
    dup 0 > static_assert
    drop
    drop
end
compile-time ct_dispatch_probe

word main
    "source_probe" meta-runtime-get-source write_buf cr
    0
end
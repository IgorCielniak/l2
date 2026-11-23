import stdlib.sl
import meta.sl

word source_probe
    1 2 +
end

# Two candidates: only the selected (higher-priority) one is ever returned.
word shadowed 1 end
priority 5 word shadowed "é" drop 2 end

word ct_dispatch_probe
    "source_probe" meta-runtime-get-source
    dup 0 > static-assert
    drop
    drop
    "shadowed" meta-runtime-get-source
    swap "priority" string-contains? 0 == static-assert
    "word shadowed \"é\" drop 2 end" string-utf8-length == static-assert
    "no_such_word" meta-runtime-get-source 0 == static-assert "" string-equal? static-assert
end
compile-time ct_dispatch_probe

word main
    "source_probe" meta-runtime-get-source write_buf cr
    "shadowed" meta-runtime-get-source dup puti cr write_buf cr
    "no_such_word" meta-runtime-get-source puti cr puti cr
    0
end

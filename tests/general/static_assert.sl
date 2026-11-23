import stdlib/debug.sl
import stdlib/io.sl

word ct_checks
    1 static-assert
    2 3 < static-assert
end
compile-time ct_checks

word main
    "static assert ok" puts
end

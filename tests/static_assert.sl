import stdlib/debug.sl
import stdlib/io.sl

word ct_checks
    1 static_assert
    2 3 < static_assert
end
compile-time ct_checks

word main
    "static assert ok" puts
end

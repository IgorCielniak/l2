import stdlib.sl
import defer.sl

word a
    "hello " write_buf
end

word b
    "world" puts
end

word main
    defer_frame
    &a defer
    &b defer

    run_defers

    defer_frame
    &b defer
    &a defer

    run_defers
end

import stdlib/debug.sl
import stdlib/io.sl

word main
    1 assert
    2 2 == assert
    "should not print" 1 assert_msg
    "debug assert ok" puts
    "assert_msg ok" puts
end

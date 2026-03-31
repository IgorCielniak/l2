import stdlib/stdlib.sl
import stdlib/time.sl

# Prints measured sleep duration in nanoseconds.
word main
    with t0 in
        monotonic_ns t0 !
        sleep_one_second
        monotonic_ns t0 @ -
        puti cr
    end
end

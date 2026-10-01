extern l2_compile 2 1
extern l2_release 1 0

import stdlib/stdlib.sl
import stdlib/control.sl

# Stress test: repeatedly compile a snippet, call it, and release the
# executable page.  A leaking l2_release (or leaking mmap allocations
# inside l2_compile) would either exhaust virtual memory or crash the
# runtime after many iterations.  50 cycles is small enough to run at
# compile time via the CT VM but large enough to shake out obvious
# lifecycle bugs.

word main
    0
    50 for
        drop
        "3 4 +" l2_compile   # ( fn )
        dup                   # ( fn fn )
        call                  # ( fn 7 )
        drop                  # ( fn )
        l2_release            # ( )
        0                     # keep the loop's implicit accumulator
    end
    drop
    "compile release stress ok" puts cr
    0
end

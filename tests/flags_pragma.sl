flags -L.
flags "-Iimport_helpers -lc"

import stdlib/stdlib.sl
import plain_import.sl

extern int fflush(long stream)

word main
    from_plain_helper
    0 fflush drop
    "flags_lib_ok" puts
    0
end

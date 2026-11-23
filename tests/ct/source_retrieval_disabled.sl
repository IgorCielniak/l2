import stdlib.sl
import meta.sl

# Without --source-embed there is no catalog: lookups are unavailable.
word source_probe
    1 2 +
end

word main
    "source_probe" meta-runtime-get-source puti cr puti cr
    0
end

extern eval_env 3 0

import stdlib.sl
import meta.sl

# With --source-embed, eval re-parses the program, including meta.sl's
# `extern const char *l2_source_catalog_lookup(const char *name, ...)`,
# and resolves host words such as `some`.
word some 1 2 end

word main
    "some 3 + +" get_stack_top eval_env
    puti cr
    0
end

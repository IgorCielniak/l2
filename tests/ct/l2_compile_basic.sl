extern l2_compile 2 1
extern l2_release 1 0

import stdlib.sl

# L2 string literals push (ptr, len) so we match the 2-arg C signature
# `void *l2_compile(const char *source, long source_len)` directly.
# `l2_compile` returns a native function pointer that, when invoked via
# `call` (from stdlib/core.sl), runs the snippet against the current L2
# data stack (r12).  Call `l2_release` to free the executable page.

word main
   "3 4 +" l2_compile          # -> ( fn_ptr )
   dup                          # keep a copy for l2_release
   call                         # -> pushes 7
   puti cr
   l2_release

   "5 5 * 1 -" l2_compile
   dup
   call                         # -> pushes 24
   puti cr
   l2_release

   # Multi-value: snippet pushes 3 values; consume all three
   "1 2 3" l2_compile
   dup
   call
   puti cr                      # 3
   puti cr                      # 2
   puti cr                      # 1
   l2_release

   "compile ok" puts cr
   0
end

extern eval_program 2 1

import stdlib.sl

# Each eval_program body runs its `main` in main.c's compile-time VM.  The
# leading constants make every check depend on exact stack depth, so a word
# that left an extra item behind would change the printed result.
word main
   # map-get consumes the map: 1 2 10 -> 13
   "import stdlib.sl word main 1 2 map-new \"a\" 10 map-set \"a\" map-get drop + + puti cr end" eval_program drop
   # map-get on a missing key pushes nil 0
   "import stdlib.sl word main map-new \"z\" map-get puti cr nil? puti cr end" eval_program drop
   # map-has? consumes the map: 1 + 1 -> 2
   "import stdlib.sl word main 1 map-new \"k\" 0 map-set \"k\" map-has? + puti cr end" eval_program drop
   # list-pop pushes list value found
   "import stdlib.sl word main list-new 4 list-append list-pop puti cr puti cr list-length puti cr end" eval_program drop
   # list-pop-front on an empty list pushes list nil 0
   "import stdlib.sl word main list-new list-pop-front puti cr nil? puti cr list-length puti cr end" eval_program drop
   # list-peek-front and list-clone consume the list
   "import stdlib.sl word main 1 list-new 9 list-append list-peek-front + puti cr end" eval_program drop
   "import stdlib.sl word main 1 list-new 5 list-append list-clone list-length + puti cr end" eval_program drop
   # negative list index
   "import stdlib.sl word main list-new 1 list-append 2 list-append -1 list-get puti cr end" eval_program drop
   # string-equal? and try-string>int
   "import stdlib.sl word main \"x\" \"x\" string-equal? puti cr end" eval_program drop
   "import stdlib.sl word main \"0x1F\" try-string>int puti cr puti cr end" eval_program drop
   "import stdlib.sl word main \"1__0\" try-string>int puti cr puti cr end" eval_program drop
   # map keys keep insertion order; integer and nil keys are supported
   "import stdlib.sl word main map-new \"b\" 1 map-set 3 2 map-set nil 4 map-set \"b\" 9 map-set map-keys dup 0 list-get \"b\" string-equal? puti cr dup 1 list-get puti cr 2 list-get nil? puti cr end" eval_program drop
   # string-length counts code points; identifier? rule
   "import stdlib.sl word main \"größe\" string-length puti cr \"größe\" identifier? puti cr \"1a\" identifier? puti cr end" eval_program drop
   # strict errors make eval_program fail (prints 0: the result is not success)
   "import stdlib.sl word main \"a\" 1 string-append end" eval_program 0 == puti cr
   "import stdlib.sl word main list-new 0 list-get end" eval_program 0 == puti cr
   "c vm values ok" puts cr
   0
end

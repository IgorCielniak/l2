extern eval 2 0

import stdlib.sl

word main
   "7 7 *" eval puti cr
   "9 9 +" eval puti cr
   "10 10 *" eval puti cr
   "1 2 3" eval
   dup puti cr
   swap puti cr
   swap puti cr
   "eval string ok" puts cr
   0
end
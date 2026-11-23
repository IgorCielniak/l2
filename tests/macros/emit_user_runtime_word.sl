import stdlib.sl
macro five () `5` emit ;
word mk-seven `7` emit end
immediate compile-only
word emit putc end
macro six () `6` emit ;
macro eight () 1 drop `8` emit ;
word main
  five puti cr
  six puti cr
  mk-seven puti cr
  eight puti cr
  65 emit 10 emit
end

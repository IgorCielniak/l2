import stdlib.sl
macro five () `5` emit ;
word mk-seven `7` emit end
immediate compile-only
word emit drop `99` end
compile-only
macro six () `6` emit ;
macro eight () 1 drop `8` emit ;
word main
  five puti cr
  six puti cr
  mk-seven puti cr
  eight puti cr
end

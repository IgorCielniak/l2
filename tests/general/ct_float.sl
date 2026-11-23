import stdlib.sl
import float.sl

# At compile time a float is its IEEE-754 bits, as at runtime, so the
# float.sl words give the same results in both.
word ct-checks
  1.5 2.25 f+ float>int 3 == static-assert
  7.9 float>int 7 == static-assert
  3 int>float 2 int>float f/ 1.5 f== static-assert
  -2.5 fneg 2.5 f== static-assert
  0.1 0.2 f< static-assert
  10.0 4.0 f- 2.5 f* float>int 15 == static-assert
end
compile-time ct-checks

word main
  1.5 2.25 f+ float>int puti cr
  7.9 float>int puti cr
  3 int>float 2 int>float f/ 1.5 f== puti cr
  -2.5 fneg 2.5 f== puti cr
  0.1 0.2 f< puti cr
  10.0 4.0 f- 2.5 f* float>int puti cr
end

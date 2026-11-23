import stdlib.sl

word bss_wide
  { 9223372036854775807 4294967296 -5 }:3 dup 8 + @ puti cr
  dup 16 + @ puti cr
  24 + @ puti cr
end

# Literals outside the 32-bit immediate range, and constant folding that
# must wrap to 64 bits like the runtime.
word main
  9223372036854775807 puti cr
  4294967296 puti cr
  2147483648 puti cr
  -2147483649 puti cr
  2147483647 puti cr
  -2147483648 puti cr
  -9223372036854775808 puti cr
  9223372036854775807 1 + puti cr
  -9223372036854775808 1 - puti cr
  4294967296 4294967296 * puti cr
  -9223372036854775808 -1 * puti cr
  18446744073709551615 puti cr
  9223372036854775807 1 + 0 < puti cr
  -9223372036854775808 7 / puti cr
  -9223372036854775808 7 % puti cr
  bss_wide
end

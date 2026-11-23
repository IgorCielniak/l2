import stdlib.sl
# The compile-time key/value store library.
import ct_store.sl
macro remember (x) "last" $x ct-store-set ;
macro recall () `$( "last" ct-store-get drop )` emit ;
remember 42
word check
  "last" ct-store-has? static-assert
  "missing" 7 ct-store-get-or 7 == static-assert
  "n" 5 ct-store-set "n" ct-store-get static-assert 5 == static-assert
  ct-store-keys list-length 2 == static-assert
  "n" ct-store-delete "n" ct-store-has? 0 == static-assert
end
compile-time check
word main recall puti cr end

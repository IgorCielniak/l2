import stdlib.sl

macro m_fixed (x y) `$x $y +` emit ;

macro m_none `1` emit ;

word check_macro_introspection
  "m_fixed" ct-macro-get-by-name static-assert
  dup "parameters" map-get drop
  dup list-length 2 == static-assert
  dup 0 list-get "x" string-equal? static-assert
  1 list-get "y" string-equal? static-assert
  dup "body" map-get drop syntax-length 6 == static-assert
  "metadata" map-get drop map-length 0 == static-assert

  "m_none" ct-macro-get-by-name static-assert
  "parameters" map-get drop list-length 0 == static-assert

  # dup is not a macro
  "dup" ct-macro-get-by-name 0 == static-assert drop
end
compile-time check_macro_introspection

word main
  1 puti cr
end

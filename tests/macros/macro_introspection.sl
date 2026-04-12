import stdlib.sl
import meta.sl

macro m_fixed (x y)
  $x $y +
;

macro m_var (x *rest)
  $x $*rest
;

macro m_pattern
  $a:int + 0 => $a ;
;

word check_macro_introspection
  "m_fixed" meta-macro-is-text static_assert
  "m_fixed" meta-macro-is-pattern 0 == static_assert

  "m_var" meta-macro-is-text static_assert
  "m_var" meta-macro-is-pattern 0 == static_assert

  "m_pattern" meta-macro-is-pattern static_assert
  "m_pattern" meta-macro-is-text 0 == static_assert

  "m_fixed" meta-macro-signature
  static_assert                    # found
  dup nil? static_assert           # variadic
  drop
  dup list-length 2 == static_assert
  dup 0 list-get "x" string= static_assert
  dup 1 list-get "y" string= static_assert
  drop

  "m_var" meta-macro-signature
  static_assert
  dup "rest" string= static_assert
  drop
  dup list-length 1 == static_assert
  dup 0 list-get "x" string= static_assert
  drop

  "m_pattern" meta-macro-signature
  0 == static_assert               # found flag should be false for pattern macros
  drop
  drop

  meta-macro-pattern-list list-length 1 >= static_assert
end
compile-time check_macro_introspection

word main
  1 puti cr
end

import stdlib.sl

word foo
  1 2 +
end

:asm foo_asm {
  nop
};

word ct_word_introspection_checks
  ct-definition-list-names list-length 1 > static-assert

  "dup" ct-definition-exists? static-assert
  "__definitely_missing_word__" ct-definition-exists? 0 == static-assert

  "foo" ct-definition-asm-by-name 0 == static-assert nil? static-assert
  "foo_asm" ct-definition-asm-by-name static-assert
  string-length 0 > static-assert
  "__definitely_missing_word__" ct-definition-asm-by-name 0 == static-assert drop

  "foo" ct-definition-body-by-name static-assert
  dup list-length 3 == static-assert

  dup 0 list-get "op" map-get static-assert
  "literal" string-equal? static-assert

  dup 0 list-get "data" map-get static-assert
  1 == static-assert

  dup 2 list-get "op" map-get static-assert
  "word" string-equal? static-assert

  dup 2 list-get "data" map-get static-assert
  "+" string-equal? static-assert

  drop

  "foo_asm" ct-definition-body-by-name 0 == static-assert drop
  "__definitely_missing_word__" ct-definition-body-by-name 0 == static-assert drop
end
compile-time ct_word_introspection_checks

word main
  "ct-word-introspection-ok" puts
end

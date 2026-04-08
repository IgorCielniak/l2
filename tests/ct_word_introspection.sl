import stdlib.sl

word foo
  1 2 +
end

:asm foo_asm {
  nop
};

word ct_word_introspection_checks
  ct-list-words list-length 1 > static_assert

  "dup" ct-word-exists? static_assert
  "__definitely_missing_word__" ct-word-exists? 0 == static_assert

  "foo" ct-get-word-asm nil? static_assert
  "foo_asm" ct-get-word-asm dup nil? 0 == static_assert
  string-length 0 > static_assert
  "__definitely_missing_word__" ct-get-word-asm nil? static_assert

  "foo" ct-get-word-body dup nil? 0 == static_assert
  dup list-length 3 == static_assert

  dup 0 list-get "op" map-get static_assert
  "literal" string= static_assert
  drop

  dup 0 list-get "data" map-get static_assert
  1 == static_assert
  drop

  dup 2 list-get "op" map-get static_assert
  "word" string= static_assert
  drop

  dup 2 list-get "data" map-get static_assert
  "+" string= static_assert
  drop

  drop

  "foo_asm" ct-get-word-body nil? static_assert
  "__definitely_missing_word__" ct-get-word-body nil? static_assert
end
compile-time ct_word_introspection_checks

word main
  "ct-word-introspection-ok" puts
end

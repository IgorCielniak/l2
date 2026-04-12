import stdlib.sl

macro include_add1 (x)
  ct-include "macro_template_include_snippet.tpl"
;

macro import_add2 (a b)
  ct-import "macro_template_import_lib.tpl"
  ct-call imported_add
;

macro import_add2_twice (a b)
  ct-import "macro_template_import_lib.tpl"
  ct-call imported_add
;

word main
  include_add1(4) puti cr
  import_add2(7, 8) puti cr
  import_add2_twice(1, 9) puti cr
end

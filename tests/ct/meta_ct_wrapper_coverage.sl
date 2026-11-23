import stdlib.sl
import meta.sl

word verify_meta_ct_wrapper_coverage
  "ct-parser-current" ct-definition-exists? static-assert
  "ct-parser-get-pos" ct-definition-exists? static-assert
  "ct-parser-remaining" ct-definition-exists? static-assert
end
compile-time verify_meta_ct_wrapper_coverage

word main
  1 puti cr
end

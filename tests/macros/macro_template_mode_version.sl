import stdlib.sl

macro permissive_unknown
  ct-permissive
  ct-if missing_symbol then
    1
  else
    0
  end
;

macro versioned_plus1 (x)
  ct-version "1.2.3"
  $x 1 +
;

word main
  permissive_unknown puti cr
  versioned_plus1(9) puti cr
end

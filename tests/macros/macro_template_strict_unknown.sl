import stdlib.sl

macro strict_unknown
  ct-strict
  ct-if missing_symbol then
    1
  else
    0
  end
;

word main
  strict_unknown puti cr
end

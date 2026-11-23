import stdlib.sl

# ct-parser-error-at reports at an explicit line/column of the user's file.
word check-it
  ct-parser-consume drop
  "value out of range" 12 3 ct-parser-error-at
end
immediate compile-only

word main
  check-it
  0
end

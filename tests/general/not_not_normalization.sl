import stdlib.sl

# `not` is logical (any nonzero value becomes 0), so `not not` normalizes a
# value to 0/1 and must not be removed by the peephole optimizer.  `0 ==` is
# rewritten to `not`, so `0 == 0 ==` exercises the same rule.
word main
    7 not not puti cr
    -5 not not puti cr
    0 not not puti cr
    7 0 == 0 == puti cr
    0 0 == 0 == puti cr
    0
end

import stdlib.sl

# `else <cond> if` on one line continues an else-chain that one `end` closes;
# writing one `end` per `if` (nested style) closes the same blocks.
word chained
  dup 1 == if "one" else dup 2 == if "two" else dup 3 == if "three" else "other" end
  puts drop
end

word nested-same-line
  dup 1 == if "one" else dup 2 == if "two" else "other" end end
  puts drop
end

word nested-next-line
  dup 1 == if "one" puts else dup 2 == if "two" puts else "other" puts end
  end
  drop
end

word partly-nested
  dup 1 == if "one" else dup 2 == if "two" else dup 3 == if "three" else "other" end end
  puts drop
end

# A shorthand `if` (no `end`) inside a loop is closed by the loop's `end`,
# even when more blocks follow in the same definition.
word shorthand-then-loop
  2 for
    dup 0 > if "pos" puts
  end
  1 for "after" puts end
  drop
end

word two-chains
  dup 1 == if "one" puts else dup 2 == if "two" puts end
  dup 3 == if "three" puts else dup 4 == if "four" puts end
  drop
end

word main
  1 chained 2 chained 3 chained 4 chained
  1 nested-same-line 2 nested-same-line 3 nested-same-line
  1 nested-next-line 2 nested-next-line 3 nested-next-line
  1 partly-nested 3 partly-nested 4 partly-nested
  1 shorthand-then-loop 0 shorthand-then-loop
  1 two-chains 2 two-chains 3 two-chains 4 two-chains
end

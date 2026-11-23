import stdlib.sl

# break leaves and continue restarts the innermost loop, in for,
# while ... do ... end, and begin ... again.
word main
  0 10 for 1 + dup 4 == if break end end puti cr                 # 4
  0 0 6 for 1 + dup 2 % 0 == if continue end swap 1 + swap end drop puti cr   # 3
  # break in an inner for leaves only the inner loop and drops its counter
  0 3 for 5 for 1 + break end end puti cr                        # 3
  # breaking a for whose count is a literal keeps it a real loop
  0 3 for 1 + break end puti cr                                  # 1
  0 while dup 100 < do 1 + dup 7 == if break end end puti cr     # 7
  0 0 while dup 10 < do 1 + dup 3 % if continue end swap 1 + swap end drop puti cr  # 3
  0 begin 1 + dup 5 == if break end again puti cr                # 5
  0 0 begin 1 + dup 9 > if break end dup 2 % if continue end swap 1 + swap again drop puti cr  # 4
  # an outer for counter is intact after an inner break
  0 4 for 2 for break end 1 + end puti cr                        # 4
  0
end

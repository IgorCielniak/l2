# Rule 110 Cellular Automaton
# Rule 110 is a Turing-complete cellular automaton

import stdlib/stdlib.sl
import stdlib/io.sl
import stdlib/mem.sl

# Configuration
macro WIDTH 0 80 ;
macro GENERATIONS 0 40 ;

# Apply Rule 110 to three cells (left center right -- new_state)
word rule110
    swap 2 *     # center*2
    rot 4 *      # left*4
    + +          # pattern = left*4 + center*2 + right
    
    dup 0 == if drop 0 else
    dup 1 == if drop 1 else
    dup 2 == if drop 1 else
    dup 3 == if drop 1 else
    dup 4 == if drop 0 else
    dup 5 == if drop 1 else
    dup 6 == if drop 1 else
    dup 7 == if drop 0 else
    drop 0
    end end end end end end end end
end

# Print a single cell (state -- )
word print_cell
    if 35 putc else 32 putc end
end

# Print the current generation (addr -- )
word print_gen
    WIDTH 
    while dup 0 > do
        over @ print_cell
        swap 8 + swap
        1 -
    end
    drop drop
    10 putc
end

# Get cell with wraparound (ptr idx -- value)
word get_cell
    dup 0 < if WIDTH + end
    dup WIDTH >= if WIDTH - end
    8 * + @
end

# Compute next generation (current next --)
word next_gen
    over over   # current next current next
    0           # current next current next i
    while dup WIDTH < do
        # Stack (bottom->top): current next current next i
        # Indices (0=top): i=0, next=1, current=2, next=3, current=4
        
        # Get left neighbor: current[i-1]
        2 pick      # current
        over        # i
        1 - get_cell
        # Stack: current next current next i left
        # Indices: left=0, i=1, next=2, current=3
        
        # Get center: current[i]
        3 pick      # current
        2 pick      # i
        get_cell
        # Stack: current next current next i left center
        
        # Get right: current[i+1]
        4 pick      # current
        3 pick      # i
        1 + get_cell
        # Stack: current next current next i left center right
        
        rule110
        # Stack: current next current next i result
        
        # Store in next[i]
        2 pick      # next
        2 pick      # i
        8 * +       # addr
        swap !
        # Stack: current next current next i
        
        1 +
    end
    drop
    2drop
    2drop
end

# Copy qword array (dest src count --)
word copy_arr
    while dup 0 > do
        over @              # get src value
        3 pick swap !       # store at dest
        swap 8 + swap       # src += 8
        rot 8 + -rot        # dest += 8
        1 -
    end
    drop 2drop
end

# Main entry point
word main
    WIDTH 8 * alloc   # current
    WIDTH 8 * alloc   # next
    
    # Initialize current to zeros
    over WIDTH for
        dup 0 !
        8 +
    end
    drop
    
    # Set rightmost cell to 1
    over WIDTH 1 - 8 * + 1 !
    
    # Run simulation
    GENERATIONS for
        over print_gen
        2dup next_gen
        over over WIDTH copy_arr
    end
    
    # Free memory
    swap WIDTH 8 * free
    WIDTH 8 * free
    
    0
end


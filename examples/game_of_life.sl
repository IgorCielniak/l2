# Conway's Game of Life

import stdlib/stdlib.sl
import stdlib/io.sl
import stdlib/mem.sl

macro WIDTH 0 40 ;
macro HEIGHT 0 24 ;
macro GENERATIONS 0 80 ;
macro CELLS 0 WIDTH HEIGHT * ;
macro FRAME_DELAY 0 35000000;

#cell_addr [*, grid, x | y] -> [* | addr]
word cell_addr
    WIDTH * + 8 * +
end

#get_cell [*, grid, x | y] -> [* | value]
word get_cell
    dup 0 < if
        2drop drop 0
    else dup HEIGHT >= if
        2drop drop 0
    else over 0 < if
        2drop drop 0
    else over WIDTH >= if
        2drop drop 0
    else
        cell_addr @
    end
end

#term_enter [*] -> [*]
word term_enter
    # Enter alternate screen: ESC[?1049h
    27 putc 91 putc 63 putc 49 putc 48 putc 52 putc 57 putc 104 putc
    # Hide cursor: ESC[?25l
    27 putc 91 putc 63 putc 50 putc 53 putc 108 putc
end

#term_leave [*] -> [*]
word term_leave
    # Show cursor: ESC[?25h
    27 putc 91 putc 63 putc 50 putc 53 putc 104 putc
    # Leave alternate screen (restores original terminal view): ESC[?1049l
    27 putc 91 putc 63 putc 49 putc 48 putc 52 putc 57 putc 108 putc
end

#clear_screen_home [*] -> [*]
word clear_screen_home
    # Clear full screen: ESC[2J
    27 putc 91 putc 50 putc 74 putc
    # Move cursor home: ESC[H
    27 putc 91 putc 72 putc
end

#frame_sleep [*] -> [*]
word frame_sleep
    # Busy wait between frames; tune FRAME_DELAY for your machine.
    FRAME_DELAY for
        1 drop
    end
end

#set_cell [*, grid, x, y | value] -> [*]
word set_cell
    >r
    cell_addr
    r> !
end

#count_neighbors [*, grid, x | y] -> [* | n]
word count_neighbors
    with g x y in
        0
        g x 1 - y 1 - get_cell +
        g x     y 1 - get_cell +
        g x 1 + y 1 - get_cell +
        g x 1 - y     get_cell +
        g x 1 + y     get_cell +
        g x 1 - y 1 + get_cell +
        g x     y 1 + get_cell +
        g x 1 + y 1 + get_cell +
    end
end

#print_cell [* | state] -> [*]
word print_cell
    if 35 putc else 46 putc end
end

#print_board [* | grid] -> [*]
word print_board
    0
    while dup HEIGHT < do
        0
        while dup WIDTH < do
            2 pick
            1 pick
            3 pick
            get_cell print_cell
            1 +
        end
        drop
        10 putc
        1 +
    end
    drop
    drop
    10 putc
end

#evolve [*, state | neighbors] -> [* | new_state]
word evolve
    over 1 == if
        nip
        dup 2 == if
            drop 1
        else
            dup 3 == if drop 1 else drop 0 end
        end
    else
        nip
        dup 3 == if drop 1 else drop 0 end
    end
end

#copy_qwords [*, dst, src | count] -> [*]
word copy_qwords
    while dup 0 > do
        over @
        3 pick swap !
        swap 8 + swap
        rot 8 + -rot
        1 -
    end
    drop 2drop
end

#clear_board [* | grid] -> [*]
word clear_board
    0
    while dup CELLS < do
        over over 8 * + 0 !
        1 +
    end
    drop
    drop
end

#seed_glider [* | grid] -> [*]
word seed_glider
    dup 1 0 1 set_cell
    dup 2 1 1 set_cell
    dup 0 2 1 set_cell
    dup 1 2 1 set_cell
    dup 2 2 1 set_cell
    drop
end

#step [*, current | next] -> [*, current | next]
word step
    0
    while dup HEIGHT < do
        0
        while dup WIDTH < do
            # current next y x
            3 pick
            1 pick
            3 pick
            get_cell
            # current next y x state

            4 pick
            2 pick
            4 pick
            count_neighbors
            # current next y x state neighbors

            evolve
            # current next y x new_state

            3 pick
            2 pick
            4 pick
            cell_addr
            swap !
            # current next y x

            1 +
        end
        drop
        1 +
    end
    drop
end

word main
    CELLS 8 * alloc
    CELLS 8 * alloc

    over clear_board
    dup clear_board

    over seed_glider

    term_enter

    GENERATIONS for
        clear_screen_home
        over print_board
        frame_sleep
        2dup step
        over over CELLS copy_qwords
    end

    term_leave

    swap CELLS 8 * free
    CELLS 8 * free

    0
end

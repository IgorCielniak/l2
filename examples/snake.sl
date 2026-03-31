# Terminal Snake (classic real-time: WASD steer, q quit)

import stdlib.sl
import arr.sl
import linux.sl
import time.sl

macro WIDTH 0 20 ;
macro HEIGHT 0 12 ;
macro CELLS 0 WIDTH HEIGHT * ;

macro CH_W 0 119 ;
macro CH_A 0 97 ;
macro CH_S 0 115 ;
macro CH_D 0 100 ;
macro CH_Q 0 113 ;
macro CH_w 0 87 ;
macro CH_a 0 65 ;
macro CH_s 0 83 ;
macro CH_d 0 68 ;
macro CH_q 0 81 ;
macro FRAME_DELAY_NS 0 350000000 ;

macro TCGETS 0 21505 ;
macro TCSETS 0 21506 ;
macro LFLAG_OFF 0 12 ;
macro ECHO 0 8 ;
macro ICANON 0 2 ;

# state layout (qwords)
macro ST_DIR 0 0 ;
macro ST_LEN 0 8 ;
macro ST_FOOD_X 0 16 ;
macro ST_FOOD_Y 0 24 ;
macro ST_GAME_OVER 0 32 ;
macro ST_QUIT 0 40 ;
macro ST_WIN 0 48 ;

# direction constants
macro DIR_RIGHT 0 0 ;
macro DIR_DOWN 0 1 ;
macro DIR_LEFT 0 2 ;
macro DIR_UP 0 3 ;

#xy_idx [*, x | y] -> [* | idx]
word xy_idx
    WIDTH * +
end

#board_get [*, board, x | y] -> [* | value]
word board_get
    xy_idx
    1 - arr_get
end

#board_set [*, board, x, y | value] -> [*]
word board_set
    >r
    xy_idx
    r> swap 1 - arr_set
end

#state_dir@ [* | state] -> [* | dir]
word state_dir@
    ST_DIR + @
end

#state_dir! [*, state | dir] -> [*]
word state_dir!
    swap ST_DIR + swap !
end

#state_len@ [* | state] -> [* | len]
word state_len@
    ST_LEN + @
end

#state_len! [*, state | len] -> [*]
word state_len!
    swap ST_LEN + swap !
end

#state_food_x@ [* | state] -> [* | x]
word state_food_x@
    ST_FOOD_X + @
end

#state_food_x! [*, state | x] -> [*]
word state_food_x!
    swap ST_FOOD_X + swap !
end

#state_food_y@ [* | state] -> [* | y]
word state_food_y@
    ST_FOOD_Y + @
end

#state_food_y! [*, state | y] -> [*]
word state_food_y!
    swap ST_FOOD_Y + swap !
end

#state_game_over@ [* | state] -> [* | flag]
word state_game_over@
    ST_GAME_OVER + @
end

#state_game_over! [*, state | flag] -> [*]
word state_game_over!
    swap ST_GAME_OVER + swap !
end

#state_quit@ [* | state] -> [* | flag]
word state_quit@
    ST_QUIT + @
end

#state_quit! [*, state | flag] -> [*]
word state_quit!
    swap ST_QUIT + swap !
end

#state_win@ [* | state] -> [* | flag]
word state_win@
    ST_WIN + @
end

#state_win! [*, state | flag] -> [*]
word state_win!
    swap ST_WIN + swap !
end

#term_enter [*] -> [*]
word term_enter
    # Enter alternate screen: ESC[?1049h
    27 putc 91 putc 63 putc 49 putc 48 putc 52 putc 57 putc 104 putc
    # Hide cursor: ESC[?25l
    27 putc 91 putc 63 putc 50 putc 53 putc 108 putc
end

#term_raw_on [*, orig | work] -> [*]
:asm term_raw_on {
    ; stack: orig (NOS), work (TOS)
    mov r14, [r12]        ; work
    mov r15, [r12 + 8]    ; orig
    add r12, 16

    ; ioctl(0, TCGETS, orig)
    mov rax, 16
    mov rdi, 0
    mov rsi, 21505
    mov rdx, r15
    syscall

    ; copy 64 bytes orig -> work
    mov rcx, 8
    mov rsi, r15
    mov rdi, r14
.copy_loop:
    mov rbx, [rsi]
    mov [rdi], rbx
    add rsi, 8
    add rdi, 8
    loop .copy_loop

    ; clear ECHO | ICANON in c_lflag (offset 12)
    mov eax, [r14 + 12]
    and eax, 0xFFFFFFF5
    mov [r14 + 12], eax

    ; c_cc[VTIME]=0 (offset 17+5), c_cc[VMIN]=0 (offset 17+6)
    mov byte [r14 + 22], 0
    mov byte [r14 + 23], 0

    ; ioctl(0, TCSETS, work)
    mov rax, 16
    mov rdi, 0
    mov rsi, 21506
    mov rdx, r14
    syscall
}
;

#stdin_nonblock_on [* | old_flags_ptr] -> [*]
:asm stdin_nonblock_on {
    mov r14, [r12]
    add r12, 8

    ; old_flags = fcntl(0, F_GETFL, 0)
    mov rax, 72
    mov rdi, 0
    mov rsi, 3
    xor rdx, rdx
    syscall
    mov [r14], rax

    ; fcntl(0, F_SETFL, old_flags | O_NONBLOCK)
    mov rbx, rax
    or rbx, 2048
    mov rax, 72
    mov rdi, 0
    mov rsi, 4
    mov rdx, rbx
    syscall
}
;

#stdin_nonblock_off [* | old_flags_ptr] -> [*]
:asm stdin_nonblock_off {
    mov r14, [r12]
    add r12, 8

    mov rax, 72
    mov rdi, 0
    mov rsi, 4
    mov rdx, [r14]
    syscall
}
;

#term_raw_off [* | orig] -> [*]
:asm term_raw_off {
    mov r14, [r12]
    add r12, 8

    mov rax, 16
    mov rdi, 0
    mov rsi, 21506
    mov rdx, r14
    syscall
}
;

#term_leave [*] -> [*]
word term_leave
    # Show cursor: ESC[?25h
    27 putc 91 putc 63 putc 50 putc 53 putc 104 putc
    # Leave alternate screen: ESC[?1049l
    27 putc 91 putc 63 putc 49 putc 48 putc 52 putc 57 putc 108 putc
end

#clear_screen_home [*] -> [*]
word clear_screen_home
    # Clear full screen: ESC[2J
    27 putc 91 putc 50 putc 74 putc
    # Move cursor home: ESC[H
    27 putc 91 putc 72 putc
end

#clear_board [* | board] -> [*]
word clear_board
    0
    while dup CELLS < do
        over over 8 * + 0 !
        1 +
    end
    drop
    drop
end

#init_state [* | state] -> [*]
word init_state
    dup DIR_RIGHT state_dir!
    dup 3 state_len!
    dup 0 state_food_x!
    dup 0 state_food_y!
    dup 0 state_game_over!
    dup 0 state_quit!
    dup 0 state_win!
    drop
end

#init_snake [*, board, xs | ys] -> [*]
word init_snake
    with b xs ys in
        WIDTH 2 /
        HEIGHT 2 /
        with cx cy in
            xs 0 cx swap 1 - arr_set
            ys 0 cy swap 1 - arr_set
            b cx cy 1 board_set

            xs 1 cx 1 - swap 1 - arr_set
            ys 1 cy swap 1 - arr_set
            b cx 1 - cy 1 board_set

            xs 2 cx 2 - swap 1 - arr_set
            ys 2 cy swap 1 - arr_set
            b cx 2 - cy 1 board_set
        end
    end
end

#spawn_food [*, board | state] -> [*]
word spawn_food
    with b s in
        rand syscall.getpid + CELLS %
        0
        0
        with start tried found in
            while tried CELLS < do
                start tried + CELLS %
                dup b swap 1 - arr_get 0 == if
                    dup WIDTH % s swap state_food_x!
                    dup WIDTH / s swap state_food_y!
                    drop
                    1 found !
                    CELLS tried !
                else
                    drop
                    tried 1 + tried !
                end
            end

            found 0 == if
                s 1 state_win!
            end
        end
    end
end

#draw_game [*, board, xs, ys | state] -> [*]
word draw_game
    with b xs ys s in
        "Snake (WASD to steer, q to quit)" puts
        "Score: " puts
        s state_len@ 3 - puti
        10 putc

        xs drop
        ys drop

        0
        while dup HEIGHT < do
            0
            while dup WIDTH < do
                over s state_food_y@ == if
                    dup s state_food_x@ == if
                        42 putc
                    else
                        over WIDTH * over +
                        b swap 1 - arr_get
                        if 111 putc else 46 putc end
                    end
                else
                    over WIDTH * over +
                    b swap 1 - arr_get
                    if 111 putc else 46 putc end
                end
                1 +
            end
            drop
            10 putc
            1 +
        end
        drop

        s state_game_over@ if
            "Game over!" puts
        end
        s state_win@ if
            "You win!" puts
        end
    end
end

#read_input [*, input_buf | state] -> [*]
word read_input
    with ibuf s in
        FD_STDIN ibuf 8 syscall.read
        dup 0 <= if
            drop
        else
            drop
            ibuf c@

            dup CH_Q == if
                drop
                s 1 state_quit!
            else dup CH_q == if
                drop
                s 1 state_quit!
            else dup CH_W == if
                drop
                s state_dir@ DIR_DOWN != if
                    s DIR_UP state_dir!
                end
            else dup CH_w == if
                drop
                s state_dir@ DIR_DOWN != if
                    s DIR_UP state_dir!
                end
            else dup CH_S == if
                drop
                s state_dir@ DIR_UP != if
                    s DIR_DOWN state_dir!
                end
            else dup CH_s == if
                drop
                s state_dir@ DIR_UP != if
                    s DIR_DOWN state_dir!
                end
            else dup CH_A == if
                drop
                s state_dir@ DIR_RIGHT != if
                    s DIR_LEFT state_dir!
                end
            else dup CH_a == if
                drop
                s state_dir@ DIR_RIGHT != if
                    s DIR_LEFT state_dir!
                end
            else dup CH_D == if
                drop
                s state_dir@ DIR_LEFT != if
                    s DIR_RIGHT state_dir!
                end
            else dup CH_d == if
                drop
                s state_dir@ DIR_LEFT != if
                    s DIR_RIGHT state_dir!
                end
            else
                drop
            end
        end
    end
end

#step_game [*, board, xs, ys | state] -> [*]
word step_game
    with b xs ys s in
        xs 0 1 - arr_get
        ys 0 1 - arr_get
        with hx hy in
            hx
            hy
            # Compute next head from direction.
            s state_dir@ DIR_RIGHT == if
                drop
                hx 1 +
                hy
            else s state_dir@ DIR_DOWN == if
                drop
                hx
                hy 1 +
            else s state_dir@ DIR_LEFT == if
                drop
                hx 1 -
                hy
            else
                drop
                hx
                hy 1 -
            end

            with nx ny in
                # dead flag from wall collision
                0
                nx 0 < if drop 1 end
                nx WIDTH >= if drop 1 end
                ny 0 < if drop 1 end
                ny HEIGHT >= if drop 1 end

                with dead in
                    dead if
                        s 1 state_game_over!
                    else
                        # grow flag
                        0
                        nx s state_food_x@ == if
                            ny s state_food_y@ == if
                                drop 1
                            end
                        end

                        with grow in
                            # when not growing, remove tail before collision check
                            grow 0 == if
                                s state_len@ 1 -
                                with ti in
                                    xs ti 1 - arr_get
                                    ys ti 1 - arr_get
                                    with tx ty in
                                        b tx ty 0 board_set
                                    end
                                end
                            end

                            # self collision
                            b nx ny board_get if
                                s 1 state_game_over!
                            else
                                # shift body
                                s state_len@
                                grow if
                                    # start at len for growth
                                else
                                    1 -
                                end
                                while dup 0 > do
                                    dup >r
                                    xs r@ xs r@ 2 - arr_get swap 1 - arr_set
                                    ys r@ ys r@ 2 - arr_get swap 1 - arr_set
                                    rdrop
                                    1 -
                                end
                                drop

                                # write new head
                                xs 0 nx swap 1 - arr_set
                                ys 0 ny swap 1 - arr_set
                                b nx ny 1 board_set

                                grow if
                                    s state_len@ 1 + s swap state_len!
                                    b s spawn_food
                                end
                            end
                        end
                    end
                end
            end
        end
    end
end

word main
    CELLS 8 * alloc
    CELLS 8 * alloc
    CELLS 8 * alloc
    56 alloc
    8 alloc
    64 alloc
    64 alloc
    8 alloc
    16 alloc

    with board xs ys state input term_orig term_work fd_flags sleep_ts in
        board clear_board
        state init_state
        board xs ys init_snake
        board state spawn_food

        sleep_ts 0 !
        sleep_ts 8 + FRAME_DELAY_NS !

        term_orig term_work term_raw_on
        fd_flags stdin_nonblock_on
        term_enter

        1
        while dup do
            drop
            clear_screen_home
            board xs ys state draw_game

            state state_game_over@ if
                0
            else state state_win@ if
                0
            else state state_quit@ if
                0
            else
                input state read_input
                state state_quit@ if
                    0
                else
                    board xs ys state step_game
                    sleep_ts sleep
                    1
                end
            end
        end
        drop

        clear_screen_home
        board xs ys state draw_game

        fd_flags stdin_nonblock_off
        term_orig term_raw_off
        term_leave

        sleep_ts 16 free
        fd_flags 8 free
        term_work 64 free
        term_orig 64 free
        input 8 free
        state 56 free
        ys CELLS 8 * free
        xs CELLS 8 * free
        board CELLS 8 * free
    end

    0
end

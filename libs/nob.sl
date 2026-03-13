import stdlib/stdlib.sl
import stdlib/linux.sl
import stdlib/mem.sl

# sh  [*, cmd_addr | cmd_len ] -> [* | exit_code ]
word sh
    swap
    >r                       # save cmd_addr
    >r                       # save cmd_len

    r@ 1 +
    dup >r                   # stash len+1 for munmap
    alloc
    dup 0 < if
        rdrop
        rdrop
        rdrop
    else
        dup >r               # remember buffer pointer
        drop

        3 rpick              # src addr
        0 rpick              # dst addr
        swap
        2 rpick              # len
        memcpy

        0 rpick
        2 rpick
        +
        0
        c!

        mem
        "/bin/sh" drop
        !
        mem 8 +
        "-c" drop
        !
        mem 16 +
        0 rpick
        !
        mem 24 +
        0
        !
        mem 32 +
        0
        !

        syscall.fork
        dup 0 < if
            >r
            1 rpick
            2 rpick
            free
            r>
            rdrop
            rdrop
            rdrop
            rdrop
        else
            dup 0 == if
                drop
                "/bin/sh" drop
                mem
                dup
                32 +
                syscall.execve
                drop
                127
                syscall.exit
            else
                mem
                40 +
                dup >r
                0
                0
                syscall.wait4
                dup 0 < if
                    >r
                    rdrop
                    1 rpick
                    2 rpick
                    free
                    r>
                    rdrop
                    rdrop
                    rdrop
                    rdrop
                else
                    drop
                    0 rpick
                    @
                    rdrop
                    dup
                    128 %
                    dup 0 != if
                        swap drop
                        128 +
                    else
                        drop
                        256 /
                    end
                    >r
                    1 rpick
                    2 rpick
                    free
                    r>
                    rdrop
                    rdrop
                    rdrop
                    rdrop
                end
            end
        end
    end
end

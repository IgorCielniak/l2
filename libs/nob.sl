import stdlib/stdlib.sl
import stdlib/linux.sl
import stdlib/mem.sl

# The exit code a shell reports for a wait4 status: the child's exit code,
# or 128 + the signal number when a signal ended it.
# sh-exit-code [* | status] -> [* | exit_code]
word sh-exit-code
    dup 128 %
    dup 0 != if
        nip 128 +
    else
        drop 256 /
    end
end

# Run `cmd` with /bin/sh -c in the caller's environment and wait for it.
# Returns the command's exit code, or a negative errno when the buffer, the
# fork or the wait fails.  Each call has its own buffer, laid out as
#   0: argv ("/bin/sh", "-c", command, NULL)   32: wait4 status
#   40: the command, NUL-terminated
# sh  [*, cmd_addr | cmd_len ] -> [* | exit_code ]
word sh
    with cmd_addr cmd_len in
        cmd_len 41 + alloc
        dup 0 >= if
            with buf in
                buf 40 + cmd_addr cmd_len memcpy 2drop
                buf 40 + cmd_len + 0 c!
                buf "/bin/sh" drop !
                buf 8 + "-c" drop !
                buf 16 + buf 40 + !
                buf 24 + 0 !

                syscall.fork
                dup 0 == if
                    drop
                    "/bin/sh" drop buf envp syscall.execve drop
                    127 syscall.exit
                end
                dup 0 >= if
                    buf 32 + 0 0 syscall.wait4
                    dup 0 >= if
                        drop buf 32 + @ sh-exit-code
                    end
                end
                buf cmd_len 41 + free
            end
        end
    end
end

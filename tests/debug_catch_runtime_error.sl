import stdlib/debug.sl
import stdlib/io.sl

:asm crash_segv {
	mov qword [0], 1
}
;

word no_crash
	123 drop
end

word main
	&no_crash catch_runtime_error puti cr
	&crash_segv catch_runtime_error puti cr
	&no_crash try puti cr
	&crash_segv try puti cr
	&crash_segv try_with_error puti cr puti cr
	0
end
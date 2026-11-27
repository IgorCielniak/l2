import ../stdlib/stdlib.sl
import ../stdlib/time.sl

# Legacy assembly implementation retained only for the benchmark.
:asm read_file_old {
	mov r14, [r12]
	mov r15, [r12 + 8]
	mov rdi, [r12 + 24]
	add r12, 32
	mov rax, 2
	xor rsi, rsi
	xor rdx, rdx
	syscall
	test rax, rax
	js .done
	mov r8, rax
	mov rax, 0
	mov rdi, r8
	mov rsi, r15
	mov rdx, r14
	syscall
	mov r10, rax
	mov rax, 3
	mov rdi, r8
	syscall
	mov rax, r10
.done:
	sub r12, 8
	mov [r12], rax
	ret
}
;

:asm write_file_old {
	mov r14, [r12]
	mov r15, [r12 + 8]
	mov rdx, [r12 + 16]
	mov rsi, [r12 + 24]
	add r12, 32

	mov rdi, rsi
	mov rsi, 577
	mov rdx, 438
	mov rax, 2
	syscall
	cmp rax, 0
	jl .fail_open
	mov r9, rax

	mov rax, 1
	mov rdi, r9
	mov rsi, r15
	mov rdx, r14
	syscall
	mov r10, rax
	cmp r10, 0
	jl .fail_write

	mov rax, 3
	mov rdi, r9
	syscall

	sub r12, 8
	mov [r12], r10
	ret

.fail_write:
	mov rax, 3
	mov rdi, r9
	syscall
	sub r12, 8
	mov [r12], r10
	ret

.fail_open:
	sub r12, 8
	mov [r12], rax
	ret
}
;

word read_file_new
	with path path_len buffer buf_len in
		path O_RDONLY 0 syscall.open
		dup 0 < if
		else
			with fd in
				fd buffer buf_len syscall.read
				with count in
					fd syscall.close drop
					count
				end
			end
		end
	end
end

word write_file_new
	with path path_len buf buf_len in
		path 577 438 syscall.open
		dup 0 < if
		else
			with fd in
				fd buf buf_len syscall.write
				with count in
					fd syscall.close drop
					count
				end
			end
		end
	end
end

word bench_write_old
	with path path_len data data_len iterations in
		monotonic_ns >r
		iterations for
			path path_len data data_len write_file_old drop
		end
		monotonic_ns r> -
	end
end

word bench_write_new
	with path path_len data data_len iterations in
		monotonic_ns >r
		iterations for
			path path_len data data_len write_file_new drop
		end
		monotonic_ns r> -
	end
end

word bench_read_old
	with path path_len buffer buf_len iterations in
		monotonic_ns >r
		iterations for
			path path_len buffer buf_len read_file_old drop
		end
		monotonic_ns r> -
	end
end

word bench_read_new
	with path path_len buffer buf_len iterations in
		monotonic_ns >r
		iterations for
			path path_len buffer buf_len read_file_new drop
		end
		monotonic_ns r> -
	end
end

word main
	5000 with iterations in
		"/tmp/l2_io_file_benchmark.dat" with path path_len in
			"benchmark payload for file IO\n" with data data_len in
				path path_len data data_len write_file_new drop

				"write old ns: " puts
				path path_len data data_len iterations bench_write_old dup puti cr
				"write new ns: " puts
				path path_len data data_len iterations bench_write_new dup puti cr

				path path_len read_file_size with buf_len in
					buf_len alloc with buffer in
						"read old ns: " puts
						path path_len buffer buf_len iterations bench_read_old dup puti cr
						"read new ns: " puts
						path path_len buffer buf_len iterations bench_read_new dup puti cr
						buffer buf_len free
					end
				end
			end
		end
	end
	0
end
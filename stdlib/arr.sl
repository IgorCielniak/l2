# Dynamic arrays (qword elements)
#
# Layout at address `arr`:
#   [arr + 0]  len   (qword)
#   [arr + 8]  cap   (qword)
#   [arr + 16] data  (qword)  = arr + 24
#   [arr + 24] elements (cap * 8 bytes)
#
# Allocation: mmap; free: munmap.
# Growth: allocate new block, copy elements, munmap old block.

# : arr_new ( cap -- arr )
:asm arr_new {
	mov r14, [r12]        ; requested cap
	cmp r14, 1
	jge .cap_ok
	mov r14, 1
.cap_ok:
	; bytes = 24 + cap*8
	mov rsi, r14
	shl rsi, 3
	add rsi, 24

	; mmap(NULL, bytes, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANON, -1, 0)
	xor rdi, rdi
	mov rdx, 3
	mov r10, 34
	mov r8, -1
	xor r9, r9
	mov rax, 9
	syscall

	; header
	mov qword [rax], 0
	mov [rax + 8], r14
	lea rbx, [rax + 24]
	mov [rax + 16], rbx

	; replace cap with arr pointer
	mov [r12], rax
	ret
}
;

# : arr_len ( arr -- len )
:asm arr_len {
	mov rax, [r12]
	mov rax, [rax]
	mov [r12], rax
	ret
}
;

# : arr_cap ( arr -- cap )
:asm arr_cap {
	mov rax, [r12]
	mov rax, [rax + 8]
	mov [r12], rax
	ret
}
;

# : arr_data ( arr -- ptr )
:asm arr_data {
	mov rax, [r12]
	mov rax, [rax + 16]
	mov [r12], rax
	ret
}
;

# : arr_free ( arr -- )
:asm arr_free {
	mov rbx, [r12]        ; base
	mov rcx, [rbx + 8]    ; cap
	mov rsi, rcx
	shl rsi, 3
	add rsi, 24
	mov rdi, rbx
	mov rax, 11
	syscall
	add r12, 8            ; drop arr
	ret
}
;

# : arr_reserve ( cap arr -- arr )
# Ensures capacity >= cap; returns (possibly moved) arr pointer.
:asm arr_reserve {
	mov rbx, [r12]        ; arr
	mov r14, [r12 + 8]    ; requested cap
	cmp r14, 1
	jge .req_ok
	mov r14, 1
.req_ok:
	mov rdx, [rbx + 8]    ; old cap
	cmp rdx, r14
	jae .no_change

	; alloc new block: bytes = 24 + reqcap*8
	mov rsi, r14
	shl rsi, 3
	add rsi, 24
	xor rdi, rdi
	mov rdx, 3
	mov r10, 34
	mov r8, -1
	xor r9, r9
	mov rax, 9
	syscall

	mov r10, rax          ; new base
	lea r9, [r10 + 24]    ; new data

	; header
	mov r8, [rbx]         ; len
	mov [r10], r8
	mov [r10 + 8], r14
	mov [r10 + 16], r9

	; copy elements from old data
	mov r11, [rbx + 16]   ; old data
	xor rcx, rcx          ; i
.copy_loop:
	cmp rcx, r8
	je .copy_done
	mov rdx, [r11 + rcx*8]
	mov [r9 + rcx*8], rdx
	inc rcx
	jmp .copy_loop
.copy_done:

	; munmap old block
	mov rsi, [rbx + 8]
	shl rsi, 3
	add rsi, 24
	mov rdi, rbx
	mov rax, 11
	syscall

	; return new arr only
	mov [r12 + 8], r10
	add r12, 8
	ret

.no_change:
	; drop cap, keep arr
	mov [r12 + 8], rbx
	add r12, 8
	ret
}
;

# : arr_push ( x arr -- arr )
:asm arr_push {
	mov rbx, [r12]        ; arr
	mov rcx, [rbx]        ; len
	mov rdx, [rbx + 8]    ; cap
	cmp rcx, rdx
	jb .have_space

	; grow: newcap = max(1, cap) * 2
	mov r14, rdx
	cmp r14, 1
	jae .cap_ok
	mov r14, 1
.cap_ok:
	shl r14, 1

	; alloc new block
	mov rsi, r14
	shl rsi, 3
	add rsi, 24
	xor rdi, rdi
	mov rdx, 3
	mov r10, 34
	mov r8, -1
	xor r9, r9
	mov rax, 9
	syscall

	mov r10, rax          ; new base
	lea r9, [r10 + 24]    ; new data

	; header
	mov rcx, [rbx]        ; len (reload; syscall clobbers rcx)
	mov [r10], rcx
	mov [r10 + 8], r14
	mov [r10 + 16], r9

	; copy old data
	mov r11, [rbx + 16]   ; old data
	xor r8, r8
.push_copy_loop:
	cmp r8, rcx
	je .push_copy_done
	mov rdx, [r11 + r8*8]
	mov [r9 + r8*8], rdx
	inc r8
	jmp .push_copy_loop
.push_copy_done:

	; munmap old block
	mov rsi, [rbx + 8]
	shl rsi, 3
	add rsi, 24
	mov rdi, rbx
	mov rax, 11
	syscall

	; switch to new base
	mov rbx, r10

.have_space:
	; store element at data[len]
	mov r9, [rbx + 16]
	mov rax, [r12 + 8]    ; x
	mov rcx, [rbx]        ; len
	mov [r9 + rcx*8], rax
	inc rcx
	mov [rbx], rcx

	; return arr only
	mov [r12 + 8], rbx
	add r12, 8
	ret
}
;

# : arr_pop ( arr -- x arr )
:asm arr_pop {
	mov rbx, [r12]        ; arr
	mov rcx, [rbx]        ; len
	test rcx, rcx
	jz .empty
	dec rcx
	mov [rbx], rcx
	mov rdx, [rbx + 16]   ; data
	mov rax, [rdx + rcx*8]
	jmp .push
.empty:
	xor rax, rax
.push:
	sub r12, 8
	mov [r12], rax
	ret
}
;

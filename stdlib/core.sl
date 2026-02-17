#get_addr [*] -> [* | addr]
:asm get_addr {
    mov rax, [rsp]
    sub r12, 8
    mov [r12], rax
};

#jmp [* | addr] -> [*]
:asm jmp {
    mov rax, [r12]
    add r12, 8
    jmp rax
};

# Reserve 64 bytes in .bss
# persistent: resb 64
# push the addr of it

#mem [*] -> [* | ptr]
:asm mem {
	lea rax, [rel persistent]
	sub r12, 8
	mov [r12], rax
}
;

#argc [*] -> [* | n]
:asm argc {
	extern sys_argc
	mov rax, [rel sys_argc]
	sub r12, 8
	mov [r12], rax
	ret
}
;

#argv [*] -> [* | ptr]
:asm argv {
	extern sys_argv
	mov rax, [rel sys_argv]
	sub r12, 8
	mov [r12], rax
	ret
}
;

#argv@ [* | n] -> [* | ptr]
:asm argv@ {
	extern sys_argv
	mov rbx, [r12]      ; n
	mov rax, [rel sys_argv]
	mov rax, [rax + rbx*8]
	mov [r12], rax
	ret
}
;

#c@ [* | addr] -> [* | byte]
:asm c@ {
	mov rax, [r12]         ; get address from stack
	movzx rax, byte [rax]  ; load byte at address, zero-extend to rax
	mov [r12], rax         ; store result back on stack
	ret
}
;

#c! [*, addr | byte] -> [*]
:asm c! {
	mov rax, [r12]         ; get byte value (TOS)
	add r12, 8             ; pop byte
	mov rbx, [r12]         ; get address (NOS)
	add r12, 8             ; pop address
	mov [rbx], al          ; store low byte at address
	ret
}
;

#r@ [*] -> [* | x]
:asm r@ {
	mov rax, [r13]         ; get value from return stack
	sub r12, 8             ; make room on data stack
	mov [r12], rax         ; push value to data stack
	ret
}
;

#dup [* | x] -> [*, x | x]
:asm dup {
	mov rax, [r12]         ; get top of stack
	sub r12, 8             ; make room
	mov [r12], rax         ; duplicate value
}
;

#drop [* | x] -> [*]
:asm drop {
	add r12, 8             ; remove top of stack
}
;

#over [*, x1 | x2] -> [*, x1, x2 | x1]
:asm over {
	mov rax, [r12 + 8]     ; get second item
	sub r12, 8              ; make room
	mov [r12], rax          ; push copy
}
;

#swap [*, x1 | x2] -> [*, x2 | x1]
:asm swap {
	mov rax, [r12]         ; get top
	mov rbx, [r12 + 8]     ; get second
	mov [r12], rbx         ; swap
	mov [r12 + 8], rax
}
;

#rot [*, x1, x2 | x3] -> [*, x2, x3 | x1]
:asm rot {
	mov rax, [r12]         ; x3 (top)
	mov rbx, [r12 + 8]     ; x2
	mov rcx, [r12 + 16]    ; x1 (bottom)
	mov [r12], rcx         ; new top = x1
	mov [r12 + 8], rax     ; new 2nd = x3
	mov [r12 + 16], rbx    ; new 3rd = x2
}
;

#-rot [*, x1, x2 | x3] -> [*, x3, x1 | x2]
:asm -rot {
	mov rax, [r12]         ; x3 (top)
	mov rbx, [r12 + 8]     ; x2
	mov rcx, [r12 + 16]    ; x1 (bottom)
	mov [r12], rbx         ; new top = x2
	mov [r12 + 8], rcx     ; new 2nd = x1
	mov [r12 + 16], rax    ; new 3rd = x3
}
;

#nip [*, x1 | x2] -> [* | x2]
:asm nip {
	mov rax, [r12]         ; get top
	add r12, 8             ; drop lower
	mov [r12], rax         ; keep original top
}
;

#tuck [*, x1 | x2] -> [*, x2, x1 | x2]
:asm tuck {
	mov rax, [r12]         ; x2 (top)
	mov rbx, [r12 + 8]     ; x1
	sub r12, 8             ; make room
	mov [r12], rax         ; x2
	mov [r12 + 8], rbx     ; x1
	mov [r12 + 16], rax    ; x2
}
;

#2dup [*, x1 | x2] -> [*, x1, x2, x1 | x2]
:asm 2dup {
	mov rax, [r12]         ; b (top)
	mov rbx, [r12 + 8]     ; a
	sub r12, 8             ; make room
	mov [r12], rbx         ; push a
	sub r12, 8             ; make room
	mov [r12], rax         ; push b
}
;

#2drop [*, x1 | x2] -> [*]
:asm 2drop {
	add r12, 16            ; remove two items
}
;

#2swap [*, x1, x2, x3 | x4] -> [*, x3, x4, x1 | x2]
:asm 2swap {
	mov rax, [r12]         ; d (top)
	mov rbx, [r12 + 8]     ; c
	mov rcx, [r12 + 16]    ; b
	mov rdx, [r12 + 24]    ; a (bottom)
	mov [r12], rcx         ; new top = b
	mov [r12 + 8], rdx     ; new 2nd = a
	mov [r12 + 16], rax    ; new 3rd = d
	mov [r12 + 24], rbx    ; new 4th = c
}
;

#2over [*, x1, x2, x3 | x4] -> [*, x3, x4, x1, x2, x3 | x4]
:asm 2over {
	mov rax, [r12 + 16]    ; b
	mov rbx, [r12 + 24]    ; a
	sub r12, 8             ; make room
	mov [r12], rbx         ; push a
	sub r12, 8             ; make room
	mov [r12], rax         ; push b
}
;

#+ [*, x1 | x2] -> [* | x3]
:asm + {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	add qword [r12], rax   ; add to next
}
;

#- [*, x1 | x2] -> [* | x3]
:asm - {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	sub qword [r12], rax   ; subtract from next
}
;

#* [*, x1 | x2] -> [* | x3]
:asm * {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	imul qword [r12]       ; multiply
	mov [r12], rax         ; store result
}
;

#/ [*, x1 | x2] -> [* | x3]
:asm / {
	mov rbx, [r12]         ; divisor
	add r12, 8             ; pop
	mov rax, [r12]         ; dividend
	cqo                    ; sign-extend
	idiv rbx               ; divide
	mov [r12], rax         ; store quotient
}
;

#% [*, x1 | x2] -> [* | x3]
:asm % {
	mov rbx, [r12]         ; divisor
	add r12, 8             ; pop
	mov rax, [r12]         ; dividend
	cqo                    ; sign-extend
	idiv rbx               ; divide
	mov [r12], rdx         ; store remainder
}
;

#== [*, x1 | x2] -> [* | flag]
:asm == {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	mov rbx, [r12]         ; get next
	cmp rbx, rax           ; compare
	mov rbx, 0
	sete bl                ; set if equal
	mov [r12], rbx         ; store flag
}
;

#!= [*, x1 | x2] -> [* | flag]
:asm != {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	mov rbx, [r12]         ; get next
	cmp rbx, rax           ; compare
	mov rbx, 0
	setne bl               ; set if not equal
	mov [r12], rbx         ; store flag
}
;

#< [*, x1 | x2] -> [* | flag]
:asm < {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	mov rbx, [r12]         ; get next
	cmp rbx, rax           ; compare
	mov rbx, 0
	setl bl                ; set if less
	mov [r12], rbx         ; store flag
}
;

#> [*, x1 | x2] -> [* | flag]
:asm > {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	mov rbx, [r12]         ; get next
	cmp rbx, rax           ; compare
	mov rbx, 0
	setg bl                ; set if greater
	mov [r12], rbx         ; store flag
}
;

#<= [*, x1 | x2] -> [* | flag]
:asm <= {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	mov rbx, [r12]         ; get next
	cmp rbx, rax           ; compare
	mov rbx, 0
	setle bl               ; set if less or equal
	mov [r12], rbx         ; store flag
}
;

#>= [*, x1 | x2] -> [* | flag]
:asm >= {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	mov rbx, [r12]         ; get next
	cmp rbx, rax           ; compare
	mov rbx, 0
	setge bl               ; set if greater or equal
	mov [r12], rbx         ; store flag
}
;

#@ [* | addr] -> [* | x]
:asm @ {
	mov rax, [r12]         ; get address
	mov rax, [rax]         ; load value
	mov [r12], rax         ; store on stack
}
;

#! [*, addr | x] -> [*]
:asm ! {
	mov rax, [r12]         ; get value (TOS)
	add r12, 8             ; pop value
	mov rbx, [r12]         ; get addr (NOS)
	add r12, 8			   ; pop addr
	mov [rbx], rax         ; store value at address
}
;

#mmap [*, addr, len, prot, flags, fd | offset] -> [* | addr]
:asm mmap {
	mov r9, [r12]          ; offset
	add r12, 8
	mov r8, [r12]          ; fd
	add r12, 8
	mov r10, [r12]         ; flags
	add r12, 8
	mov rdx, [r12]         ; prot
	add r12, 8
	mov rsi, [r12]         ; len
	add r12, 8
	mov rdi, [r12]         ; addr
	mov rax, 9             ; syscall: mmap
	syscall
	sub r12, 8
	mov [r12], rax         ; return addr
}
;

#munmap [*, addr | len] -> [* | res]
:asm munmap {
	mov rsi, [r12]         ; len
	add r12, 8
	mov rdi, [r12]         ; addr
	add r12, 8
	mov rax, 11            ; syscall: munmap
	syscall
	sub r12, 8
	mov [r12], rax         ; return value
}
;

#exit [* | code] -> [*]
:asm exit {
	mov rdi, [r12]         ; exit code
	add r12, 8
	mov rax, 60            ; syscall: exit
	syscall
}
;

#and [*, x1 | x2] -> [* | flag]
:asm and {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	mov rbx, [r12]         ; get next
	test rax, rax
	setz cl
	test rbx, rbx
	setz dl
	movzx rcx, cl
	movzx rdx, dl
	and rcx, rdx           ; logical and
	mov [r12], rcx         ; store flag
}
;

#or [*, x1 | x2] -> [* | flag]
:asm or {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	mov rbx, [r12]         ; get next
	test rax, rax
	setz cl
	test rbx, rbx
	setz dl
	movzx rcx, cl
	movzx rdx, dl
	or rcx, rdx            ; logical or
	mov [r12], rcx         ; store flag
}
;

#not [* | x] -> [* | flag]
:asm not {
	mov rax, [r12]         ; get value
	test rax, rax
	setz al                ; set if zero
	movzx rax, al
	mov [r12], rax         ; store flag
}
;

#>r [* | x] -> [*]
:asm >r {
	mov rax, [r12]         ; get value
	add r12, 8             ; pop
	sub r13, 8             ; make room on return stack
	mov [r13], rax         ; push to return stack
}
;

#r> [*] -> [* | x]
:asm r> {
	mov rax, [r13]         ; get value from return stack
	add r13, 8             ; pop return stack
	sub r12, 8             ; make room on data stack
	mov [r12], rax         ; push to data stack
}
;

#rdrop [*] -> [*]
:asm rdrop {
	add r13, 8             ; pop return stack
}
;

#pick [* | n] -> [* | x]
:asm pick {
	mov rcx, [r12]         ; get index
	add r12, 8             ; pop
	mov rax, [r12 + rcx * 8] ; get value at index
	sub r12, 8             ; make room
	mov [r12], rax         ; push value
}
;

#rpick [* | n] -> [* | x]
:asm rpick {
	mov rcx, [r12]         ; get index
	add r12, 8             ; pop
	mov rax, [r13 + rcx * 8] ; get value from return stack
	sub r12, 8             ; make room
	mov [r12], rax         ; push value
}
;

#neg [* | x] -> [* | -x]
:asm neg {
    mov rax, [r12]   ; get value
    neg rax          ; arithmetic negation
    mov [r12], rax   ; store result
}
;

#abs [* | x] -> [* | |x|]
:asm abs {
	mov rax, [r12]   ; get value
	test rax, rax    ; check sign
	jge .done        ; keep if non-negative
	neg rax          ; flip sign when negative
.done:
	mov [r12], rax   ; store result
}
;

#bitnot [* | bool] -> [* | bool]
:asm bitnot {
    mov rax, [r12]   ; get value
    xor rax, 1       ; flip lowest bit
    mov [r12], rax   ; store result
}
;

#band [*, x1 | x2] -> [* | x3]
:asm band {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	and qword [r12], rax   ; bitwise and
}
;

#bor [*, x1 | x2] -> [* | x3]
:asm bor {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	or qword [r12], rax    ; bitwise or
}
;

#bxor [*, x1 | x2] -> [* | x3]
:asm bxor {
	mov rax, [r12]         ; get top
	add r12, 8             ; pop
	xor qword [r12], rax   ; bitwise xor
}
;

#bnot [* | x] -> [* | x]
:asm bnot {
	not qword [r12]        ; bitwise not
}
;

#shl [*, x1 | x2] -> [* | x3]
:asm shl {
	mov rcx, [r12]         ; shift count
	add r12, 8             ; pop
	shl qword [r12], cl    ; logical left shift
}
;

#sal [*, x1 | x2] -> [* | x3]
:asm sal {
	mov rcx, [r12]         ; shift count
	add r12, 8             ; pop
	sal qword [r12], cl    ; arithmetic left shift (same as shl)
}
;

#shr [*, x1 | x2] -> [* | x3]
:asm shr {
	mov rcx, [r12]         ; shift count
	add r12, 8             ; pop
	shr qword [r12], cl    ; logical right shift
}
;

#sar [*, x1 | x2] -> [* | x3]
:asm sar {
	mov rcx, [r12]         ; shift count
	add r12, 8             ; pop
	sar qword [r12], cl    ; arithmetic right shift
}
;

#rol [*, x1 | x2] -> [* | x3]
:asm rol {
	mov rcx, [r12]         ; shift count
	add r12, 8             ; pop
	rol qword [r12], cl    ; rotate left
}
;

#ror [*, x1 | x2] -> [* | x3]
:asm ror {
	mov rcx, [r12]         ; shift count
	add r12, 8             ; pop
	ror qword [r12], cl    ; rotate right
}
;

#inc [* | x] -> [* | x+1]
:asm inc {
	inc qword [r12]
}
;

#dec [* | x] -> [* | x-1]
:asm dec {
	dec qword [r12]
}
;

#min [*, x1 | x2] -> [* | x3]
:asm min {
	mov rax, [r12]         ; x2
	add r12, 8             ; pop
	mov rbx, [r12]         ; x1
	cmp rbx, rax
	cmovg rbx, rax         ; if x1 > x2, pick x2
	mov [r12], rbx
}
;

#max [*, x1 | x2] -> [* | x3]
:asm max {
	mov rax, [r12]         ; x2
	add r12, 8             ; pop
	mov rbx, [r12]         ; x1
	cmp rbx, rax
	cmovl rbx, rax         ; if x1 < x2, pick x2
	mov [r12], rbx
}
;

#clamp [*, x, lo | hi] -> [* | y]
:asm clamp {
	mov rax, [r12]         ; hi
	mov rbx, [r12 + 8]     ; lo
	mov rcx, [r12 + 16]    ; x
	cmp rcx, rbx
	cmovl rcx, rbx         ; if x < lo -> lo
	cmp rcx, rax
	cmovg rcx, rax         ; if x > hi -> hi
	mov [r12 + 16], rcx
	add r12, 16            ; drop lo, hi
}
;

#time [*] -> [* | t]
:asm time {
	mov rax, 201           ; syscall: time
	xor rdi, rdi
	syscall
	sub r12, 8
	mov [r12], rax
	ret
}
;

#rand [*] -> [* | n]
:asm rand {
	lea rbx, [rel persistent]
	mov rax, [rbx]         ; state
	test rax, rax
	jne .seeded
	; seed with time()
	mov rax, 201           ; syscall: time
	xor rdi, rdi
	syscall
	mov [rbx], rax
.seeded:
	mov rax, [rbx]
	mov rcx, 1103515245
	imul rax, rcx
	add rax, 12345
	mov [rbx], rax
	shr rax, 16
	and rax, 0x7fff
	sub r12, 8
	mov [r12], rax
	ret
}
;

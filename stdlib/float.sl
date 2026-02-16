# L2 Floating Point Library (Double Precision)

# Arithmetic
:asm f+ {
    movq xmm0, [r12]
    add r12, 8
    movq xmm1, [r12]
    addsd xmm1, xmm0
    movq [r12], xmm1
} ;

:asm f- {
    movq xmm0, [r12]
    add r12, 8
    movq xmm1, [r12]
    subsd xmm1, xmm0
    movq [r12], xmm1
} ;

:asm f* {
    movq xmm0, [r12]
    add r12, 8
    movq xmm1, [r12]
    mulsd xmm1, xmm0
    movq [r12], xmm1
} ;

:asm f/ {
    movq xmm0, [r12]
    add r12, 8
    movq xmm1, [r12]
    divsd xmm1, xmm0
    movq [r12], xmm1
} ;

:asm fneg {
    movq xmm0, [r12]
    mov rax, 0x8000000000000000
    movq xmm1, rax
    xorpd xmm0, xmm1
    movq [r12], xmm0
} ;

# Comparison
:asm f== {
    movq xmm0, [r12]
    add r12, 8
    movq xmm1, [r12]
    ucomisd xmm0, xmm1
    mov rax, 0
    setz al
    mov [r12], rax
} ;

:asm f< {
    movq xmm0, [r12]      ; a
    add r12, 8
    movq xmm1, [r12]      ; b
    ucomisd xmm0, xmm1
    mov rax, 0
    seta al               ; Above (a > b) -> b < a
    mov [r12], rax
} ;

:asm f> {
    movq xmm0, [r12]      ; a
    add r12, 8
    movq xmm1, [r12]      ; b
    ucomisd xmm1, xmm0
    mov rax, 0
    seta al               ; b > a
    mov [r12], rax
} ;

# Conversion
:asm int>float {
    cvtsi2sd xmm0, [r12]
    movq [r12], xmm0
} ;

:asm float>int {
    cvttsd2si rax, [r12]
    mov [r12], rax
} ;

# Output
extern int printf(char* fmt, double x)
extern int fflush(void* stream)

word fput
    "%f" drop swap printf drop
    0 fflush drop
end

word fputln
    "%f\n" drop swap printf drop
    0 fflush drop
end
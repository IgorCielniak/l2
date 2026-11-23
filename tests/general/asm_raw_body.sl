import stdlib.sl

# caller's comment outside asm is fine too
:asm seven {
    ; caller's x: pushes 7 } not the end
    sub r12, 8       ; it's "quoted" and `ticked`
    mov qword [r12], 7
}
;

:asm empty { }
;

word main
  seven puti cr
  empty
end

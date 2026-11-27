import stdlib.sl

# A legacy extern without counts must not read on into the next
# declaration looking for a C-style '('.
extern l2_unused_legacy_symbol
:asm seven ( effects string ) {
    sub r12, 8
    mov qword [r12], 7
}
;

word main
  seven puti cr
end

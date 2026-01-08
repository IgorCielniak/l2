import ../stdlib/linux.sl

# to demonstrate the ability to not use the stdlib at all, the return codes of syscall are not handeled (droped), in a real world scenario you would want to drop them

word main
    1
    "hello"
    syscall.write
    syscall
    #drop

    1
    " world"
    3
    syscall.write.num
    syscall
    #drop

    1
    "!\n"
    syscall.write.argc
    syscall.write.num
    syscall
    #drop

    1
    "(via syscall3)\n"
    syscall.write.num
    syscall3
    #drop

    0
end

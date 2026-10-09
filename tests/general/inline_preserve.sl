inline preserve word fn
    "hello"
end

priority 1 inline preserve word unused_fn
    42
end

word main
    &fn
    1 fn 3 1 syscall 0
end
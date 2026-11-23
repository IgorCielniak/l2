import stdlib.sl

word a
    ct-definition-predeclare-all
    b 42 == static-assert
end compile-time a

word b
    42
end

word main
    "hello" puts
end

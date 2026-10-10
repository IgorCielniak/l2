import stdlib.sl

word a
    ct-predef-all
    b 42 == static_assert
end compile-time a

word b
    42
end

word main
    "hello" puts
end

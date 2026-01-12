import stdlib/stdlib.sl

word main
    10 1 < if
        "first" puts
    else 1 2 < if
        "second" puts
    else
        "third" puts
    end
end

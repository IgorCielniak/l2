import stdlib/stdlib.sl

word classify
    with n in
        n 0 < if
            "neg" puts
        else n 0 == if
            "zero" puts
        else n 10 < if
            "small" puts
        else
            "big" puts
        end
    end
end

word main
    -1 classify
    0 classify
    3 classify
    20 classify
    0
end

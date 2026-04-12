import stdlib/stdlib.sl

word one_end
    0 if
        "a" puts
    else
        0 if
            "b" puts
        else
            1 if
                "one" puts
            else
                "x" puts
    end
end

word two_end
    0 if
        "a" puts
    else
        0 if
            "b" puts
        else
            1 if
                "two" puts
            else
                "x" puts
            end
    end
end

word three_end
    0 if
        "a" puts
    else
        0 if
            "b" puts
        else
            1 if
                "three" puts
            else
                "x" puts
            end
        end
    end
end

word main
    one_end
    two_end
    three_end
    0
end

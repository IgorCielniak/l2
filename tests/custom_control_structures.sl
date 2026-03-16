import stdlib/stdlib.sl
import stdlib/control.sl

word main
    1 if
        11 puti cr
    else
        99 puti cr
    end

    0 if
        99 puti cr
    else
        22 puti cr
    end

    0 if
        500 puti cr
    else 1 if
        33 puti cr
    else
        44 puti cr
    end

    0
    5 for
        1 +
    end
    puti cr

    0
    while dup 3 < do
        1 +
    end
    puti cr
end

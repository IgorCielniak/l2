import stdlib/stdlib.sl
import stdlib/io.sl

word strcmp
    3 pick 2 pick @ swap @ ==
end

word main
    "g" "g"
    strcmp
    puti cr
    puts
    puts
end
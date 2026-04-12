import stdlib/debug.sl
import stdlib/io.sl

word push2
    10
    20
end

word pop1
    drop
end

word main
    &push2 trace
    &pop1 trace
    drop
end

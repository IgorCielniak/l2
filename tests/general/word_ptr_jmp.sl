import stdlib/stdlib.sl
import stdlib/io.sl

word target
    "via word ptr\n" puts
end

word main
    &target
    jmp
end

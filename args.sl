import stdlib/stdlib.sl

word main
    0 argc for
        dup
        argv@ dup strlen puts
        1 +
    end
end

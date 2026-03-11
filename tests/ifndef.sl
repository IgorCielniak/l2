import stdlib/stdlib.sl
import stdlib/io.sl

# No -D flags, so ifdef FOO is false, ifndef FOO is true

ifdef FOO
word dead_code
    "BUG" puts cr
end
endif

ifndef FOO
word guarded
    "guard_ok" puts cr
end
endif

# elsedef: ifdef FALSE → skip, elsedef → include
ifdef MISSING
word wrong
    "BUG" puts cr
end
elsedef
word right
    "else_ok" puts cr
end
endif

word main
    guarded
    right
    0
end

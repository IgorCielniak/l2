import stdlib/stdlib.sl
import stdlib/io.sl

# Test ifdef: TESTFLAG is defined via -D TESTFLAG
ifdef TESTFLAG
word show_flag
    "flag_on" puts cr
end
endif

# Test ifndef: NOPE is NOT defined
ifndef NOPE
word show_nope
    "nope_off" puts cr
end
endif

# Test ifdef with elsedef
ifdef TESTFLAG
word branch
    "yes" puts cr
end
elsedef
word branch
    "no" puts cr
end
endif

# Test nested: inner depends on outer
ifdef TESTFLAG
ifndef NOPE
word nested
    "nested_ok" puts cr
end
endif
endif

word main
    show_flag
    show_nope
    branch
    nested
    0
end

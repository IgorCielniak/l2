import stdlib/stdlib.sl

define LOCAL

ifdef LOCAL
word show_local
    "local_yes" puts
end
elsedef
word show_local
    "local_no" puts
end
endif

ifndef LOCAL
word show_not_local
    "not_local_yes" puts
end
elsedef
word show_not_local
    "not_local_no" puts
end
endif

word main
    show_local
    show_not_local
    0
end

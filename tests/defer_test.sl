import defer.sl

word a
    "aa" puts
end

word b
    "bb" puts
end

word main
    &a defer
    &b defer
    "start" puts
    "end" puts
    run_defers
    "after defers run" puts
end

import stdlib.sl

# After an error inside a definition the parser skips to the end of that
# definition, so the inner blocks after the error do not produce more errors.
word broken
  1 if
    else else
    2 if 3 drop end
    4 for 5 drop end
  end
end

word fine 1 drop end

word main 0 end

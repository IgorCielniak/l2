# The compiler emits word.compile.begin with the word name in its payload.
import stdlib/stdlib.sl

word inspect_word_compile_event
  event-payload dup
  "name" map-get static-assert
  drop
  "name" map-get drop repr puts
end on-event word.compile.begin

word watched
    0
end

word main
    42 putu
end

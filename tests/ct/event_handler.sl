# The compiler emits word.compile.begin with the word name in its payload.
import stdlib/stdlib.sl

word inspect_word_compile_event
  get-event-payload
  "name" map-get static_assert
  drop drop
  get-event-payload
  "name" map-get drop ct-repr puts
end on-event word.compile.begin

word watched
    0
end

word main
    42 putu
end

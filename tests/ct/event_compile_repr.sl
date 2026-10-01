import stdlib.sl

word inspect_word_compile_event
  dup get-event-payload
  "name" map-get static_assert
  drop drop
end on-event word.compile.begin

word print_debug_payload
  get-event-payload ct-repr puts
end on-event debug.payload

word emit_debug_payload
  "debug.payload" event-object-create
  "answer" 42 event-object-prop-append
  "source" "compile-time" event-object-prop-append
  event-object-emit
end
compile-time emit_debug_payload

word main
  0
end
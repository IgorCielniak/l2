import stdlib.sl

word inspect_word_compile_event
  event-payload
  "name" map-get static-assert
  drop
end on-event word.compile.begin

word print_debug_payload
  event-payload repr puts
end on-event debug.payload

word emit_debug_payload
  "debug.payload" event-new
  "answer" 42 event-payload-set
  "source" "compile-time" event-payload-set
  ct-event-emit-object drop
end
compile-time emit_debug_payload

word main
  0
end
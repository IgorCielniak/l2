import stdlib.sl

word check_compile_event
  dup event-name "compile" string-equal? static-assert
  event-name string-length 0 > static-assert
end on-event compile

word consume_custom_event
  dup event-name "custom" string-equal? static-assert
  dup event-timestamp static-assert 0 > static-assert
  dup "answer" event-payload-get static-assert
  drop
  event-payload dup repr puts
  "answer" map-get static-assert
  drop
end on-event custom

word make_custom_event
  "custom" event-new
  "answer" 42 event-payload-set
  ct-event-emit-object drop
end
compile-time make_custom_event

word target
  1
end

word main
  "event-objects-ok" puts
end

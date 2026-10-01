import stdlib.sl

word check_compile_event
  get-event-type "compile" string= static_assert
  get-event-name string-length 0 > static_assert
end on-event compile

word consume_custom_event
  dup get-event-type "custom" string= static_assert
  dup get-event-name "custom" string= static_assert
  dup get-event-timestamp 0 > static_assert
  dup "answer" event-object-prop-get static_assert
  drop
  drop
  get-event-payload dup ct-repr puts
  "answer" map-get static_assert
  drop
  drop
end on-event custom

word make_custom_event
  "custom" event-object-create
  "answer" 42 event-object-prop-append
  event-object-emit
end
compile-time make_custom_event

word target
  1
end

word main
  "event-objects-ok" puts
end

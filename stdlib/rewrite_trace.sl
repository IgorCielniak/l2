# Rewrite tracing: report every applied reader or grammar rewrite as a
# compiler note.
#
#   import rewrite_trace.sl
#   rewrite-trace-on
#   ...                    # each rewrite applied here is reported
#   rewrite-trace-off
#
# A note reads:  rewrite grammar 'rule' at file:line:column: before -> after
# (--macro-preview prints the same applications with source context).

import ct_store.sl

# [* | event] -> [*]
word __rewrite-trace-note
	"rewrite-trace.on" 0 ct-store-get-or 0 == if drop exit end
	event-payload
	"rewrite " over "stage" map-get drop string-append
	" '" string-append over "rule" map-get drop string-append "'" string-append
	over "source-origin" map-get if    # payload message origin
		swap " at " string-append over source-origin-path string-append
		":" string-append over source-origin-start-line int>string string-append
		":" string-append swap source-origin-start-column int>string string-append
	else
		drop
	end
	": " string-append over "before" map-get drop " " string-join string-append
	" -> " string-append swap "after" map-get drop " " string-join string-append
	ct-diagnostic-note
end
compile-only

# Start reporting rewrites.  The subscription is made once; afterwards the
# handler checks the on/off flag (the store cannot hold subscription handles).
word rewrite-trace-on
	"rewrite-trace.subscribed" ct-store-has? 0 == if
		"rewrite.applied" "__rewrite-trace-note" nil ct-event-subscribe drop
		"rewrite-trace.subscribed" 1 ct-store-set
	end
	"rewrite-trace.on" 1 ct-store-set
end
immediate
compile-only

# Stop reporting rewrites.
word rewrite-trace-off
	"rewrite-trace.on" 0 ct-store-set
end
immediate
compile-only

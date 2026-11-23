import stdlib.sl
import meta.sl

word verify_ct_word_inventory_coverage
  "CT" ct-definition-exists? static-assert
  "add-token" ct-definition-exists? 0 == static-assert
  "add-token-chars" ct-definition-exists? 0 == static-assert
  "ct-tokenizer-get-config" ct-definition-exists? static-assert
  "ct-tokenizer-set-config" ct-definition-exists? static-assert
  "ct-tokenizer-run" ct-definition-exists? static-assert
  "source-cursor-new" ct-definition-exists? static-assert
  "source-cursor-peek" ct-definition-exists? static-assert
  "source-cursor-read" ct-definition-exists? static-assert
  "source-cursor-eof?" ct-definition-exists? static-assert
  "source-cursor-pos" ct-definition-exists? static-assert
  "source-cursor-set-pos" ct-definition-exists? static-assert
  "source-cursor-slice" ct-definition-exists? static-assert
  "source-cursor-span" ct-definition-exists? static-assert
  "diagnostic-new" ct-definition-exists? static-assert
  "diagnostic-severity" ct-definition-exists? static-assert
  "diagnostic-code" ct-definition-exists? static-assert
  "diagnostic-category" ct-definition-exists? static-assert
  "diagnostic-phase" ct-definition-exists? static-assert
  "diagnostic-message" ct-definition-exists? static-assert
  "diagnostic-primary" ct-definition-exists? static-assert
  "diagnostic-related" ct-definition-exists? static-assert
  "ct-diagnostic-raise" ct-definition-exists? static-assert
  "ct-diagnostic-list" ct-definition-exists? static-assert
  "ct-diagnostic-error" ct-definition-exists? static-assert
  "ct-diagnostic-error-at" ct-definition-exists? static-assert
  "ct-parser-error-at" ct-definition-exists? static-assert
  "ct-diagnostic-warning" ct-definition-exists? static-assert
  "ct-diagnostic-note" ct-definition-exists? static-assert
  "ct-event-subscribe" ct-definition-exists? static-assert
  "ct-event-unsubscribe" ct-definition-exists? static-assert
  "ct-event-emit" ct-definition-exists? static-assert
  "ct-event-emit-object" ct-definition-exists? static-assert
  "event-new" ct-definition-exists? static-assert
  "event-payload-set" ct-definition-exists? static-assert
  "event-payload-get" ct-definition-exists? static-assert
  "event-name" ct-definition-exists? static-assert
  "event-timestamp" ct-definition-exists? static-assert
  "event-payload" ct-definition-exists? static-assert
  "ct-event-enabled?" ct-definition-exists? static-assert
  "ct-event-set-enabled" ct-definition-exists? static-assert
  "ct-recovery-list-restore-points" ct-definition-exists? static-assert
  "ct-recovery-get-restore-point" ct-definition-exists? static-assert
  "ct-recovery-jump" ct-definition-exists? static-assert
  "ct-recovery-resume" ct-definition-exists? static-assert
  "ct-recovery-get-retry-limit" ct-definition-exists? static-assert
  "ct-recovery-set-retry-limit" ct-definition-exists? static-assert
  "event-subscribe" ct-definition-exists? 0 == static-assert
  "event-unsubscribe" ct-definition-exists? 0 == static-assert
  "event-emit" ct-definition-exists? 0 == static-assert
  "event-object-create" ct-definition-exists? 0 == static-assert
  "event-object-prop-append" ct-definition-exists? 0 == static-assert
  "event-object-prop-get" ct-definition-exists? 0 == static-assert
  "event-object-emit" ct-definition-exists? 0 == static-assert
  "get-event-type" ct-definition-exists? 0 == static-assert
  "get-event-name" ct-definition-exists? 0 == static-assert
  "get-event-timestamp" ct-definition-exists? 0 == static-assert
  "get-event-payload" ct-definition-exists? 0 == static-assert
  "bss-append" ct-definition-exists? 0 == static-assert
  "bss-clear" ct-definition-exists? 0 == static-assert
  "bss-set" ct-definition-exists? 0 == static-assert
  "clear-token-hook" ct-definition-exists? 0 == static-assert
  "ct-add-grammar-rewrite" ct-definition-exists? 0 == static-assert
  "ct-add-grammar-rewrite-named" ct-definition-exists? 0 == static-assert
  "ct-add-grammar-rewrite-priority" ct-definition-exists? 0 == static-assert
  "ct-add-reader-rewrite" ct-definition-exists? 0 == static-assert
  "ct-add-reader-rewrite-named" ct-definition-exists? 0 == static-assert
  "ct-add-reader-rewrite-priority" ct-definition-exists? 0 == static-assert
  "ct-capture-args" ct-definition-exists? 0 == static-assert
  "ct-capture-assert-shape" ct-definition-exists? 0 == static-assert
  "ct-capture-clone" ct-definition-exists? 0 == static-assert
  "ct-capture-coerce-number" ct-definition-exists? 0 == static-assert
  "ct-capture-coerce-string" ct-definition-exists? 0 == static-assert
  "ct-capture-coerce-tokens" ct-definition-exists? 0 == static-assert
  "ct-capture-compress" ct-definition-exists? 0 == static-assert
  "ct-capture-count" ct-definition-exists? 0 == static-assert
  "ct-capture-decompress" ct-definition-exists? 0 == static-assert
  "ct-capture-deserialize" ct-definition-exists? 0 == static-assert
  "ct-capture-diff" ct-definition-exists? 0 == static-assert
  "ct-capture-equal?" ct-definition-exists? 0 == static-assert
  "ct-capture-filter" ct-definition-exists? 0 == static-assert
  "ct-capture-freeze" ct-definition-exists? 0 == static-assert
  "ct-capture-get" ct-definition-exists? 0 == static-assert
  "ct-capture-global-clear" ct-definition-exists? 0 == static-assert
  "ct-capture-global-delete" ct-definition-exists? 0 == static-assert
  "ct-capture-global-get" ct-definition-exists? 0 == static-assert
  "ct-capture-global-set" ct-definition-exists? 0 == static-assert
  "ct-capture-globals" ct-definition-exists? 0 == static-assert
  "ct-capture-has?" ct-definition-exists? 0 == static-assert
  "ct-capture-hash" ct-definition-exists? 0 == static-assert
  "ct-capture-join" ct-definition-exists? 0 == static-assert
  "ct-capture-lifetime" ct-definition-exists? 0 == static-assert
  "ct-capture-lifetime-assert" ct-definition-exists? 0 == static-assert
  "ct-capture-lifetime-live?" ct-definition-exists? 0 == static-assert
  "ct-capture-lint" ct-definition-exists? 0 == static-assert
  "ct-capture-locals" ct-definition-exists? 0 == static-assert
  "ct-capture-map" ct-definition-exists? 0 == static-assert
  "ct-capture-mutable?" ct-definition-exists? 0 == static-assert
  "ct-capture-normalize" ct-definition-exists? 0 == static-assert
  "ct-capture-origin" ct-definition-exists? 0 == static-assert
  "ct-capture-pretty" ct-definition-exists? 0 == static-assert
  "repr" ct-definition-exists? static-assert
  "ct-capture-replay-clear" ct-definition-exists? 0 == static-assert
  "ct-capture-replay-log" ct-definition-exists? 0 == static-assert
  "ct-capture-schema-get" ct-definition-exists? 0 == static-assert
  "ct-capture-schema-put" ct-definition-exists? 0 == static-assert
  "ct-capture-schema-validate" ct-definition-exists? 0 == static-assert
  "ct-capture-separate" ct-definition-exists? 0 == static-assert
  "ct-capture-serialize" ct-definition-exists? 0 == static-assert
  "ct-capture-shape" ct-definition-exists? 0 == static-assert
  "ct-capture-slice" ct-definition-exists? 0 == static-assert
  "ct-capture-taint-get" ct-definition-exists? 0 == static-assert
  "ct-capture-taint-set" ct-definition-exists? 0 == static-assert
  "ct-capture-tainted?" ct-definition-exists? 0 == static-assert
  "ct-capture-thaw" ct-definition-exists? 0 == static-assert
  "ct-clear-ct-call-memo" ct-definition-exists? 0 == static-assert
  "ct-clear-ct-call-side-effect-log" ct-definition-exists? 0 == static-assert
  "ct-clear-grammar-rewrites" ct-definition-exists? 0 == static-assert
  "ct-clear-reader-rewrites" ct-definition-exists? 0 == static-assert
  "ct-clear-rewrite-loop-reports" ct-definition-exists? 0 == static-assert
  "ct-clear-rewrite-profile" ct-definition-exists? 0 == static-assert
  "ct-clear-rewrite-trace-log" ct-definition-exists? 0 == static-assert
  "ct-clone-macro" ct-definition-exists? 0 == static-assert
  "ct-control-add-close-op" ct-definition-exists? static-assert
  "ct-control-depth" ct-definition-exists? static-assert
  "ct-control-frame-new" ct-definition-exists? static-assert
  "ct-control-get" ct-definition-exists? static-assert
  "ct-control-get?" ct-definition-exists? static-assert
  "token-slice" ct-definition-exists? static-assert
  "lexeme" ct-definition-exists? static-assert
  "token?" ct-definition-exists? static-assert
  "ct-definition-set-effect" ct-definition-exists? static-assert
  "ct-definition-effect" ct-definition-exists? static-assert
  "ct-control-peek" ct-definition-exists? static-assert
  "ct-control-pop" ct-definition-exists? static-assert
  "ct-control-push" ct-definition-exists? static-assert
  "ct-control-set" ct-definition-exists? static-assert
  "ct-ctrand-int" ct-definition-exists? 0 == static-assert
  "ct-ctrand-range" ct-definition-exists? 0 == static-assert
  "ct-ctrand-seed" ct-definition-exists? 0 == static-assert
  "ct-parser-current" ct-definition-exists? static-assert
  "ct-detect-pattern-conflicts" ct-definition-exists? 0 == static-assert
  "ct-detect-pattern-conflicts-named" ct-definition-exists? 0 == static-assert
  "ct-emit-op" ct-definition-exists? 0 == static-assert
  "ct-export-rewrite-pack" ct-definition-exists? 0 == static-assert
  "gensym" ct-definition-exists? static-assert
  "ct-get-ct-call-contract" ct-definition-exists? 0 == static-assert
  "ct-get-ct-call-exception-policy" ct-definition-exists? 0 == static-assert
  "ct-get-ct-call-memo" ct-definition-exists? 0 == static-assert
  "ct-get-ct-call-memo-size" ct-definition-exists? 0 == static-assert
  "ct-get-ct-call-recursion-limit" ct-definition-exists? 0 == static-assert
  "ct-get-ct-call-sandbox-allowlist" ct-definition-exists? 0 == static-assert
  "ct-get-ct-call-sandbox-mode" ct-definition-exists? 0 == static-assert
  "ct-get-ct-call-side-effect-log" ct-definition-exists? 0 == static-assert
  "ct-get-ct-call-side-effects" ct-definition-exists? 0 == static-assert
  "ct-get-ct-call-timeout-ms" ct-definition-exists? 0 == static-assert
  "ct-get-grammar-rewrite-enabled" ct-definition-exists? 0 == static-assert
  "ct-get-grammar-rewrite-priority" ct-definition-exists? 0 == static-assert
  "ct-get-macro-expansion" ct-definition-exists? 0 == static-assert
  "ct-expansion-get-depth-limit" ct-definition-exists? static-assert
  "ct-get-macro-preview" ct-definition-exists? 0 == static-assert
  "ct-get-macro-signature" ct-definition-exists? 0 == static-assert
  "ct-get-macro-template-mode" ct-definition-exists? 0 == static-assert
  "ct-get-macro-template-program-size" ct-definition-exists? 0 == static-assert
  "ct-get-macro-template-version" ct-definition-exists? 0 == static-assert
  "ct-get-pattern-macro-clause-details" ct-definition-exists? 0 == static-assert
  "ct-get-pattern-macro-clauses" ct-definition-exists? 0 == static-assert
  "ct-get-pattern-macro-enabled" ct-definition-exists? 0 == static-assert
  "ct-get-pattern-macro-group" ct-definition-exists? 0 == static-assert
  "ct-get-pattern-macro-priority" ct-definition-exists? 0 == static-assert
  "ct-get-pattern-macro-scope" ct-definition-exists? 0 == static-assert
  "ct-get-reader-rewrite-enabled" ct-definition-exists? 0 == static-assert
  "ct-get-reader-rewrite-priority" ct-definition-exists? 0 == static-assert
  "ct-get-rewrite-index-stats" ct-definition-exists? 0 == static-assert
  "ct-get-rewrite-loop-detection" ct-definition-exists? 0 == static-assert
  "ct-get-rewrite-loop-reports" ct-definition-exists? 0 == static-assert
  "ct-get-rewrite-max-steps" ct-definition-exists? 0 == static-assert
  "ct-get-rewrite-pipeline" ct-definition-exists? 0 == static-assert
  "ct-get-rewrite-profile" ct-definition-exists? 0 == static-assert
  "ct-get-rewrite-provenance" ct-definition-exists? 0 == static-assert
  "ct-get-rewrite-saturation" ct-definition-exists? 0 == static-assert
  "ct-get-rewrite-specificity" ct-definition-exists? 0 == static-assert
  "ct-get-rewrite-trace" ct-definition-exists? 0 == static-assert
  "ct-get-rewrite-trace-log" ct-definition-exists? 0 == static-assert
  "ct-definition-asm-by-name" ct-definition-exists? static-assert
  "ct-definition-body-by-name" ct-definition-exists? static-assert
  "ct-import-rewrite-pack" ct-definition-exists? 0 == static-assert
  "ct-import-rewrite-pack-replace" ct-definition-exists? 0 == static-assert
  "ct-last-token-line" ct-definition-exists? 0 == static-assert
  "ct-list-active-pattern-groups" ct-definition-exists? 0 == static-assert
  "ct-list-active-pattern-scopes" ct-definition-exists? 0 == static-assert
  "ct-list-grammar-rewrites" ct-definition-exists? 0 == static-assert
  "ct-list-pattern-macros" ct-definition-exists? 0 == static-assert
  "ct-list-reader-rewrites" ct-definition-exists? 0 == static-assert
  "ct-list-rewrite-active-pipelines" ct-definition-exists? 0 == static-assert
  "ct-definition-list-names" ct-definition-exists? static-assert
  "ct-definition-list-names-prefix" ct-definition-exists? static-assert
  "ct-macro-attrs-get" ct-definition-exists? 0 == static-assert
  "ct-macro-attrs-set" ct-definition-exists? 0 == static-assert
  "ct-macro-doc-get" ct-definition-exists? 0 == static-assert
  "ct-macro-doc-set" ct-definition-exists? 0 == static-assert
  "ct-new-label" ct-definition-exists? 0 == static-assert
  "ct-parser-get-pos" ct-definition-exists? static-assert
  "ct-parser-remaining" ct-definition-exists? static-assert
  "ct-definition-predeclare-all" ct-definition-exists? static-assert
  "ct-rebuild-rewrite-index" ct-definition-exists? 0 == static-assert
  "ct-register-block-opener" ct-definition-exists? 0 == static-assert
  "ct-register-control-override" ct-definition-exists? 0 == static-assert
  "ct-register-pattern-macro" ct-definition-exists? 0 == static-assert
  "ct-register-text-macro" ct-definition-exists? 0 == static-assert
  "ct-register-text-macro-signature" ct-definition-exists? 0 == static-assert
  "ct-remove-grammar-rewrite" ct-definition-exists? 0 == static-assert
  "ct-remove-reader-rewrite" ct-definition-exists? 0 == static-assert
  "ct-rename-macro" ct-definition-exists? 0 == static-assert
  "ct-rewrite-compatibility-matrix" ct-definition-exists? 0 == static-assert
  "ct-rewrite-dry-run" ct-definition-exists? static-assert
  "ct-rewrite-generate-fixture" ct-definition-exists? 0 == static-assert
  "ct-rewrite-txn-begin" ct-definition-exists? 0 == static-assert
  "ct-rewrite-txn-commit" ct-definition-exists? 0 == static-assert
  "ct-rewrite-txn-rollback" ct-definition-exists? 0 == static-assert
  "ct-set-ct-call-contract" ct-definition-exists? 0 == static-assert
  "ct-set-ct-call-exception-policy" ct-definition-exists? 0 == static-assert
  "ct-set-ct-call-memo" ct-definition-exists? 0 == static-assert
  "ct-set-ct-call-recursion-limit" ct-definition-exists? 0 == static-assert
  "ct-set-ct-call-sandbox-allowlist" ct-definition-exists? 0 == static-assert
  "ct-set-ct-call-sandbox-mode" ct-definition-exists? 0 == static-assert
  "ct-set-ct-call-side-effects" ct-definition-exists? 0 == static-assert
  "ct-set-ct-call-timeout-ms" ct-definition-exists? 0 == static-assert
  "ct-set-grammar-rewrite-enabled" ct-definition-exists? 0 == static-assert
  "ct-set-grammar-rewrite-priority" ct-definition-exists? 0 == static-assert
  "ct-set-macro-expansion" ct-definition-exists? 0 == static-assert
  "ct-expansion-set-depth-limit" ct-definition-exists? static-assert
  "ct-set-macro-preview" ct-definition-exists? 0 == static-assert
  "ct-set-pattern-group-active" ct-definition-exists? 0 == static-assert
  "ct-set-pattern-macro-clause-guard" ct-definition-exists? 0 == static-assert
  "ct-set-pattern-macro-enabled" ct-definition-exists? 0 == static-assert
  "ct-set-pattern-macro-group" ct-definition-exists? 0 == static-assert
  "ct-set-pattern-macro-priority" ct-definition-exists? 0 == static-assert
  "ct-set-pattern-macro-scope" ct-definition-exists? 0 == static-assert
  "ct-set-pattern-scope-active" ct-definition-exists? 0 == static-assert
  "ct-set-reader-rewrite-enabled" ct-definition-exists? 0 == static-assert
  "ct-set-reader-rewrite-priority" ct-definition-exists? 0 == static-assert
  "ct-set-rewrite-loop-detection" ct-definition-exists? 0 == static-assert
  "ct-set-rewrite-max-steps" ct-definition-exists? 0 == static-assert
  "ct-set-rewrite-pipeline" ct-definition-exists? 0 == static-assert
  "ct-set-rewrite-pipeline-active" ct-definition-exists? 0 == static-assert
  "ct-set-rewrite-saturation" ct-definition-exists? 0 == static-assert
  "ct-set-rewrite-trace" ct-definition-exists? 0 == static-assert
  "ct-unregister-block-opener" ct-definition-exists? 0 == static-assert
  "ct-unregister-control-override" ct-definition-exists? 0 == static-assert
  "ct-unregister-pattern-macro" ct-definition-exists? 0 == static-assert
  "ct-definition-unregister-by-name" ct-definition-exists? static-assert
  "ct-definition-exists?" ct-definition-exists? static-assert
  "ct-word-is-pattern-macro" ct-definition-exists? 0 == static-assert
  "ct-word-is-text-macro" ct-definition-exists? 0 == static-assert
  "emit-definition" ct-definition-exists? 0 == static-assert
  "i" ct-definition-exists? static-assert
  "identifier?" ct-definition-exists? static-assert
  "ct-parser-inject" ct-definition-exists? static-assert
  "int>string" ct-definition-exists? static-assert
  "lexer-collect-brace" ct-definition-exists? 0 == static-assert
  "lexer-expect" ct-definition-exists? 0 == static-assert
  "lexer-new" ct-definition-exists? 0 == static-assert
  "lexer-peek" ct-definition-exists? 0 == static-assert
  "lexer-pop" ct-definition-exists? 0 == static-assert
  "lexer-push-back" ct-definition-exists? 0 == static-assert
  "list-append" ct-definition-exists? static-assert
  "list-clear" ct-definition-exists? static-assert
  "list-clone" ct-definition-exists? static-assert
  "list-deep-clone" ct-definition-exists? static-assert
  "list-contains?" ct-definition-exists? static-assert
  "list-empty?" ct-definition-exists? static-assert
  "list-extend" ct-definition-exists? static-assert
  "list-find" ct-definition-exists? static-assert
  "list-get" ct-definition-exists? static-assert
  "list-insert" ct-definition-exists? static-assert
  "list-join" ct-definition-exists? 0 == static-assert
  "list-last" ct-definition-exists? static-assert
  "list-length" ct-definition-exists? static-assert
  "list-new" ct-definition-exists? static-assert
  "list-peek-front" ct-definition-exists? static-assert
  "list-pop" ct-definition-exists? static-assert
  "list-pop-front" ct-definition-exists? static-assert
  "list-push-front" ct-definition-exists? static-assert
  "list-remove" ct-definition-exists? static-assert
  "list-reverse" ct-definition-exists? static-assert
  "list-set" ct-definition-exists? static-assert
  "list-slice" ct-definition-exists? static-assert
  "map-clear" ct-definition-exists? static-assert
  "map-clone" ct-definition-exists? static-assert
  "map-delete" ct-definition-exists? static-assert
  "map-empty?" ct-definition-exists? static-assert
  "map-get" ct-definition-exists? static-assert
  "map-has?" ct-definition-exists? static-assert
  "map-keys" ct-definition-exists? static-assert
  "map-length" ct-definition-exists? static-assert
  "map-new" ct-definition-exists? static-assert
  "map-set" ct-definition-exists? static-assert
  "map-merge" ct-definition-exists? static-assert
  "map-values" ct-definition-exists? static-assert
  "ct-parser-consume" ct-definition-exists? static-assert
  "nil" ct-definition-exists? static-assert
  "nil?" ct-definition-exists? static-assert
  "ct-parser-error" ct-definition-exists? static-assert
  "parse-error" ct-definition-exists? 0 == static-assert
  "peek-token" ct-definition-exists? 0 == static-assert
  "prelude-append" ct-definition-exists? 0 == static-assert
  "prelude-clear" ct-definition-exists? 0 == static-assert
  "prelude-set" ct-definition-exists? 0 == static-assert
  "set-token-hook" ct-definition-exists? 0 == static-assert
  "shunt" ct-definition-exists? 0 == static-assert
  "static-assert" ct-definition-exists? static-assert
  "string-append" ct-definition-exists? static-assert
  "string-contains?" ct-definition-exists? static-assert
  "string-ends-with?" ct-definition-exists? static-assert
  "string-join" ct-definition-exists? static-assert
  "string-length" ct-definition-exists? static-assert
  "string-lower" ct-definition-exists? static-assert
  "string-replace" ct-definition-exists? static-assert
  "string-split" ct-definition-exists? static-assert
  "string-starts-with?" ct-definition-exists? static-assert
  "string-strip" ct-definition-exists? static-assert
  "string-upper" ct-definition-exists? static-assert
  "string-equal?" ct-definition-exists? static-assert
  "try-string>int" ct-definition-exists? static-assert
  "token-column" ct-definition-exists? static-assert
  "token-from-lexeme" ct-definition-exists? 0 == static-assert
  "token-clone" ct-definition-exists? 0 == static-assert
  "inject-lexemes" ct-definition-exists? 0 == static-assert
  "ct-parser-mark" ct-definition-exists? 0 == static-assert
  "ct-parser-diff" ct-definition-exists? 0 == static-assert
  "next-token" ct-definition-exists? 0 == static-assert
  "inject-tokens" ct-definition-exists? 0 == static-assert
  "ct-current-token" ct-definition-exists? 0 == static-assert
  "ct-parser-pos" ct-definition-exists? 0 == static-assert
  "ct-parser-slice" ct-definition-exists? static-assert
  "ct-parser-checkpoint" ct-definition-exists? static-assert
  "ct-parser-restore" ct-definition-exists? static-assert
  "ct-parser-session-begin" ct-definition-exists? static-assert
  "ct-parser-session-commit" ct-definition-exists? static-assert
  "ct-parser-session-rollback" ct-definition-exists? static-assert
  "ct-parser-collect-until" ct-definition-exists? static-assert
  "ct-parser-collect-balanced" ct-definition-exists? static-assert
  "ct-parser-expected" ct-definition-exists? static-assert
  "ct-parser-tail" ct-definition-exists? static-assert
  "ct-parser-peek" ct-definition-exists? static-assert
  "ct-parser-eof?" ct-definition-exists? static-assert
  "ct-parser-set-pos" ct-definition-exists? static-assert
  "token-shift-column" ct-definition-exists? 0 == static-assert
  "ct-get-word-asm" ct-definition-exists? 0 == static-assert
  "ct-get-word-body" ct-definition-exists? 0 == static-assert
  "ct-list-words" ct-definition-exists? 0 == static-assert
  "ct-list-words-prefix" ct-definition-exists? 0 == static-assert
  "ct-predef-all" ct-definition-exists? 0 == static-assert
  "ct-unregister-word" ct-definition-exists? 0 == static-assert
  "ct-word-exists?" ct-definition-exists? 0 == static-assert
  "use-l2-ct" ct-definition-exists? 0 == static-assert
  "ct-error-define" ct-definition-exists? 0 == static-assert
  "ct-gensym" ct-definition-exists? 0 == static-assert
  "ct-get-macro-expansion-limit" ct-definition-exists? 0 == static-assert
  "ct-set-macro-expansion-limit" ct-definition-exists? 0 == static-assert
  "syntax-length" ct-definition-exists? static-assert
  "syntax-get" ct-definition-exists? static-assert
  "syntax-slice" ct-definition-exists? static-assert
  "syntax-concat" ct-definition-exists? static-assert
  "syntax>list" ct-definition-exists? static-assert
  "list>syntax" ct-definition-exists? static-assert
  "emit" ct-definition-exists? static-assert
  "ct-macro-get" ct-definition-exists? static-assert
  "ct-macro-get-by-name" ct-definition-exists? static-assert
  "ct-macro-set" ct-definition-exists? static-assert
  "ct-macro-set-by-name" ct-definition-exists? static-assert
  "ct-macro-context" ct-definition-exists? static-assert
  "ct-definition-set-body" ct-definition-exists? static-assert
  "ct-definition-set-body-by-name" ct-definition-exists? static-assert
  "ct-lang-register-macro" ct-definition-exists? static-assert
  "token-from-origin" ct-definition-exists? static-assert
  "token-origin" ct-definition-exists? static-assert
  "token-expansion-chain" ct-definition-exists? static-assert
  "source-origin-path" ct-definition-exists? static-assert
  "expansion-frame-kind" ct-definition-exists? static-assert
  "token-lexeme" ct-definition-exists? static-assert
  "token-line" ct-definition-exists? static-assert
  "ct-definition-prefer-l2-by-name" ct-definition-exists? static-assert
  "ct-rewrite-add-reader" ct-definition-exists? static-assert
  "ct-rewrite-add-reader-named" ct-definition-exists? static-assert
  "ct-rewrite-add-grammar" ct-definition-exists? static-assert
  "ct-rewrite-add-grammar-named" ct-definition-exists? static-assert
  "ct-rewrite-remove" ct-definition-exists? static-assert
  "ct-rewrite-clear-reader" ct-definition-exists? static-assert
  "ct-rewrite-clear-grammar" ct-definition-exists? static-assert
  "ct-rewrite-list-reader" ct-definition-exists? static-assert
  "ct-rewrite-list-grammar" ct-definition-exists? static-assert
  "ct-rewrite-set-loop-detection" ct-definition-exists? static-assert
  "ct-rewrite-loop-detection?" ct-definition-exists? static-assert
  "ct-rewrite-get-loop-reports" ct-definition-exists? static-assert
  "ct-rewrite-clear-loop-reports" ct-definition-exists? static-assert
  "ct-rewrite-set-max-steps" ct-definition-exists? static-assert
  "ct-rewrite-get-max-steps" ct-definition-exists? static-assert
  "ct-rewrite-set-pipeline-active" ct-definition-exists? static-assert
  "ct-rewrite-list-active-pipelines" ct-definition-exists? static-assert
  "ct-rewrite-scope-push" ct-definition-exists? static-assert
  "ct-rewrite-scope-pop" ct-definition-exists? static-assert
  "ct-rewrite-run-on-list" ct-definition-exists? static-assert
  "ct-rewrite-dry-run" ct-definition-exists? static-assert
  "ct-rewrite-rule-set-priority" ct-definition-exists? static-assert
  "ct-rewrite-rule-get-priority" ct-definition-exists? static-assert
  "ct-rewrite-rule-set-enabled" ct-definition-exists? static-assert
  "ct-rewrite-rule-get-enabled" ct-definition-exists? static-assert
  "ct-rewrite-rule-set-pipeline" ct-definition-exists? static-assert
  "ct-rewrite-rule-get-pipeline" ct-definition-exists? static-assert
  "ct-rewrite-rule-set-guard" ct-definition-exists? static-assert
  "ct-rewrite-rule-get-guard" ct-definition-exists? static-assert
  "ct-parser-set-token-hook" ct-definition-exists? static-assert
  "ct-parser-clear-token-hook" ct-definition-exists? static-assert
  "ct-lang-list-active" ct-definition-exists? static-assert
  "ct-lang-get-meta" ct-definition-exists? static-assert
  "ct-lang-set-meta" ct-definition-exists? static-assert
  "ct-lang-add-token-hook" ct-definition-exists? static-assert
  "ct-lang-set-token-hook-priority" ct-definition-exists? static-assert
  "ct-lang-set-token-hook-enabled" ct-definition-exists? static-assert
  "ct-lang-remove-token-hook" ct-definition-exists? static-assert
  "ct-lang-register-word" ct-definition-exists? static-assert
  "ct-lang-set-resource-priority" ct-definition-exists? static-assert
  "ct-lang-get-resource-priority" ct-definition-exists? static-assert
  "ct-lang-unregister-resource" ct-definition-exists? static-assert
  "ct-output-append-text-preamble" ct-definition-exists? static-assert
  "ct-output-set-text-preamble" ct-definition-exists? static-assert
  "ct-output-clear-text-preamble" ct-definition-exists? static-assert
  "ct-output-append-data" ct-definition-exists? static-assert
  "ct-output-set-data" ct-definition-exists? static-assert
  "ct-output-clear-data" ct-definition-exists? static-assert
  "ct-output-append-bss" ct-definition-exists? static-assert
  "ct-output-set-bss" ct-definition-exists? static-assert
  "ct-output-clear-bss" ct-definition-exists? static-assert
  "ct-rng-seed" ct-definition-exists? static-assert
  "ct-rng-int" ct-definition-exists? static-assert
  "ct-rng-range" ct-definition-exists? static-assert
  "ct-op-emit" ct-definition-exists? static-assert
  "ct-label-new" ct-definition-exists? static-assert
  "ct-parser-last-line" ct-definition-exists? static-assert
  "ct-parser-register-block-opener" ct-definition-exists? static-assert
  "ct-parser-unregister-block-opener" ct-definition-exists? static-assert
  "ct-control-register-override" ct-definition-exists? static-assert
  "ct-control-unregister-override" ct-definition-exists? static-assert
  "repr" ct-definition-exists? static-assert
  "static-assert" ct-definition-exists? static-assert
  "string-utf8-length" ct-definition-exists? static-assert
  "static_assert" ct-definition-exists? 0 == static-assert
  "ct-repr" ct-definition-exists? 0 == static-assert
  "ct-event-stream-enabled?" ct-definition-exists? static-assert
  "ct-event-stream-set-enabled" ct-definition-exists? static-assert
end
compile-time verify_ct_word_inventory_coverage

word main
  1 puti cr
end

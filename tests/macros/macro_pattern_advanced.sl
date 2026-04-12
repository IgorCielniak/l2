import stdlib.sl
import meta.sl

word guard_nonzero
  "captures" map-get static_assert
  "x" map-get static_assert
  "0" string= not
end
use-l2-ct
compile-only

word setup_part_c_advanced
  # Guarded pattern macro clause.
  "pc_guarded"
  list-new
  list-new "$x:int" list-append "+" list-append "0" list-append
  list-new "$x" list-append
  meta-macro-clause
  "guard_nonzero" list-append
  list-append
  ct-register-pattern-macro

  "pc_guarded" "arith" ct-set-pattern-macro-group static_assert
  "pc_guarded" "scope_a" ct-set-pattern-macro-scope static_assert
  "pc_guarded" ct-get-pattern-macro-group static_assert "arith" string= static_assert
  "pc_guarded" ct-get-pattern-macro-scope static_assert "scope_a" string= static_assert

  "arith" 1 ct-set-pattern-group-active static_assert
  "scope_a" 1 ct-set-pattern-scope-active static_assert
  ct-list-active-pattern-groups "arith" list-contains? static_assert
  ct-list-active-pattern-scopes "scope_a" list-contains? static_assert

  "pc_guarded" ct-get-pattern-macro-clause-details
  static_assert
  dup list-length 1 == static_assert
  drop

  # Clause guard can be changed dynamically.
  "pc_guarded" 0 nil ct-set-pattern-macro-clause-guard static_assert
  "pc_guarded" 0 "guard_nonzero" ct-set-pattern-macro-clause-guard static_assert

  # Pipeline controls.
  "grammar" "pattern-macro:pc_guarded:0" "pipe_a" ct-set-rewrite-pipeline static_assert
  "grammar" "pipe_a" 1 ct-set-rewrite-pipeline-active
  "grammar" "pattern-macro:pc_guarded:0" ct-get-rewrite-pipeline static_assert "pipe_a" string= static_assert
  "grammar" ct-list-rewrite-active-pipelines "pipe_a" list-contains? static_assert

  # Index stats.
  "grammar" ct-rebuild-rewrite-index drop
  "grammar" ct-get-rewrite-index-stats
  dup "stage" map-get static_assert
  swap drop
  "grammar" string= static_assert

  # Conflict detector target.
  "pc_conflict"
  list-new
  list-new "$x:int" list-append
  list-new "1" list-append
  meta-macro-clauses-append
  list-new "$x:int" list-append
  list-new "2" list-append
  meta-macro-clauses-append
  ct-register-pattern-macro

  "pc_conflict" ct-detect-pattern-conflicts-named
  dup list-length 1 >= static_assert
  drop

  "grammar" "pattern-macro:pc_guarded:0" ct-get-rewrite-specificity
  static_assert
  dup 1 >= static_assert
  drop

  "grammar" "pattern-macro:pc_guarded:0" ct-get-rewrite-provenance
  static_assert
  dup "stage" map-get static_assert
  swap drop
  "grammar" string= static_assert

  # Optional/negative/repetition operators in rewrites.
  "rw_opt"
  list-new "rw_opt" list-append "x?" list-append
  list-new "123" list-append
  ct-add-grammar-rewrite-named drop

  "rw_neg"
  list-new "rw_neg" list-append "!0" list-append "end" list-append
  list-new "321" list-append
  ct-add-grammar-rewrite-named drop

  "rw_rep"
  list-new "rw_rep" list-append "$x:int+" list-append "end" list-append
  list-new "$*x" list-append
  ct-add-grammar-rewrite-named drop

  # Dry-run + patch output.
  "grammar"
  list-new "pc_guarded" list-append "5" list-append "+" list-append "0" list-append
  32
  ct-rewrite-dry-run
  dup list-length 1 >= static_assert
  drop
  dup list-length 1 == static_assert
  dup 0 list-get "5" string= static_assert
  drop

  "grammar"
  list-new "rw_opt" list-append
  16
  ct-rewrite-dry-run
  drop
  dup list-length 1 == static_assert
  dup 0 list-get "123" string= static_assert
  drop

  "grammar"
  list-new "rw_opt" list-append "x" list-append
  16
  ct-rewrite-dry-run
  drop
  dup list-length 1 == static_assert
  dup 0 list-get "123" string= static_assert
  drop

  "grammar"
  list-new "rw_neg" list-append "7" list-append "end" list-append
  16
  ct-rewrite-dry-run
  drop
  dup list-length 1 == static_assert
  dup 0 list-get "321" string= static_assert
  drop

  "grammar"
  list-new "rw_neg" list-append "0" list-append "end" list-append
  16
  ct-rewrite-dry-run
  drop
  dup list-length 3 == static_assert
  drop

  "grammar"
  list-new "rw_rep" list-append "1" list-append "2" list-append "3" list-append "end" list-append
  16
  ct-rewrite-dry-run
  drop
  dup list-length 3 == static_assert
  dup 0 list-get "1" string= static_assert
  dup 1 list-get "2" string= static_assert
  dup 2 list-get "3" string= static_assert
  drop

  # Fixture generation helper.
  "grammar"
  list-new "rw_opt" list-append
  16
  ct-rewrite-generate-fixture
  dup "output" map-get static_assert
  swap drop
  dup list-length 1 == static_assert
  drop

  # Transactions.
  ct-rewrite-txn-begin drop
  "txn_tmp"
  list-new "txn_tmp" list-append
  list-new "99" list-append
  ct-add-grammar-rewrite-named drop
  ct-rewrite-txn-rollback static_assert
  ct-list-grammar-rewrites "txn_tmp" list-contains? 0 == static_assert

  ct-rewrite-txn-begin drop
  "txn_keep"
  list-new "txn_keep" list-append
  list-new "77" list-append
  ct-add-grammar-rewrite-named drop
  ct-rewrite-txn-commit static_assert
  ct-list-grammar-rewrites "txn_keep" list-contains? static_assert
  "txn_keep" ct-remove-grammar-rewrite static_assert

  # Pack import/export.
  ct-export-rewrite-pack
  ct-import-rewrite-pack
  dup 1 >= static_assert
  drop

  # Trace/profile controls.
  1 ct-set-rewrite-trace
  ct-get-rewrite-trace static_assert
  "grammar"
  list-new "rw_opt" list-append
  16
  ct-rewrite-dry-run
  drop drop
  ct-get-rewrite-trace-log dup list-length 1 >= static_assert drop
  ct-clear-rewrite-trace-log dup 1 >= static_assert drop
  0 ct-set-rewrite-trace

  ct-get-rewrite-profile map-length 2 == static_assert
  ct-clear-rewrite-profile
  ct-get-rewrite-profile map-length 2 == static_assert

  # Strategy/budget/loop settings.
  "specificity" ct-set-rewrite-saturation
  ct-get-rewrite-saturation "specificity" string= static_assert
  "first" ct-set-rewrite-saturation

  64 ct-set-rewrite-max-steps
  ct-get-rewrite-max-steps 64 == static_assert
  100000 ct-set-rewrite-max-steps

  1 ct-set-rewrite-loop-detection
  ct-get-rewrite-loop-detection static_assert
  ct-clear-rewrite-loop-reports drop
  ct-get-rewrite-loop-reports list-length 0 == static_assert

  # Compatibility matrix.
  "grammar" ct-rewrite-compatibility-matrix dup list-length 1 >= static_assert drop
end
compile-time setup_part_c_advanced

word main
  1 puti cr
end
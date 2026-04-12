import stdlib.sl
import meta.sl

word setup_pattern_controls
  "pc_rule"
  list-new
  list-new "$x:int" list-append "+" list-append "0" list-append
  list-new "$x" list-append
  meta-macro-clauses-append
  meta-macro-pattern-register

  "pc_rule" meta-macro-pattern-enabled-get
  static_assert
  dup 1 == static_assert
  drop

  "pc_rule" 11 meta-macro-pattern-priority-set static_assert
  "pc_rule" meta-macro-pattern-priority-get
  static_assert
  dup 11 == static_assert
  drop

  "pc_rule" meta-macro-pattern-clauses
  static_assert
  dup list-length 1 == static_assert
  drop

  "pc_rule" 0 meta-macro-pattern-enabled-set static_assert
  "pc_rule" meta-macro-pattern-enabled-get
  static_assert
  dup 0 == static_assert
  drop

  "pc_rule" 1 meta-macro-pattern-enabled-set static_assert
end
compile-time setup_pattern_controls

word main
  pc_rule 77 + 0 puti cr
end

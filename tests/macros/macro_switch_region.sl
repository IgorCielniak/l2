import stdlib.sl
import macro_switch.sl

# Between macros-off and macros-on a macro name compiles as a word call.
macro seven () `7` emit ;

word expanded seven end           # macro expansion: 7

macros-off
word called seven end             # a call to the word `seven`
macros-on

word seven 8 end                  # a runtime word shadowing the macro

word main
  expanded puti cr
  called puti cr
end

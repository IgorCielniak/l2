import stdlib.sl

# `const NAME value` expands NAME to one token, created on first use.
const ANSWER 42
const NEG -7
const LETTER 'A'
const GREETING "hi"
const syscall.custom.num 99

word existing 1 end
const existing 2        # shadows the earlier word, like any redefinition

priority 5 const PRIO 10
const PRIO 11           # lower priority: PRIO stays 10

const UNUSED 123        # never used: never becomes a definition

word checks
  "ANSWER" ct-definition-exists? static-assert
  "UNUSED" ct-definition-exists? static-assert
end
compile-time checks

word main
  ANSWER puti cr
  NEG puti cr
  LETTER putc cr
  GREETING puts
  syscall.custom.num puti cr
  existing puti cr
  PRIO puti cr
  FD_STDERR puti cr
end

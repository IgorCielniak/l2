# This file intentionally has multiple errors to test error recovery.
# The compiler should report all of them rather than stopping at the first.
# No stdlib import — keeps line numbers stable.

word foo
    end end
end

word bar
    end end
end

word main
    0
end

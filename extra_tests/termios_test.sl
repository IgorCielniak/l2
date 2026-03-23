import stdlib.sl
import termios.sl

word main
    "stdin is a tty? " puts
    0 isatty puti cr

    "stdout is a tty? " puts
    1 isatty puti cr

    "stderr is a tty? " puts
    2 isatty puti cr

    "Invalid fd (999) is a tty? " puts
    999 isatty puti cr
end

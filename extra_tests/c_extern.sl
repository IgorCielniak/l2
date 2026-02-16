import stdlib.sl
import float.sl

# C-style externs (auto ABI handling)
extern long labs(long n)
extern void exit(int status)
extern double atan2(double y, double x)

word main
    # Test C-style extern with implicit ABI handling
    -10 labs puti cr

    1.5 2.5 f+              # 4.0
    fputln

    # External math library (libm)
    10.0 10.0 atan2         # Result is pi/4
    4.0 f*                  # Approx pi
    fputln

    # Test extern void
    0 exit
end

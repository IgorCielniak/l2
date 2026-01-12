import stdlib.sl
import float.sl

# C-style externs (auto ABI handling)
extern long labs(long n)
extern void exit(int status)
extern double atan2(double y, double x)

word main
    # Test C-style extern with implicit ABI handling
    -10 labs puti cr

    # Basic math
    1.5 2.5 f+ fputln       # Outputs: 4.000000
    
    # External math library (libm)
    10.0 10.0 atan2         # Result is pi/4
    4.0 f* fputln           # Outputs: 3.141593 (approx pi)

    # Test extern void
    0 exit
end

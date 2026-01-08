import stdlib/stdlib.sl
import stdlib/io.sl
import stdlib/float.sl

extern double atan2(double y, double x)

word main
    # Basic math
    1.5 2.5 f+ fputln       # Outputs: 4.000000
    
    # External math library (libm)
    10.0 10.0 atan2         # Result is pi/4
    4.0 f* fputln           # Outputs: 3.141593 (approx pi)
    
    0
end


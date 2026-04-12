#include <stdarg.h>

/* Sums variadic long args until sentinel -1 is seen. */
long va_sum_sentinel(long first, ...) {
    va_list ap;
    va_start(ap, first);
    long total = first;
    while (1) {
        long v = va_arg(ap, long);
        if (v == -1) break;
        total += v;
    }
    va_end(ap);
    return total;
}

/* Non-variadic helper for comparison. */
long add_two(long a, long b) {
    return a + b;
}

#include <stdint.h>
#include <stdio.h>
#include "libsysv_add.h"

int main(void) {
    int64_t result = l2_add2(2, INT64_C(19), INT64_C(23));
    int64_t doubled = l2_double(1, INT64_C(21));
    int64_t eight = l2_sum8(8, INT64_C(1), INT64_C(2), INT64_C(3), INT64_C(4),
                            INT64_C(5), INT64_C(6), INT64_C(7), INT64_C(8));
    printf("%lld %lld %lld\n",
           (long long)result,
           (long long)doubled,
           (long long)eight);
    return result == 42 && doubled == 42 && eight == 36 ? 0 : 1;
}

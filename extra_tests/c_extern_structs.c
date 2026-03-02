#include <stdint.h>
#include <time.h>

typedef struct {
    int64_t a;
    int64_t b;
} Pair;

typedef struct {
    int64_t a;
    int64_t b;
    int64_t c;
} Big;

long long pair_sum(Pair p) {
    return (long long)(p.a + p.b);
}

Pair make_pair(long long seed) {
    Pair out;
    out.a = seed;
    out.b = seed + 10;
    return out;
}

Big make_big(long long seed) {
    Big out;
    out.a = seed;
    out.b = seed + 1;
    out.c = seed + 2;
    return out;
}

long long big_sum(Big b) {
    return b.a + b.b + b.c;
}

long long pair_after_six(long long a, long long b, long long c,
                         long long d, long long e, long long f,
                         Pair p) {
    return a + b + c + d + e + f + p.a + p.b;
}

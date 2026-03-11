#ifndef TEST_STRUCTS_H
#define TEST_STRUCTS_H

struct Point {
    long x;
    long y;
};

struct Pair {
    long a;
    long b;
};

/* Pointer-based helpers (simple scalar ABI). */
long point_sum_ptr(struct Point *p);
long pair_sum_ptr(struct Pair *p);

#endif

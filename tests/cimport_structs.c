#include "cimport_structs.h"

long point_sum_ptr(struct Point *p) {
    return p->x + p->y;
}

long pair_sum_ptr(struct Pair *p) {
    return p->a + p->b;
}

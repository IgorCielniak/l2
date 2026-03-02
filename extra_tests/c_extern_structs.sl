import stdlib.sl

cstruct Pair
    cfield a i64
    cfield b i64
end

cstruct Big
    cfield a i64
    cfield b i64
    cfield c i64
end

cstruct LDiv
    cfield quot i64
    cfield rem i64
end

cstruct TimeSpec
    cfield tv_sec i64
    cfield tv_nsec i64
end

extern long long pair_sum(struct Pair p)
extern struct Pair make_pair(long long seed)
extern struct Big make_big(long long seed)
extern long long big_sum(struct Big b)
extern long long pair_after_six(long long a, long long b, long long c, long long d, long long e, long long f, struct Pair p)
extern struct LDiv ldiv(long numer, long denom)
extern int timespec_get(struct TimeSpec* ts, int base)
extern void exit(int status)

word main
    Pair.size alloc dup >r
    r@ 11 Pair.a!
    r@ 31 Pair.b!
    r@ pair_sum puti cr
    r> Pair.size free

    100 make_big dup >r
    r@ Big.a@ puti cr
    r@ Big.b@ puti cr
    r@ Big.c@ puti cr
    r@ big_sum puti cr
    r> Big.size free

    7 make_pair dup >r
    r@ Pair.a@ puti cr
    r@ Pair.b@ puti cr
    r> Pair.size free

    Pair.size alloc dup >r
    r@ 11 Pair.a!
    r@ 31 Pair.b!
    1 2 3 4 5 6 r@ pair_after_six puti cr
    r> Pair.size free

    20 3 ldiv dup >r
    r@ LDiv.quot@ puti cr
    r@ LDiv.rem@ puti cr
    r> LDiv.size free

    TimeSpec.size alloc dup >r
    r@ 1 timespec_get 1 == if 1 else 0 end puti cr
    r@ TimeSpec.tv_sec@ 0 > if 1 else 0 end puti cr
    r@ TimeSpec.tv_nsec@ 0 >= if 1 else 0 end puti cr
    r@ TimeSpec.tv_nsec@ 1000000000 < if 1 else 0 end puti cr
    r> TimeSpec.size free

    0 exit
end

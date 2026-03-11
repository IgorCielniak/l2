import ../stdlib/stdlib.sl
import ../stdlib/io.sl
import ../stdlib/mem.sl
import ../stdlib/hashmap.sl

word main
    # ── hm_new / hm_count / hm_capacity ──
    8 hm_new
    dup hm_count puti cr       # 0
    dup hm_capacity puti cr    # 8

    # ── hm_set / hm_get ──
    dup 42 100 hm_set
    dup 99 200 hm_set
    dup 7  300 hm_set

    dup hm_count puti cr       # 3

    dup 42 hm_get drop puti cr # 100
    dup 99 hm_get drop puti cr # 200
    dup 7  hm_get drop puti cr # 300

    # ── hm_get miss ──
    dup 999 hm_get             # should be 0, 0
    puti dup puti cr drop      # 00

    # ── hm_has ──
    dup 42 hm_has puti cr      # 1
    dup 999 hm_has puti cr     # 0

    # ── hm_set overwrite ──
    dup 42 111 hm_set
    dup 42 hm_get drop puti cr # 111
    dup hm_count puti cr       # 3 (no new entry)

    # ── hm_del ──
    dup 99 hm_del puti cr      # 1 (deleted)
    dup 99 hm_del puti cr      # 0 (already gone)
    dup 99 hm_has puti cr      # 0
    dup hm_count puti cr       # 2

    # ── insert after delete (tombstone reuse) ──
    dup 99 999 hm_set
    dup 99 hm_get drop puti cr # 999
    dup hm_count puti cr       # 3

    # ── hm_keys / hm_vals / hm_flags raw access ──
    dup hm_keys 0 != puti cr   # 1 (non-null pointer)
    dup hm_vals 0 != puti cr   # 1
    dup hm_flags 0 != puti cr  # 1

    # ── hm_clear ──
    hm_clear
    dup hm_count puti cr       # 0
    dup 42 hm_has puti cr      # 0 (cleared)

    # ── rehash (force growth) ──
    # insert enough to trigger rehash on the cleared map
    dup 1  10 hm_set
    dup 2  20 hm_set
    dup 3  30 hm_set
    dup 4  40 hm_set
    dup 5  50 hm_set
    dup 6  60 hm_set   # load > 70% → rehash
    dup 7  70 hm_set

    dup hm_capacity 8 > puti cr  # 1 (grew)
    dup hm_count puti cr         # 7

    # verify all entries survived rehash
    dup 1 hm_get drop puti cr    # 10
    dup 4 hm_get drop puti cr    # 40
    dup 7 hm_get drop puti cr    # 70

    # ── large key values ──
    dup 1000000 77 hm_set
    dup 1000000 hm_get drop puti cr  # 77

    hm_free
end

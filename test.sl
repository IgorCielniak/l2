import stdlib.sl

: test-add
    5 7 + puts
;

: test-sub
    10 3 - puts
;

: test-mul
    6 7 * puts
;

: test-div
    84 7 / puts
;

: test-mod
    85 7 % puts
;

: test-drop
    10 20 drop puts
;

: test-dup
    11 dup + puts
;

: test-swap
    2 5 swap - puts
;

: main
    test-add
    test-sub
    test-mul
    test-div
    test-mod
    test-drop
    test-dup
    test-swap
    0
;

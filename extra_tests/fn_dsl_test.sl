import stdlib/stdlib.sl
import stdlib/io.sl
import libs/fn.sl

use-fn-dsl

fun inc1(number x){
    x 1 +
}

def square(number x){
    x x *
}

fnc cube(number x){
    x x * x *
}

defn add(number a, number b){
    a + b;
}

function mix(int a, int b, int c){
    a + b;
    return a * b + c;
}

fn triple(int x){
    x + x + x
}

func inc2(number x){
    x 2 +
}

fnx dec1(number x){
    x 1 -
}

method mul2(number x){
    x 2 *
}

defun ident(number x){
    x
}

defn sub2(number a, number b){
    return a - b;
}

defn sum4(number a, number b, number c, number d){
    return a + b + c + d;
}

defn sum5(number a, number b, number c, number d, number e){
    return a + b + c + d + e;
}

defn sum6(number a, number b, number c, number d, number e, number f){
    return a + b + c + d + e + f;
}

word fn_dsl_compile_checks
    fn-dsl-active? static_assert

    fn-dsl-stats
    dup "stage" map-get static_assert
    swap drop
    "grammar" string= static_assert

    fn-dsl-compat
    dup list-length 1 >= static_assert
    drop

    fn-dsl-mode-fast
    ct-get-rewrite-saturation "first" string= static_assert
    ct-get-rewrite-max-steps 4096 == static_assert
    ct-get-rewrite-loop-detection 0 == static_assert

    fn-dsl-mode-safe
    ct-get-rewrite-saturation "specificity" string= static_assert
    ct-get-rewrite-max-steps 100000 == static_assert
    ct-get-rewrite-loop-detection static_assert

    fn-dsl-pack-export
    dup map-length 1 >= static_assert
    dup fn-dsl-pack-import dup 1 >= static_assert drop
    dup fn-dsl-pack-replace dup 1 >= static_assert drop
    drop

    fn-dsl-trace-on
    fn-dsl-trace-off

    "ct-list-words" fn-dsl-words-prefix "ct-list-words-prefix" list-contains? static_assert

    fn-dsl-ct-capabilities
    dup "ct_total" map-get static_assert 50 >= static_assert
    dup "ct_capture" map-get static_assert 8 >= static_assert
    dup "ct_parser" map-get static_assert 5 >= static_assert
    dup "ct_rewrite" map-get static_assert 5 >= static_assert
    drop

    fn-dsl-pattern-enabled
    swap static_assert
    static_assert

    fn-dsl-pattern-clauses
    static_assert
    dup list-length 8 == static_assert
    drop

    fn-dsl-pattern-conflicts
    dup list-length 0 >= static_assert
    drop

    fn-dsl-ct-call-policy-safe
    fn-dsl-ct-call-status
    dup "sandbox_mode" map-get static_assert "compile-only" string= static_assert
    dup "exception_policy" map-get static_assert "raise" string= static_assert
    dup "memo_enabled" map-get static_assert static_assert
    dup "side_effects_enabled" map-get static_assert static_assert
    dup "sandbox_allowlist_size" map-get static_assert 8 >= static_assert
    dup "recursion_limit" map-get static_assert 64 == static_assert
    dup "timeout_ms" map-get static_assert 250 == static_assert
    drop
    fn-dsl-ct-call-reset
    fn-dsl-ct-call-policy-open

    fn-dsl-parser-session-begin drop
    fn-dsl-parser-session-rollback static_assert

    fn-dsl-parser-session-begin drop
    list-new "(" list-append
    "1" list-append
    "(" list-append
    "2" list-append
    ")" list-append
    ")" list-append
    ct-current-token inject-lexemes
    next-token drop
    "(" ")" fn-dsl-parser-collect-balanced
    dup static_assert
    swap
    dup list-length 4 == static_assert
    drop
    drop
    fn-dsl-parser-session-rollback static_assert

    fn-dsl-parser-session-begin drop
    list-new "word" list-append
    "tmp_fn_token" list-append
    "99" list-append
    "end" list-append
    ct-current-token inject-lexemes
    0 ct-parser-peek dup token-lexeme swap fn-dsl-token-clone token-lexeme string= static_assert
    0 ct-parser-peek "renamed" fn-dsl-token-rename token-lexeme "renamed" string= static_assert
    0 ct-parser-peek token-column
    0 ct-parser-peek 2 fn-dsl-token-shift-column token-column
    swap 2 + == static_assert
    fn-dsl-parser-session-rollback static_assert

    fn-dsl-rewrite-scope-push drop
    "fn.dsl.test.inline"
    list-new "kwdsl" list-append
    list-new "77" list-append
    ct-add-grammar-rewrite-named drop
    "grammar" "fn.dsl.test.inline" "fn.dsl.test.pipeline" ct-set-rewrite-pipeline drop
    "grammar" "fn.dsl.test.pipeline" 1 ct-set-rewrite-pipeline-active
    "grammar" ct-rebuild-rewrite-index drop

    "grammar" list-new "kwdsl" list-append fn-dsl-rewrite-run
    dup list-length 1 >= static_assert
    drop
    dup 0 list-get "77" string= static_assert
    drop

    "grammar" list-new "kwdsl" list-append fn-dsl-rewrite-run-scoped
    dup list-length 1 >= static_assert
    drop
    dup 0 list-get "77" string= static_assert
    drop
    fn-dsl-rewrite-scope-pop static_assert
end
compile-time fn_dsl_compile_checks

word main
    use-fn-calls

    pipe(3, inc1) puti cr
    pipe2(3, 5, add) puti cr
    thread(4, square) puti cr
    chain 2 inc1 square puti cr
    invoke add 20 22 puti cr
    tapn 3 add 9 puti cr
    apply2 add 6 7 puti cr
    apply3 mix 2 3 4 puti cr
    pipe4(1, 2, 3, 4, sum4) puti cr
    pipe5(1, 2, 3, 4, 5, sum5) puti cr
    apply4 sum4 1 2 3 4 puti cr
    apply5 sum5 1 2 3 4 5 puti cr
    compose4 inc1 inc1 inc1 inc1 0 puti cr
    compose5 inc1 inc1 inc1 inc1 inc1 0 puti cr
    juxt2 inc1 square 3 + puti cr
    juxt3 inc1 square cube 3 + + puti cr
    flip2 sub2 2 10 puti cr
    guard(0, 33, 44) puti cr
    when-not(0, 55) puti cr
    thrush(9, inc1) puti cr

    ifelse(1, 9, 2) puti cr
    when(1, 7) puti cr
    unless(0, 8) puti cr

    7 |> inc1 puti cr
    square <| 4 puti cr
    5 -> inc1 puti cr
    square <- 5 puti cr
    1 && 5 puti cr
    0 || 7 puti cr

    inc2(8) puti cr
    dec1(8) puti cr
    mul2(8) puti cr
    ident(8) puti cr

    identity(42) puti cr
    pipe-if(4, 1, inc1) puti cr
    pipe-if(4, 0, inc1) puti cr
    pipe-unless(4, 1, inc1) puti cr
    pipe-unless(4, 0, inc1) puti cr
    pipe-last2(3, 4, add) puti cr
    pipe-last3(2, 3, 4, mix) puti cr
    pipe-last4(1, 2, 3, 4, sum4) puti cr
    apply6 sum6 1 2 3 4 5 6 puti cr
    pipe6(1, 2, 3, 4, 5, 6, sum6) puti cr
    compose6 inc1 inc1 inc1 inc1 inc1 inc1 0 puti cr
    juxt4 inc1 square cube ident 2 + + + puti cr
    juxt5 inc1 inc1 inc1 inc1 inc1 1 + + + + puti cr
    juxt6 inc1 inc1 inc1 inc1 inc1 inc1 1 + + + + + puti cr
    fn_simplify 9 + 0 puti cr
    fn_simplify 0 * 99 puti cr
    fn_simplify 7 * 1 puti cr

    when-do(1, 66 puti cr)
    unless-do(0, 67 puti cr)

    add(2, 5) puti cr
    mix(2, 3, 4) puti cr
    triple(4) puti cr
    cube(3) puti cr

    add(1, 2)
    add(3, 4)
    add puti cr
end

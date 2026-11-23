import stdlib/stdlib.sl
import stdlib/io.sl
import libs/fn.sl

# fn-dsl-test-tokens [*, lexemes | template] -> [* | tokens]
word fn-dsl-test-tokens
    >r list-new swap
    begin
        dup list-empty? if drop rdrop exit end
        list-pop-front drop
        r@ swap token-with-lexeme
        rot swap list-append swap
    again
end
compile-only

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

defn vars_math(int a, int b){
    let number acc = a + b;
    let number half = 0;
    acc = acc + 1;
    half = acc / 2;
    return half + acc;
}

defn vars_chain(number x){
    let number y = x + 1;
    y = y * 2;
    return y;
}

word fn_dsl_compile_checks
    fn-dsl-active? static-assert

    fn-dsl-lang-name ct-lang-exists? static-assert
    fn-dsl-lang-name ct-lang-active? static-assert
    fn-dsl-lang-name ct-lang-status
    dup static-assert
    drop
    dup "name" map-get static-assert "fn.dsl" string-equal? static-assert
    dup "active" map-get static-assert static-assert
    drop

    # The pack owns its aliases and operators; reinstalling replaces them.
    fn-dsl-lang-name ct-lang-status drop "grammar_rewrites" map-get drop
    list-length 13 == static-assert
    fn-dsl-install-rewrites
    fn-dsl-lang-name ct-lang-status drop "grammar_rewrites" map-get drop
    list-length 13 == static-assert

    fn-dsl-mode-fast
    ct-rewrite-get-max-steps 4096 == static-assert
    ct-rewrite-loop-detection? 0 == static-assert

    fn-dsl-mode-safe
    ct-rewrite-get-max-steps 100000 == static-assert
    ct-rewrite-loop-detection? static-assert

    "ct-definition-list-names" ct-definition-list-names-prefix "ct-definition-list-names-prefix" list-contains? static-assert

    fn-dsl-ct-capabilities
    dup "ct_total" map-get static-assert 50 >= static-assert
    dup "ct_parser" map-get static-assert 5 >= static-assert
    dup "ct_rewrite" map-get static-assert 5 >= static-assert
    drop

    # fn_simplify is a unified macro adopted by the fn.dsl pack
    "fn_simplify" ct-macro-get-by-name static-assert
    "parameters" map-get drop list-length 0 == static-assert


    ct-parser-session-begin
    ct-parser-session-rollback static-assert

    ct-parser-session-begin >r
    list-new "(" list-append
    "1" list-append
    "(" list-append
    "2" list-append
    ")" list-append
    ")" list-append
    0 ct-parser-peek fn-dsl-test-tokens ct-parser-inject
    ct-parser-consume drop
    "(" ")" ct-parser-collect-balanced
    static-assert
    list-length 4 == static-assert
    r> ct-parser-session-rollback static-assert

    ct-parser-session-begin >r
    list-new "word" list-append
    "tmp_fn_token" list-append
    "99" list-append
    "end" list-append
    0 ct-parser-peek fn-dsl-test-tokens ct-parser-inject
    0 ct-parser-peek "renamed" token-with-lexeme token-lexeme "renamed" string-equal? static-assert
    0 ct-parser-peek token-column
    0 ct-parser-peek "renamed" token-with-lexeme token-column
    == static-assert
    r> ct-parser-session-rollback static-assert

    ct-rewrite-scope-push drop
    "fn.dsl.test.inline"
    list-new "kwdsl" list-append
    list-new "77" list-append
    ct-rewrite-add-grammar-named
    "fn.dsl.test.pipeline" ct-rewrite-rule-set-pipeline static-assert
    "grammar" "fn.dsl.test.pipeline" 1 ct-rewrite-set-pipeline-active

    "grammar" list-new "kwdsl" list-append ct-rewrite-run-on-list
    dup list-length 1 >= static-assert
    drop
    dup 0 list-get "77" string-equal? static-assert
    drop

    "grammar" list-new "kwdsl" list-append fn-dsl-rewrite-run-scoped
    dup list-length 1 >= static-assert
    drop
    dup 0 list-get "77" string-equal? static-assert
    drop
    ct-rewrite-scope-pop static-assert
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
    when-not 0 55 puti cr
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
    pipe-if 4 1 inc1 puti cr
    pipe-if 4 0 inc1 puti cr
    pipe-unless 4 1 inc1 puti cr
    pipe-unless 4 0 inc1 puti cr
    pipe-last2 3 4 add puti cr
    pipe-last3 2 3 4 mix puti cr
    pipe-last4 1 2 3 4 sum4 puti cr
    apply6 sum6 1 2 3 4 5 6 puti cr
    pipe6(1, 2, 3, 4, 5, 6, sum6) puti cr
    compose6 inc1 inc1 inc1 inc1 inc1 inc1 0 puti cr
    juxt4 inc1 square cube ident 2 + + + puti cr
    juxt5 inc1 inc1 inc1 inc1 inc1 1 + + + + puti cr
    juxt6 inc1 inc1 inc1 inc1 inc1 inc1 1 + + + + + puti cr
    fn_simplify 9 + 0 puti cr
    fn_simplify 0 * 99 puti cr
    fn_simplify 7 * 1 puti cr

    when-do 1 `66 puti cr`
    unless-do 0 `67 puti cr`

    add(2, 5) puti cr
    mix(2, 3, 4) puti cr
    triple(4) puti cr
    cube(3) puti cr
    vars_math(4, 5) puti cr
    vars_chain(3) puti cr

    add(1, 2)
    add(3, 4)
    add puti cr
end

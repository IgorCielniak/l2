import stdlib/stdlib.sl
import stdlib/io.sl

word main
    # --- indexof ---
    "hello" 108 indexof puti cr             # 'l' at index 2
    "hello" 122 indexof puti cr             # 'z' not found -> -1

    # --- count_char_in_str ---
    "hello world" 108 count_char_in_str     # 3 l's
    puti cr 2drop

    # --- format1s (single string substitution) ---
    "world" "hello %" format1s
    2dup puts free

    # --- format1i (single int substitution) ---
    42 "the answer is %" format1i
    2dup puts free

    # --- formats (multi string substitution) ---
    "Bob" "Alice" "% and %" formats
    2dup puts free

    # --- formati (multi int substitution) ---
    2 1 "a: %, b: %" formati
    2dup puts free

    # --- edge: no '%' ---
    "no placeholders" formati
    puts

    # --- edge: '%' at start ---
    "X" "% is first" format1s
    2dup puts free

    # --- edge: '%' at end ---
    "Y" "last is %" format1s
    2dup puts free

    # --- format (mixed %i and %s) ---
    "hello" 1 "a: %i, b: %s" format
    2dup puts free

    # --- format: single %i ---
    42 "answer is %i" format
    2dup puts free

    # --- format: single %s ---
    "world" "hello %s" format
    2dup puts free

    # --- format: three mixed ---
    "bar" 123 "foo" "%s: %i and %s" format
    2dup puts free

    # --- format: no placeholders ---
    "nothing here" format
    puts

    # --- format: %s at start and end ---
    "B" "A" "%s and %s" format
    2dup puts free

    # --- format: %i at boundaries ---
    2 1 "%i+%i" format
    2dup puts free
end

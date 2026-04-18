
#strcmp [*, addr, len, addr | len] -> [* | bool]
word strcmp
    >r nip r> for
        2dup c@ swap c@ != if drop drop 0 rdrop ret end
        1 + swap 1 +
    end
    drop drop 1
end

# strdup [*, addr | len] -> [*, addr, len, addr1 | len1]
word strdup
    dup alloc 2 pick 2 pick memcpy
end

#strconcat [*, addr, len, addr | len] -> [*, addr | len]
word strconcat
    0 pick 3 pick +
    dup
    >r >r >r >r >r >r
    5 rpick
    alloc
    r> r>
    dup >r
    memcpy
    swap
    r> dup -rot +
    r> r>
    memcpy
    swap
    3 pick
    -
    nip
    swap
    0 rpick
    nip
    rot
    drop
    rdrop rdrop
end

#strlen [* | addr] -> [* | len]
# for null terminated strings
word strlen
    0 swap                  # len addr
    while dup c@ 0 != do
        1 +                 # addr++
        swap 1 + swap       # len++
    end
    drop                    # drop addr, leave len
end

#digitsN>num [*, d_{n-1}, d0 | n] -> [* | value]
word digitsN>num  # digits bottom=MSD, top=LSD, length on top (MSD-most significant digit, LSD-least significant digit)
    0 swap        # place accumulator below length
    for           # loop n times using the length on top
        r@ pick   # fetch next digit starting from MSD (uses loop counter as index)
        swap      # acc on top
        10 *      # acc *= 10
        +         # acc += digit
    end
end

#toint [*, addr | len] -> [* | int]
# converts a string to an int
word toint
    tuck
    0 swap
    dup >r
    for
        2dup +
        c@ 48 -
        swap rot
        swap
        1 +
    end
    2drop
    r>
    dup >r
    digitsN>num
    r> 1 +
    for
        nip
    end
    rdrop rdrop
end

#count_digits [* | int] -> [* | int]
# returns the amount of digits of an int
word count_digits
    dup 0 == if
        drop 1
    else
        0
        swap
        while dup 0 > do
            10 / swap 1 + swap
        end
        drop
    end
end

#tostr [* | int] -> [*, addr | len]
# the function allocates a buffer, remember to free it
word tostr
    dup
    count_digits
    dup >r alloc r@ swap
    swap rot swap
    for
        dup 10 % swap 10 /
    end
    drop

    r>
    1 swap dup
    for
        dup
        2 + pick
        2 pick
        2 + pick
        3 pick rot +
        swap 48 + swap 1 - swap c!
        swap
        1 +
        swap
    end

    swap 0 +
    pick 1 +
    over for
    rot drop
    end drop
end

# ---------------------------------------------------------------------------
#  String search helpers
# ---------------------------------------------------------------------------

#indexof [*, addr, len | char] -> [* | index]
# Finds the index of the first occurrence of char in the string.
# Returns -1 if not found. Consumes addr, len, and char.
word indexof
    >r                                      # char -> rstack
    -1 0                                    # result=-1, i=0
    while dup 3 pick < 2 pick -1 == band do # while i < len && result == -1
        dup 4 pick + c@                     # byte = addr[i]
        r@ == if                            # byte == char?
            nip dup                         # result = i
        end
        1 +                                 # i++
    end
    drop nip nip                            # drop i, addr, len -> leave result
    rdrop                                   # clean char from rstack
end

#count_char_in_str [*, addr, len | char] -> [*, addr, len | count]
# Counts the number of occurrences of char in the string.
# Preserves addr and len on stack; pushes count on top.
word count_char_in_str
    >r                          # char -> rstack
    0                           # count = 0
    1 pick                      # push len (for loop count)
    for
        # for counter r@ goes len..1; char is at 1 rpick
        # byte index = len - r@  (0 .. len-1)
        1 pick r@ - 3 pick + c@ # byte at addr[len - r@]
        1 rpick ==              # byte == char?
        if 1 + end              # increment count
    end
    rdrop                       # clean char from rstack
end

# ---------------------------------------------------------------------------
#  Single-substitution helpers
# ---------------------------------------------------------------------------

#format1s [*, arg_addr, arg_len, fmt_addr | fmt_len] -> [*, result_addr | result_len]
# Replaces the first '%' in fmt with the arg string.
# Allocates a new result string (caller should free it).
# If no '%' is found, returns fmt as-is (NOT newly allocated).
word format1s
    2dup 37 indexof                 # find first '%'
    dup -1 == if
        # no '%' — drop pos, drop arg, return fmt unchanged
        drop 2swap 2drop
    else
        # pos is on TOS; save pos, fmt_addr, fmt_len to rstack
        >r                         # rstack: [pos]
        over >r                    # rstack: [pos, fmt_addr]
        dup >r                     # rstack: [pos, fmt_addr, fmt_len]
        # rpick 0=fmt_len  1=fmt_addr  2=pos
        # stack: arg_addr, arg_len, fmt_addr, fmt_len
        drop                       # drop fmt_len (we have it on rstack)
        2 rpick                    # push pos
        # stack: arg_addr, arg_len, fmt_addr, pos  (= prefix pair)
        2swap                      # stack: fmt_addr, pos, arg_addr, arg_len
        strconcat                  # tmp = prefix + arg
        # save tmp for later freeing
        2dup >r >r
        # rpick 0=tmp_addr 1=tmp_len 2=fmt_len 3=fmt_addr 4=pos
        # build suffix: (fmt_addr + pos + 1, fmt_len - pos - 1)
        3 rpick 4 rpick + 1 +     # suffix_addr
        2 rpick 4 rpick - 1 -     # suffix_len
        strconcat                  # result = tmp + suffix
        # free tmp
        r> r> free
        # clean saved pos, fmt_addr, fmt_len
        rdrop rdrop rdrop
    end
end

#format1i [*, int_arg, fmt_addr | fmt_len] -> [*, result_addr | result_len]
# Replaces the first '%' in fmt with the decimal representation of int_arg.
# Allocates a new result string (caller should free it).
word format1i
    rot tostr                      # convert int to string (allocates)
    2dup >r >r                     # save tostr result for freeing
    2swap                          # bring fmt on top
    format1s                       # substitute
    r> r> free                     # free the tostr buffer
end

# ---------------------------------------------------------------------------
#  Multi-substitution format words
# ---------------------------------------------------------------------------

# Replaces each '%' in fmt with the corresponding string argument.
# s1 (just below fmt) maps to the first '%', s2 to the second, etc.
# The number of string-pair args must equal the number of '%' markers.
# Returns a newly allocated string, or fmt as-is when there are no '%'.
#formats [*, sN_addr, sN_len, ..., s1_addr, s1_len, fmt_addr | fmt_len] -> [*, result_addr | result_len]
word formats
    2dup 37 count_char_in_str nip nip  # count '%' -> n
    dup not if
        drop                       # no substitutions needed
    else
        1 - >r                     # remaining = n - 1
        format1s                   # first substitution
        while r@ 0 > do
            2dup >r >r             # save current result for freeing
            format1s               # next substitution
            r> r> free             # free previous result
            r> 1 - >r             # remaining--
        end
        rdrop                      # clean counter
    end
end


# Replaces each '%' in fmt with the decimal string of the corresponding
# integer argument.  i1 (just below fmt) maps to the first '%', etc.
# The number of int args must equal the number of '%' markers.
# Returns a newly allocated string, or fmt as-is when there are no '%'.
#formati [*, iN, ..., i1, fmt_addr | fmt_len] -> [*, result_addr | result_len]
word formati
    2dup 37 count_char_in_str nip nip  # count '%' -> n
    dup not if
        drop                       # no substitutions needed
    else
        1 - >r                     # remaining = n - 1
        format1i                   # first substitution
        while r@ 0 > do
            2dup >r >r             # save current result for freeing
            format1i               # next substitution
            r> r> free             # free previous result
            r> 1 - >r             # remaining--
        end
        rdrop                      # clean counter
    end
end

# ---------------------------------------------------------------------------
#  Mixed-type printf-style format (with %i and %s specifiers)
# ---------------------------------------------------------------------------

#find_fmt [*, addr | len] -> [* | pos, type]
# Scans for the first %i or %s placeholder in the string.
# Returns position of '%' and the type byte (105='i' or 115='s').
# Returns -1, 0 if no placeholder is found.
word find_fmt
    over >r dup >r           # rstack: 0=len, 1=addr
    -1 0 0                    # pos=-1, type=0, i=0
    while dup 0 rpick 1 - < 2 pick not band do
        1 rpick over + c@    # addr[i]
        37 == if              # '%'
            1 rpick over + 1 + c@  # addr[i+1]
            dup 105 == over 115 == bor if
                rot drop rot drop over  # pos=i, type=char, keep i
            else
                drop
            end
        end
        1 +
    end
    drop                      # drop i
    rdrop rdrop
    2swap 2drop               # remove addr, len copies; leave pos, type
end

#count_fmt [*, addr | len] -> [*, addr, len | count]
# Counts the number of %i and %s placeholders in a string.
word count_fmt
    over >r dup >r            # rstack: 0=len, 1=addr
    0 0                        # count=0, i=0
    while dup 0 rpick 1 - < do
        1 rpick over + c@
        37 == if
            1 rpick over + 1 + c@
            dup 105 == over 115 == bor if
                drop
                swap 1 + swap
                1 +
            else
                drop
            end
        end
        1 +
    end
    drop                       # drop i
    rdrop rdrop
end

#fmt_splice [*, repl_addr, repl_len, fmt_addr, fmt_len | pos] -> [*, result_addr | result_len]
# Replaces the 2-char placeholder at pos in fmt with repl.
# Allocates a new result string (caller should free it).
word fmt_splice
    >r >r >r                  # rstack: [pos, fmt_len, fmt_addr]
    # rpick: 0=fmt_addr, 1=fmt_len, 2=pos
    0 rpick 2 rpick           # push fmt_addr, pos (= prefix pair)
    2swap                      # stack: fmt_addr, pos, repl_addr, repl_len
    strconcat                  # tmp = prefix + repl
    2dup >r >r                 # save tmp for freeing
    # rpick: 0=tmp_addr, 1=tmp_len, 2=fmt_addr, 3=fmt_len, 4=pos
    2 rpick 4 rpick + 2 +     # suffix_addr = fmt_addr + pos + 2
    3 rpick 4 rpick - 2 -     # suffix_len  = fmt_len - pos - 2
    strconcat                  # result = tmp + suffix
    r> r> free                 # free tmp
    rdrop rdrop rdrop          # clean fmt_addr, fmt_len, pos
end

#format1 [*, arg(s), fmt_addr | fmt_len] -> [*, result_addr | result_len]
# Replaces the first %i or %s placeholder in fmt.
# For %i: consumes one int from below fmt and converts via tostr.
# For %s: consumes one (addr, len) string pair from below fmt.
# Allocates a new result string (caller should free it).
word format1
    2dup find_fmt              # stack: ..., fmt_addr, fmt_len, pos, type
    dup 105 == if
        # %i
        drop                   # drop type
        >r >r >r               # save pos, fmt_len, fmt_addr
        tostr                  # convert int arg to string
        2dup >r >r             # save istr for freeing
        r> r> r> r> r>         # restore: istr_addr, istr_len, fmt_addr, fmt_len, pos
        fmt_splice
        2swap free             # free tostr buffer
    else 115 == if
        # %s: stack already has repl_addr, repl_len, fmt_addr, fmt_len, pos
        fmt_splice
    else
        drop                   # no placeholder, drop pos
    end
end

#format [*, args..., fmt_addr | fmt_len] -> [*, result_addr | result_len]
# Printf-style formatting with %i (integer) and %s (string) placeholders.
# Arguments are consumed left-to-right: the closest arg to the format
# string maps to the first placeholder.
#
# Example:  "bar" 123 "foo" "%s: %i and %s" format
#           -> "foo: 123 and bar"
#
# Returns a newly allocated string (caller should free it), or the
# original format string unchanged if there are no placeholders.
word format
    2dup count_fmt nip nip     # n = placeholder count
    dup not if
        drop
    else
        >r
        format1                # first substitution
        r> 1 -
        while dup 0 > do
            >r
            2dup >r >r         # save current result for freeing
            format1            # next substitution
            r> r> free         # free previous result
            r> 1 -
        end
        drop                   # drop counter (0)
    end
end

# rotate N elements of the top of the stack
# nrot [*, x1 ... xN - 1 | xN] -> [*, xN, xN - 1 ... x2 | x1]
word nrot
    dup 1 + 1 swap for
        dup pick swap 2 +
    end
    1 - 2 / pick
    dup for
        swap >r rswap
    end
    1 + for
        nip
    end
    for
        rswap r>
    end
end

# convert a string to a sequence of ascii codes of its characters and push the codes on to the stack, 
# Warning! the sequence is reversed so the ascii code of the last character ends up first on the stack
# toascii [*, addr | LEN] -> [*, x, x1 ... xLEN - 1 | xLEN]
word toascii
    0 swap
    for
        2dup + c@
        -rot
        1 +
    end
    2drop
end

# rm_zero_len_str [*, addr0, len0 ... addrN, lenN | N] -> [*, addrX, lenX ... addrY, lenY | Z]
word rm_zero_len_str
    dup for
        swap dup 0 == if
            drop nip 1 -
        else
            >r rswap swap >r rswap
        end
    end

    dup 2 * for
        rswap r> swap
    end
end

# emit_strs [*, addr | len] -> [*,  addr0, len0 ... addrN | lenN]
# given an addr and len emits pairs (addr, len) of strings in the given memopry region
word emit_strs
    0 >r
    >r
    while r@ 0 > do
        dup strlen dup r> swap - >r
        over over + rswap r> 1 + >r rswap
        while dup c@ 0 == do 1 + r> 1 - >r end
    end
    drop rdrop
end

# splitby_str [*, addr, len, addr1, len1] -> [*, addr0, len0 ... addrN, lenN | N]
# splits a string by another string and emmits a sequence of the new (addr, len) pairs on to the stack as well as the number of strings the oprtation resulted in.
word splitby_str
    2 pick for
        3 pick 0 2 pick 4 pick swap
        strcmp 1 == if 3 pick over 0 memset_bytes end
        >r >r swap 1 + swap r> r>
    end
    2drop 2dup - >r nip r> swap emit_strs r>
    rm_zero_len_str
end

# splitby [*, addr, len, addr1 | len1] -> [*, addr0, len0 ... addrN, lenN | N]
# split a string by another string, delegates to either splitby_char or splitby_str based on the length of the delimiter.
word splitby
    dup 1 == if
        splitby_char
    else
        splitby_str
    end
end

# splitby_char [*, addr, len, addr1 | len] ->  [*, addr1, len1 ... addrN, lenN | N]
# split a string by a given character, the resulting (addr, len) pairs are pushed on to the stack followed by the number of the pushed strings.
word splitby_char
    2 pick >r
    >r >r 2dup r> r> 2swap 2dup
    >r >r toascii 1 rpick nrot r> r>

    dup 3 + pick c@

    swap for
        dup
        3 pick == if over 0 c! end
        swap 1 + swap >r nip r>
    end

    2drop 2drop drop

    r>
    emit_strs
    r>
    rm_zero_len_str
end

# ltrim [*, addr | len] -> [*, addr, | len]
word ltrim
    dup for
        over c@ 32 == if
            swap 1 + swap 1 -
        end
    end
end

# rtrim [*, addr | len] -> [*, addr, | len]
word rtrim
    swap tuck swap
    swap over + 1 - swap
    dup for
        over c@ 32 == if
            swap 1 - swap 1 -
        end
    end nip
end

# trim [*, addr | len] -> [*, addr | len]
word trim
    ltrim rtrim
end

# startswith [*, addr, len, addr | len] -> [*, bool]
inline word startswith
    strcmp
end

# endswith [*, addr, len, addr | len] -> [*, bool]
word endswith
    dup 3 pick swap - 4 pick + over 2 pick 4 pick swap strcmp
    nip nip nip nip
end

# contains [*, addr, len, addr | len] -> [* | bool]
word contains
    2 pick for
        4dup strcmp 1 == if 1 nip nip nip nip rdrop ret end
        >r >r >r 1 + r> r> r>
    end 0 nip nip nip nip
end

# find the first occurence of a string inside another string, returns the index
# find [*, addr, len, addr | len] -> [* | index]
word find
    0 >r 2 pick for
        4dup strcmp 1 == if rswap r> nip nip nip nip rdrop ret end
        >r >r >r 1 + r> r> r> rswap r> 1 + >r rswap
    end -1 nip nip nip nip
end

# find the last occurence of a string inside another string, returns the index
# rfind [*, addr, len, addr | len] -> [* | index]
word rfind
    >r >r dup >r + 1 - r> r> r>
    2 pick 1 - >r 2 pick for
        4dup strcmp 1 == if rswap r> nip nip nip nip rdrop ret end
        >r >r >r 1 - r> r> r> rswap r> 1 - >r rswap
    end -1 nip nip nip nip
end

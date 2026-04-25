import stdlib.sl
import gvars.sl
import hmm.sl

sized_global defer_buf   8
sized_global defer_count 8

macro cur_frame 0 defer_buf @ defer_count @ 1 - 8 * + ;

macro func 1 word $0 defer_frame ;
macro fend 0 run_defers end ;

# defer_frame [*] -> [*]
word defer_frame
    defer_count dup @ 1 + !
    defer_buf @ 0 == if
        8 halloc
        defer_buf swap !
    end
    cur_frame 8 halloc !
end

# defer [* | word_ptr] -> [*]
word defer
    cur_frame @ dup @ 1 + !
    cur_frame @ cur_frame @ @ 1 + 8 * hrealloc cur_frame over !
    cur_frame @ @ 8 * + swap !
end

# run_defers [*] -> [*]
word run_defers
    0 cur_frame @ @ for
        cur_frame @ over 1 + 8 * + @ call 1 +
    end drop
    cur_frame @ hfree
    defer_count dup @ 1 - !
    defer_count @ 0 == if
        defer_buf @ hfree
        defer_buf 0 !
    end
end

import ../stdlib/stdlib.sl
import ../stdlib/hma.sl

# hfree consumes exactly its pointer: values the caller keeps beneath it
# survive, whether or not the freed block gives its region back.
word main
    777 16 halloc 32 halloc 48 halloc
    swap hfree             # a block in the middle of a region
    depth puti cr
    hfree                  # the last block allocated
    depth puti cr
    hfree                  # the last live block: its region is unmapped
    depth puti cr
    puti cr
    0
end

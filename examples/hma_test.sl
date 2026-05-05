import hma.sl
import debug.sl

word main
    10 halloc
    dump_blocks
    "----------- stack dump -------------" puts
    3 dump
    "------------------------------------" puts
    hfree
    dump_blocks
    "------------------------------------" puts
    20 halloc
    30 halloc
    
    "----------- blocks dump2 -----------" puts
    dump_blocks
    "------------------------------------" puts

    swap hfree
    dump_blocks
    "------- stack dump ----------------" puts
    10 dump
    "-----------------------------------" puts
    "---- free last and dump blocks ----" puts
    hfree

    dump_blocks

    "---------- stack dump ------------" puts
    10 dump
end


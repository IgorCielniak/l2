import stdlib.sl

# and/or are logical: any nonzero value is true, the result is 0 or 1.
word main
  1 1 and puti 1 0 and puti 0 1 and puti 0 0 and puti cr     # 1000
  1 1 or puti 1 0 or puti 0 1 or puti 0 0 or puti cr         # 1110
  5 3 and puti -2 0 or puti 0 7 and puti 5 not puti 0 not puti cr   # 11001
  0
end

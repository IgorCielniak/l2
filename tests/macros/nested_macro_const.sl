import stdlib.sl

macro const (name value)
    `macro $name \` $value \` emit ;` emit
;

const a 4

word main
    a puti cr
end

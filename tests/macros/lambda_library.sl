import stdlib.sl
import lambda.sl

word main
  lambda(
    1 2 +
  ) call puti cr

  lambda(
    40 2 +
  ) call puti cr

  lambda(
    1 if
      10
    else
      20
    end
  ) call puti cr
end

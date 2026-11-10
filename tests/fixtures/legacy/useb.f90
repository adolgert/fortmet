! Same common block under different local names; dummy has no intent.
subroutine useb(v)
  real v
  common /blk/ first, second, k
  v = first + 1
  k = 2
end subroutine

module hm
  implicit none
  real :: g1, g2
  integer :: hits
contains
  subroutine touch(x)
    real, intent(in) :: x
    hits = hits + 1
    g1 = x
    call inner()
  contains
    subroutine inner()
      g2 = g1 + x
    end subroutine
  end subroutine
  real function peek()
    peek = g2
  end function
end module

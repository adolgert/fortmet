module state
  implicit none
  real :: total = 0.0
  integer :: ncalls
  integer, parameter :: nmax = 10
  type :: shape
    real :: r
  contains
    procedure :: area => shape_area
  end type
  type, extends(shape) :: disk
  contains
    procedure :: area => disk_area
  end type
  interface norm
    module procedure norm_r, norm_i
  end interface
  abstract interface
    real function fn(x)
      real, intent(in) :: x
    end function
  end interface
contains
  real function shape_area(self)
    class(shape), intent(in) :: self
    shape_area = self%r
  end function
  real function disk_area(self)
    class(disk), intent(in) :: self
    disk_area = 3.14 * self%r**2
  end function
  pure real function norm_r(x)
    real, intent(in) :: x
    norm_r = abs(x)
  end function
  pure real function norm_i(i)
    integer, intent(in) :: i
    norm_i = abs(real(i))
  end function
  subroutine bump(x)
    real, intent(inout) :: x
    x = x + 1
  end subroutine
  subroutine setout(x)
    real, intent(out) :: x
    x = 0
  end subroutine
end module state

subroutine work(s, f, a, n)
  use state
  implicit none
  class(shape), intent(in) :: s
  procedure(fn) :: f
  procedure(fn), pointer :: pf
  integer, intent(in) :: n
  real :: a(n), y, t
  real :: counter = 0.0
  real, save :: kept
  common /blk/ cx, cy
  real :: cx, cy
  integer :: i, ios
  ncalls = ncalls + 1
  y = s%area() + norm(2) + norm(1.5) + f(1.0)
  pf => f
  y = y + pf(2.0) + cx
  call bump(total)
  call setout(cy)
  call random_number(t)
  call cpu_time(t)
  do i = 1, min(n, nmax)
    a(i) = y + t
  end do
  read(5, *, iostat=ios, end=10, err=10) kept
  write(*, *) y, total
  print *, counter
10 if (ios /= 0) stop 1
end subroutine work

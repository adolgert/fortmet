module qm
  implicit none
  type :: vec
    real :: v(3)
    type(vec), pointer :: next => null()
  contains
    procedure :: add
    generic :: operator(+) => add
    procedure :: scale
  end type
  type :: box
    type(vec) :: corner
  end type
  real, target :: gbuf(10)
  real :: gx, gy
  integer :: gcount
  namelist /cfg/ gx, gy
contains
  function add(a, b) result(c)
    class(vec), intent(in) :: a, b
    type(vec) :: c
    c%v = a%v + b%v
  end function
  subroutine scale(self, f)
    class(vec), intent(inout) :: self
    real, intent(in) :: f
    self%v = self%v * f
  end subroutine
  subroutine driver(b, w, n)
    type(box), intent(inout) :: b
    type(vec) :: w, z
    integer, intent(in) :: n
    real, pointer :: p(:)
    real, allocatable :: tmp(:)
    character(20) :: line
    integer :: i, u, ios
    z = w + b%corner
    call b%corner%scale(f=gx)
    call w%scale(2.0)
    allocate(tmp(n), stat=ios)
    p => gbuf
    p(1) = 3.0
    outer: do i = 1, n
      if (tmp(i) > gy) exit outer
      gbuf(i) = tmp(i)
    end do outer
    associate (c => gcount, e => gx + 1)
      c = c + 1
    end associate
    open(newunit=u, file='x', iostat=ios)
    read(u, nml=cfg)
    write(line, '(f8.3)') gx
    read(line, *) gy
    deallocate(tmp)
    call helper(gcount, tmp)
    call ext(gx)
  end subroutine
  subroutine helper(k, t)
    integer :: k
    real, allocatable, intent(out) :: t(:)
    k = 0
  end subroutine
end module

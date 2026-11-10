# fortmet

Fortran source metrics from three free tools, merged into one set of tables.

    uv run fortmet path/to/src                 # all tools, writes ./fortmet-out
    uv run fortmet src -DREAL64 -Iinclude      # preprocessor symbols and include dirs
    uv run fortmet src --tools lizard,fparser  # no compiler needed

From another project: `uv add --dev /path/to/fortmet` (or `uvx --from /path/to/fortmet fortmet src`).

Needs: Python 3.12 + uv; `gfortran` (or `cpp`) for preprocessing; `flang-new`/`flang` (LLVM 18
tested) for the flang backend. Set `FORTMET_FLANG` to pick a specific binary.

## What each tool contributes

| | Lizard (`lz_`) | fparser2 (`fp_`) | flang (`fl_`) |
|---|---|---|---|
| How it reads code | token patterns | full syntax tree, no semantics | compiler parse tree + symbol table |
| Needs code to compile | no | no | modules it uses must be found; falls back to syntax-only |
| Per procedure | NLOC, CCN, tokens, params | statements, CCN, CCN+logical ops, cognitive complexity, nesting, Halstead, maintainability index, gotos, exits | statements, CCN, nesting, calls resolved from array refs, fan-out/fan-in, symbol counts by role and type, implicit typing, missing INTENT |
| Structure | none | USE (with/without ONLY), COMMON, IMPLICIT, derived types, interfaces, EQUIVALENCE, DATA, SAVE | COMMON with resolved members, EQUIVALENCE sets, use/host association, module public/private |
| Weak spots seen on the sample | skips main programs; misses `else if` written as two words | rejects F2008 binding lists and all coarray syntax (worked around, see below) | LLVM 18 has gaps in coarray semantics (1 of 36 files) |

fparser2 and flang use the same counting rules and agree on CCN, statements, nesting and
argument counts for all 150 procedures in `tsunami/src`. That agreement is the cross-check;
a row where they differ is worth a look.

## Output

- `files.csv` physical line counts (`loc_*`), per-tool parse status and errors
- `procedures.csv` one row per procedure (`module::proc::internal`), columns prefixed by tool
- `units.csv` modules, submodules, block data
- `calls.csv` resolved call graph: caller, callee, kind, count, certain (see References)
- `accesses.csv` routine -> variable with reads, writes, maybe_writes (see References)
- `uses.csv` module use graph (fparser)
- `commons.csv` common blocks and members per program unit (both parsers)
- `metrics.json` all of the above

## Definitions

- **loc_code**: physical lines carrying statement text. `loc_code_after_cpp` is the same count
  on preprocessed text, i.e. the code active under the given `-D` flags.
- **ccn**: 1 + `if`/`else if`/one-line `if` + `do` + non-default `case`/type guards
  + 2 per arithmetic `if` + labels of a computed `goto`. `where`/`forall` are not counted.
- **ccn_ext**: ccn + each `.and.`/`.or.`.
- **cognitive** (fparser): Sonar-style; +1 plus nesting depth per `if`/`do`/`select`, +1 per
  `else`/`else if`/`goto` and per run of like logical operators. An approximation.
- **Halstead** (fparser): operands are names and literals, operators are every other tree
  leaf plus statement kinds without a keyword leaf. Not comparable to other tools' Halstead.
- **maintainability**: `171 - 5.2 ln(volume) - 0.23 ccn - 16.2 ln(statements)`, scaled 0-100.
- **ref_fan_out**: distinct non-intrinsic procedures a routine may call, counting every
  candidate of an uncertain call. **ref_fan_in**: routines that may call it.
- **ref_hidden_inputs / ref_hidden_outputs**: distinct module, COMMON and host variables a
  routine reads / writes. **ref_hidden_maybe_outputs**: passed to a dummy without INTENT.

## References (`refs.py`, needs flang)

Every name in a routine's executable statements is resolved to the entity it denotes and
tagged with how it is used. Ids are `module::routine::name`; a COMMON member is
`common:block+offset`, so two routines that give the same storage different names match.

`accesses.csv`, one row per routine and variable:

- `kind`: `local`, `dummy`, `result`, `parameter`, `module`, `host` (variable of an enclosing
  routine or main program), `common`, `procedure` (name used as a value, e.g. passed on).
- `reads`, `writes`: counted per occurrence. Writes are assignment targets, READ items,
  namelist reads, internal-file writes, `stat=`/`iostat=`/`newunit=`, allocate/deallocate,
  pointer assignment, loop variables, and arguments bound to `intent(out)`/`intent(inout)`.
- `maybe_writes`: passed to a dummy without INTENT, or to a procedure whose definition is
  not known (external, dummy procedure, procedure pointer). Also counted as a read.
- `saved`: local that persists between calls (SAVE or an initialiser).
- A write to `x%c` or `x(i)` is a write to `x`. A write through an ASSOCIATE name is a write
  to the variable it names.

`calls.csv`, one row per caller, callee and kind:

- `direct`, `intrinsic`, `external` (no definition among the analysed files)
- `generic`: resolved to the specific procedure flang chose
- `defined-operation`: call hidden behind an operator or assignment on a derived type
- `type-bound`: `certain=0` when the object is polymorphic and an extension overrides the
  binding; one row per candidate
- `dummy-procedure`, `procedure-pointer`: target unknown, `certain=0`
- `by-name`: fallback for files where flang's semantic check failed

Files are joined into one program for external procedures, type extension and fan-in. If
the same routine id is defined in two files (as in `tsunami/src`, eight copies of one
program), each directory is treated as its own program.

Known gaps: references in declarations (array bounds, initialisers) are not counted;
pointer and EQUIVALENCE aliasing is not followed (`equivalenced` only flags the variable);
operator calls are counted once per routine, not per use; type-bound generics are not
narrowed to one specific; submodules, ENTRY, statement functions and BLOCK DATA are untested.

Tests: `uv run pytest` (hand-checked fixtures in `tests/fixtures`).

## Preprocessing

`--cpp auto` (default) preprocesses files with a capitalised extension or any `#` directive,
using `gfortran -cpp -E`. Line numbers are kept aligned with the original file unless an
`#include` adds lines. Lizard and fparser get the preprocessed text; flang preprocesses
itself with the same `-D`/`-I`. Metrics describe one configuration of `-D` flags.

## Workarounds applied (reported in `fp_workarounds`, `fl_sema`)

- `split-binding-lists`: `procedure, private :: a, b` is rewritten as two bindings for fparser.
- `stripped-coarrays`: only if the parse fails; codimensions/coindices removed, image-control
  statements become `continue`, `change team` becomes `block`. Statement and branch counts
  are unchanged, Halstead counts shift slightly.
- flang runs in its own module directory per source directory (in `fortmet-out/work`), in
  module-dependency order, so gfortran `.mod` files beside the sources do not confuse it.
  Do not pass `-I` a directory that holds gfortran `.mod` files.
- If flang's semantic check fails, the file is re-read syntax-only (`fl_sema=0`): no symbol
  columns, and array references may be counted as function calls.

## Not covered yet

Graph metrics on top of the references (cohesion, coupling, closures, effect propagation),
inheritance depth, duplicate code, history, fixed-form beyond a smoke test.

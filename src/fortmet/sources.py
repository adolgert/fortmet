"""Find Fortran sources, run the C preprocessor, count lines, find module dependencies."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

FREE_EXT = {".f90", ".f95", ".f03", ".f08", ".f18"}
FIXED_EXT = {".f", ".for", ".ftn", ".f77", ".fpp"}
SKIP_DIRS = {".git", ".venv", "build", "fortmet-out", "__pycache__"}

_DIRECTIVE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|define|undef|include|else|elif|endif)\b", re.M)
_MARKER = re.compile(r'^# (\d+) "([^"]*)"')
_MODULE = re.compile(r"^\s*module\s+(\w+)\s*(?:!.*)?$", re.I | re.M)
_SUBMODULE = re.compile(r"^\s*submodule\s*\(\s*(\w+)[\w:\s]*\)\s*(\w+)", re.I | re.M)
_USE = re.compile(r"^\s*use\b(?:\s*,\s*(non_intrinsic|intrinsic))?\s*(?:::)?\s*(\w+)", re.I | re.M)


def is_fixed(path: Path) -> bool:
    return path.suffix.lower() in FIXED_EXT


def discover(paths: list[str]) -> list[Path]:
    """Expand files and directories into a sorted list of Fortran source files."""
    exts = FREE_EXT | FIXED_EXT
    found: set[Path] = set()
    for p in map(Path, paths):
        if p.is_dir():
            for f in p.rglob("*"):
                if f.suffix.lower() in exts and f.is_file() and not SKIP_DIRS & set(f.parts):
                    found.add(f.resolve())
        elif p.is_file():
            found.add(p.resolve())
        else:
            raise SystemExit(f"fortmet: no such file or directory: {p}")
    return sorted(found)


def needs_cpp(path: Path, text: str) -> bool:
    """Capitalised extension (.F90, .F) or any cpp directive in the text."""
    return path.suffix != path.suffix.lower() or bool(_DIRECTIVE.search(text))


def preprocess(path: Path, defines: list[str], includes: list[str]) -> str:
    """Run cpp via gfortran and return text whose line numbers match the original.

    Linemarkers are removed. Lines of the main file stay on their original line
    numbers as long as no #include has pulled in more text than it replaced.
    """
    flags = [f"-D{d}" for d in defines] + [f"-I{i}" for i in includes]
    if shutil.which("gfortran"):
        cmd = ["gfortran", "-cpp", "-E", *flags, str(path)]
    elif shutil.which("cpp"):
        cmd = ["cpp", "-traditional-cpp", *flags, str(path)]
    else:
        raise RuntimeError("no preprocessor found (need gfortran or cpp)")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[0] if r.stderr.strip() else "cpp failed")
    out: list[str] = []
    for line in r.stdout.splitlines():
        m = _MARKER.match(line)
        if m:
            if m.group(2) == str(path):
                out.extend([""] * (int(m.group(1)) - 1 - len(out)))
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def _split_comment(line: str) -> tuple[str, bool]:
    """Free-form: return (code part, has_comment), respecting quoted strings."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "!":
            return line[:i], True
    return line, False


def count_lines(text: str, fixed: bool) -> dict:
    """Physical line counts. 'code' counts lines holding any statement text."""
    c = dict(total=0, blank=0, comment=0, code=0, code_with_comment=0,
             pp_directive=0, omp_acc_directive=0, continuation=0)
    prev_continues = False
    for line in text.splitlines():
        c["total"] += 1
        s = line.strip()
        if not s:
            c["blank"] += 1
        elif s.startswith("#"):
            c["pp_directive"] += 1
        elif re.match(r"(?i)^[!c*]\$(omp|acc)", line if fixed else s):
            c["omp_acc_directive"] += 1
        elif s.startswith("!") or (fixed and line[0] in "cC*dD"):
            c["comment"] += 1
        else:
            code, has_comment = _split_comment(line)
            c["code"] += 1
            c["code_with_comment"] += has_comment
            if fixed:
                c["continuation"] += len(line) > 5 and line[:5].strip() == "" and line[5] not in " 0"
            else:
                c["continuation"] += prev_continues
                prev_continues = code.rstrip().endswith("&")
    c["comment_ratio"] = round((c["comment"] + c["code_with_comment"]) / c["code"], 3) if c["code"] else 0.0
    return c


def module_deps(text: str) -> tuple[set[str], set[str]]:
    """Return (modules defined, modules used) by regex; enough to order a build."""
    defined = {m.lower() for m in _MODULE.findall(text) if m.lower() != "procedure"}
    used = {name.lower() for nature, name in _USE.findall(text) if nature.lower() != "intrinsic"}
    for parent, sub in _SUBMODULE.findall(text):
        used.add(parent.lower())
    return defined, used - defined

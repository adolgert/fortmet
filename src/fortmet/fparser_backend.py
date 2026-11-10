"""fparser2: syntax-tree metrics. No semantic analysis, so f(x) may be a call or an array."""
from __future__ import annotations

import math
import re
from collections import Counter

from fparser.common.readfortran import FortranStringReader
from fparser.common.sourceinfo import FortranFormat
from fparser.two import Fortran2003 as F3
from fparser.two.parser import ParserFactory
from fparser.two.utils import BlockBase, StmtBase, walk

_PARSERS: dict = {}
_PROC = ("Subroutine_Subprogram", "Function_Subprogram", "Main_Program", "Main_Program0",
         "Separate_Module_Subprogram")
_CONTAINER = ("Module", "Submodule", "Block_Data")
_NESTING = ("If_Construct", "Case_Construct", "Select_Type_Construct", "Where_Construct",
            "Forall_Construct")
_BINDING_LIST = re.compile(r"^(\s*procedure\s*(?:,[^:!]*)?::\s*)(\w+(?:\s*,\s*\w+)+)\s*(!.*)?$", re.I)
_COBRACKET = re.compile(r"(?<=[\w)])\s*\[[^\]\n]*\]")
_IMAGE_CONTROL = re.compile(
    r"^(\s*)(sync\s+(all|images|memory|team)|event\s+(post|wait)|form\s+team|fail\s+image"
    r"|lock|unlock)\b\s*(\(.*\))?\s*(!.*)?$", re.I | re.M)
_CHANGE_TEAM = re.compile(r"^(\s*)change\s+team\s*\(.*\)\s*(!.*)?$", re.I | re.M)
_END_TEAM = re.compile(r"^(\s*)end\s+team\b.*$", re.I | re.M)


def strip_coarrays(text: str) -> str:
    """Remove what fparser 0.2 cannot parse of F2008/F2018 coarrays, keeping statement counts.

    Codimensions and coindices are dropped, image-control statements become CONTINUE and
    CHANGE TEAM blocks become BLOCK constructs. None of these carry branches.
    """
    text = _COBRACKET.sub("", text)
    text = _IMAGE_CONTROL.sub(r"\1continue", text)
    text = _CHANGE_TEAM.sub(r"\1block", text)
    return _END_TEAM.sub(r"\1end block", text)


def _cls(node) -> str:
    return type(node).__name__


def _kids(node):
    if isinstance(node, (tuple, list)):
        return node
    return getattr(node, "children", None) or ()


def split_binding_lists(text: str) -> tuple[str, int]:
    """F2008 'procedure, attrs :: a, b' -> 'procedure, attrs :: a; procedure, attrs :: b'.

    fparser 0.2 only accepts the F2003 single-binding form. Same line count afterwards.
    """
    n = 0
    out = []
    for line in text.splitlines():
        m = _BINDING_LIST.match(line)
        if m:
            n += 1
            line = "; ".join(m.group(1) + name.strip() for name in m.group(2).split(","))
        out.append(line)
    return "\n".join(out) + "\n", n


def _parse(text: str, fixed: bool, std: str, include_dirs: list[str]):
    if std not in _PARSERS:
        _PARSERS[std] = ParserFactory().create(std=std)
    reader = FortranStringReader(text, include_dirs=include_dirs, ignore_comments=False)
    reader.set_format(FortranFormat(not fixed, False))
    return _PARSERS[std](reader)


def _first_stmt(unit):
    for c in unit.content:
        if isinstance(c, StmtBase) and not isinstance(c, F3.Comment):
            return c
    return None


def _unit_name(unit) -> str:
    stmt = _first_stmt(unit)
    if stmt is not None and not _cls(stmt).startswith("End_") and _cls(stmt).endswith("_Stmt"):
        if _cls(stmt) in ("Subroutine_Stmt", "Function_Stmt", "Module_Stmt", "Program_Stmt",
                          "Block_Data_Stmt", "Mp_Subprogram_Stmt", "Submodule_Stmt"):
            names = [i for i in stmt.items if isinstance(i, F3.Name)]
            if names:
                return str(names[-1] if _cls(stmt) == "Submodule_Stmt" else names[0]).lower()
    return "<main>"


def _span(unit) -> tuple[int, int]:
    lines = [s.item.span for s in walk(unit, StmtBase) if getattr(s, "item", None) is not None]
    return (min(a for a, _ in lines), max(b for _, b in lines)) if lines else (0, 0)


def _logical_ops(node, parent_op=None) -> tuple[int, int]:
    """(number of .and./.or. operators, number of runs of the same operator)."""
    ops = runs = 0
    op = None
    if not isinstance(node, (str, tuple, list)) and hasattr(node, "items") and len(node.items) == 3 \
            and isinstance(node.items[1], str) and node.items[1].upper() in (".AND.", ".OR."):
        op = node.items[1].upper()
        ops += 1
        runs += op != parent_op
    for k in _kids(node):
        if k is not None and not isinstance(k, str):
            a, b = _logical_ops(k, op)
            ops += a
            runs += b
    return ops, runs


class _Proc:
    """Accumulates metrics over one procedure body, not descending into contained procedures."""

    def __init__(self):
        self.m = Counter()
        self.operators = Counter()
        self.operands = Counter()
        self.calls: list[str] = []
        self.max_nesting = 0

    def halstead(self, node):
        if node is None:
            return
        if isinstance(node, str):
            if node:
                self.operators[node.upper()] += 1
            return
        if isinstance(node, F3.Comment):
            return
        name = _cls(node)
        if isinstance(node, F3.Name) or name.endswith("_Constant"):
            self.operands[str(node).lower()] += 1
            return
        kids = _kids(node)
        if isinstance(node, StmtBase) and not any(isinstance(k, str) and k for k in kids):
            self.operators[name] += 1
        for k in kids:
            self.halstead(k)

    def visit(self, node, depth, in_exec, top=False):
        name = _cls(node)
        if isinstance(node, F3.Comment) or isinstance(node, str) or node is None:
            return
        if not top and (name in _PROC or name in ("Internal_Subprogram_Part", "Module_Subprogram_Part")):
            return
        m = self.m
        if isinstance(node, StmtBase):
            self.halstead(node)
            if not name.startswith("End_") and name != "Contains_Stmt":
                m["exec_stmts" if in_exec else "decl_stmts"] += 1
            ops, runs = _logical_ops(node)
            m["logical_ops"] += ops
            m["cognitive"] += runs
            text = str(node).upper()
            if name in ("If_Then_Stmt", "Else_If_Stmt", "If_Stmt", "Nonlabel_Do_Stmt", "Label_Do_Stmt"):
                m["decisions"] += 1
            elif name == "Case_Stmt" and not text.startswith("CASE DEFAULT"):
                m["decisions"] += 1
            elif name == "Type_Guard_Stmt" and not text.startswith("CLASS DEFAULT"):
                m["decisions"] += 1
            elif name == "Arithmetic_If_Stmt":
                m["decisions"] += 2
            elif name == "Computed_Goto_Stmt":
                m["decisions"] += max(1, len(walk(node, F3.Label)) - 1)
            if name in ("If_Stmt", "Arithmetic_If_Stmt"):
                m["cognitive"] += 1 + depth
            elif name in ("Else_If_Stmt", "Else_Stmt"):
                m["cognitive"] += 1
            elif name in ("Goto_Stmt", "Computed_Goto_Stmt", "Assigned_Goto_Stmt"):
                m["gotos"] += 1
                m["cognitive"] += 1
            elif name in ("Return_Stmt", "Stop_Stmt", "Error_Stop_Stmt"):
                m["exits"] += 1
            elif name == "Call_Stmt":
                self.calls.append(str(node.items[0]).lower())
            elif name == "Entry_Stmt":
                m["entries"] += 1
        nests = name in _NESTING or name.endswith("Do_Construct")
        if nests:
            if name not in ("Where_Construct", "Forall_Construct"):
                m["cognitive"] += 1 + depth
            depth += 1
            self.max_nesting = max(self.max_nesting, depth)
        in_exec = in_exec or name == "Execution_Part"
        for k in _kids(node):
            if isinstance(k, (tuple, list)):
                for kk in k:
                    self.visit(kk, depth, in_exec)
            else:
                self.visit(k, depth, in_exec)

    def result(self, unit) -> dict:
        m = self.m
        start, end = _span(unit)
        n1, n2 = len(self.operators), len(self.operands)
        N1, N2 = sum(self.operators.values()), sum(self.operands.values())
        volume = (N1 + N2) * math.log2(n1 + n2) if n1 + n2 > 1 else 0.0
        difficulty = (n1 / 2) * (N2 / n2) if n2 else 0.0
        ccn = 1 + m["decisions"]
        stmts = m["exec_stmts"] + m["decl_stmts"]
        mi = 171 - 5.2 * math.log(volume or 1) - 0.23 * ccn - 16.2 * math.log(max(stmts, 1))
        stmt = _first_stmt(unit)
        args = walk(stmt, F3.Dummy_Arg_List) if stmt is not None else []
        nargs = len(args[0].items) if args else len(
            [i for i in getattr(stmt, "items", ())[2:3] if isinstance(i, F3.Name)])
        return dict(
            kind=_cls(unit).replace("_Subprogram", "").replace("Main_Program0", "Main_Program").lower(),
            start=start, end=end, args=nargs,
            exec_stmts=m["exec_stmts"], decl_stmts=m["decl_stmts"],
            ccn=ccn, ccn_ext=ccn + m["logical_ops"], cognitive=m["cognitive"],
            max_nesting=self.max_nesting, gotos=m["gotos"], exits=m["exits"], entries=m["entries"],
            call_stmts=len(self.calls), distinct_called_subs=len(set(self.calls)),
            h_n1=n1, h_n2=n2, h_N1=N1, h_N2=N2, h_volume=round(volume, 1),
            h_difficulty=round(difficulty, 1), h_effort=round(volume * difficulty),
            maintainability=round(max(0.0, mi * 100 / 171), 1),
        )


def _own_stmts(unit, cls):
    """Statements of class cls in this unit's own specification part."""
    out = []
    for part in unit.content:
        if _cls(part) in ("Specification_Part", "Implicit_Part"):
            out += walk(part, cls)
        elif isinstance(part, cls):
            out.append(part)
    return out


def _unit_info(unit, qual: str, res: dict):
    """Declaration-level facts for a module or procedure: uses, commons, implicit, types."""
    info = Counter()
    implicit = [str(s).upper() for s in _own_stmts(unit, F3.Implicit_Stmt)]
    info["implicit_none"] = int(any(s.startswith("IMPLICIT NONE") for s in implicit))
    info["implicit_rules"] = sum(not s.startswith("IMPLICIT NONE") for s in implicit)
    for use in _own_stmts(unit, F3.Use_Stmt):
        only = isinstance(use.items[3], str) and "ONLY" in use.items[3].upper()
        n_only = len(_kids(use.items[4])) if only and use.items[4] is not None else 0
        res["uses"].append(dict(unit=qual, module=str(use.items[2]).lower(), only=int(only), n_only=n_only))
        info["uses"] += 1
        info["uses_without_only"] += not only
    for common in _own_stmts(unit, F3.Common_Stmt):
        for name, objs in common.items[0]:
            members = [str(o).lower() for o in _kids(objs)]
            res["commons"].append(dict(unit=qual, block=str(name).lower() if name else "(blank)",
                                       n_members=len(members), members=" ".join(members)))
            info["common_blocks"] += 1
    for decl in _own_stmts(unit, F3.Type_Declaration_Stmt):
        base = str(decl.items[0]).split("(")[0].upper()
        if base in ("TYPE", "CLASS"):
            base = "DERIVED"
        info["decl_" + base.lower().replace(" ", "_")] += len(_kids(decl.items[2]))
        info["declared_entities"] += len(_kids(decl.items[2]))
    types = _own_stmts(unit, F3.Derived_Type_Def)
    info["derived_types"] = len(types)
    info["type_components"] = sum(len(walk(walk(t, F3.Component_Part), F3.Component_Decl)) for t in types)
    info["type_bound_procs"] = sum(
        len(walk(t, (F3.Specific_Binding, F3.Generic_Binding, F3.Final_Binding))) for t in types)
    info["interfaces"] = len(_own_stmts(unit, F3.Interface_Block))
    info["equivalences"] = len(_own_stmts(unit, F3.Equivalence_Stmt))
    info["data_stmts"] = len(_own_stmts(unit, F3.Data_Stmt))
    info["save_stmts"] = len(_own_stmts(unit, F3.Save_Stmt))
    return dict(info)


def _collect(node, scope: list[str], res: dict):
    for unit in _kids(node):
        name = _cls(unit)
        if name in _PROC or name in _CONTAINER:
            qual = "::".join(scope + [_unit_name(unit)])
            info = _unit_info(unit, qual, res)
            if name in _PROC:
                p = _Proc()
                p.visit(unit, 0, False, top=True)
                res["procs"][qual] = {**p.result(unit), **info}
            else:
                start, end = _span(unit)
                res["units"][qual] = dict(kind=name.lower(), start=start, end=end, **info)
            _collect(unit, scope + [_unit_name(unit)], res)
        elif isinstance(unit, BlockBase):
            _collect(unit, scope, res)


def analyze(text: str, fixed: bool, std: str = "f2008", include_dirs: list[str] | None = None) -> dict:
    """Parse and measure. On a syntax error retry with coarray brackets removed."""
    text, n_split = split_binding_lists(text)
    workarounds = ["split-binding-lists"] if n_split else []
    try:
        tree = _parse(text, fixed, std, include_dirs or [])
    except Exception as first:  # FortranSyntaxError, or internal errors on odd input
        stripped = strip_coarrays(text)
        try:
            if stripped == text:
                raise first
            tree = _parse(stripped, fixed, std, include_dirs or [])
            workarounds.append("stripped-coarrays")
        except Exception:
            msg = " ".join(str(first).split())
            return dict(file=dict(ok=0, error=msg[:200]), procs={}, units={}, uses=[], commons=[])
    res: dict = dict(procs={}, units={}, uses=[], commons=[])
    _collect(tree, [], res)
    stmts = [s for s in walk(tree, StmtBase) if not isinstance(s, F3.Comment)]
    res["file"] = dict(ok=1, error="", workarounds=" ".join(workarounds), statements=len(stmts),
                       procs=len(res["procs"]), units=len(res["units"]))
    return res

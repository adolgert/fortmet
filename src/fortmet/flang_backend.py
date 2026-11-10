"""flang: metrics from the compiler's parse tree and symbol table (after semantic analysis).

Runs `flang -fc1 -fdebug-dump-all` per file in module-dependency order and reads the text
dumps. Semantic analysis resolves f(x) to a call or an array element, types every symbol
(including implicitly typed ones) and lays out common blocks.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from . import sources

_PROC = ("FunctionSubprogram", "SubroutineSubprogram", "MainProgram", "SeparateModuleSubprogram")
_CONTAINER = ("Module", "Submodule", "BlockData")
_NESTING = ("IfConstruct", "DoConstruct", "CaseConstruct", "SelectTypeConstruct",
            "SelectRankConstruct", "WhereConstruct", "ForallConstruct")
_WRAPPER_STMTS = ("ActionStmt", "ImplicitPartStmt", "OtherSpecificationStmt")
_SCOPE = re.compile(r"^(\s*)(\w+) scope:\s*(\S+)?")
_NAMED_SCOPES = ("Module", "Subprogram", "MainProgram", "BlockData")


def find_flang() -> str | None:
    if os.environ.get("FORTMET_FLANG"):
        return os.environ["FORTMET_FLANG"]
    for name in ["flang-new", "flang"] + [f"flang-new-{v}" for v in range(22, 14, -1)] \
            + [f"flang-{v}" for v in range(22, 14, -1)]:
        if shutil.which(name):
            return name
    return None


class Node:
    __slots__ = ("name", "value", "kids")

    def __init__(self, name):
        self.name, self.value, self.kids = name, None, []

    def find(self, *names):
        """All descendants with one of the given names, depth first."""
        stack = list(reversed(self.kids))
        while stack:
            n = stack.pop()
            if n.name in names:
                yield n
            stack.extend(reversed(n.kids))


def parse_tree(lines: list[str]) -> Node:
    """Read flang's '| | A -> B = 'text'' indented dump into a tree."""
    root = Node("ROOT")
    stack = [(-1, root)]
    for line in lines:
        depth = 0
        while line.startswith("| ", 2 * depth):
            depth += 1
        head, sep, value = line[2 * depth:].partition(" = '")
        while stack[-1][0] >= depth:
            stack.pop()
        node = stack[-1][1]
        for name in head.split(" -> "):
            if name.strip():
                child = Node(name.strip())
                node.kids.append(child)
                node = child
        if sep:
            node.value = value[:-1]
        stack.append((depth, node))
    return root


def _unit_name(unit: Node) -> str:
    for k in unit.kids:
        if k.name.endswith("Stmt") and not k.name.startswith("End"):
            names = [n.value for n in k.kids if n.name == "Name"]
            if names:
                return names[0].lower()
            break
    return "<main>"


def _callee(call: Node) -> str:
    for d in call.find("ProcedureDesignator"):
        names = [n.value for n in d.find("Name")]
        if names:
            return names[-1].lower()
    return "?"


def _measure(unit: Node) -> tuple[dict, list[str]]:
    m = Counter()
    calls: list[str] = []
    max_nesting = 0
    head = next((k for k in unit.kids if k.name.endswith("Stmt")), None)
    if head is not None and not head.name.startswith("End"):
        m["args"] = len([k for k in head.kids if k.name == "DummyArg"]) or max(
            0, len([k for k in head.kids if k.name == "Name"]) - 1)
    stack = [(k, 0, False) for k in unit.kids]
    while stack:
        n, depth, in_exec = stack.pop()
        name = n.name
        if name in _PROC or name == "InternalSubprogramPart":
            continue
        if name.endswith("Stmt") and not name.startswith("End") and name not in _WRAPPER_STMTS \
                and name != "ContainsStmt":
            m["exec_stmts" if in_exec else "decl_stmts"] += 1
        if name in ("IfThenStmt", "ElseIfStmt", "IfStmt", "DoConstruct"):
            m["decisions"] += 1
        elif name in ("CaseStmt", "TypeGuardStmt") and not any(True for _ in n.find("Default")):
            m["decisions"] += 1
        elif name == "ArithmeticIfStmt":
            m["decisions"] += 2
        elif name == "ComputedGotoStmt":
            m["decisions"] += max(1, len([k for k in n.kids if k.name in ("uint64_t", "Label")]))
            m["gotos"] += 1
        elif name in ("GotoStmt", "AssignedGotoStmt"):
            m["gotos"] += 1
        elif name in ("ReturnStmt", "StopStmt"):
            m["exits"] += 1
        elif name in ("AND", "OR"):
            m["logical_ops"] += 1
        elif name in ("CallStmt", "FunctionReference"):
            m["call_stmts" if name == "CallStmt" else "function_refs"] += 1
            calls.append(_callee(n))
        if name in _NESTING:
            depth += 1
            max_nesting = max(max_nesting, depth)
        in_exec = in_exec or name == "ExecutionPart"
        stack.extend((k, depth, in_exec) for k in n.kids)
    ccn = 1 + m["decisions"]
    return dict(
        kind=unit.name.replace("Subprogram", "").lower(), args=m["args"],
        exec_stmts=m["exec_stmts"], decl_stmts=m["decl_stmts"], ccn=ccn,
        ccn_ext=ccn + m["logical_ops"], max_nesting=max_nesting, gotos=m["gotos"],
        exits=m["exits"], call_stmts=m["call_stmts"], function_refs=m["function_refs"],
    ), calls


def _walk_units(node: Node, scope: list[str], res: dict):
    for k in node.kids:
        if k.name in _PROC or k.name in _CONTAINER:
            name = _unit_name(k)
            qual = "::".join(scope + [name])
            if k.name in _PROC:
                res["procs"][qual], res["_calls"][qual] = _measure(k)
            else:
                res["units"][qual] = dict(kind=k.name.lower())
            _walk_units(k, scope + [name], res)
        else:
            _walk_units(k, scope, res)


def parse_symbols(lines: list[str]) -> tuple[dict, set[str]]:
    """Per named scope ('mod::proc') symbol statistics, plus the set of intrinsic names."""
    scopes: dict[str, dict] = {}
    intrinsics: set[str] = set()
    stack: list[tuple[int, str | None, dict | None]] = []  # indent, qualified name, record
    skip_indent = None
    for line in lines:
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if skip_indent is not None:
            if indent > skip_indent:
                continue
            skip_indent = None
        while stack and stack[-1][0] >= indent:
            stack.pop()
        m = _SCOPE.match(line)
        if m:
            kind, name = m.group(2), m.group(3)
            if name and re.match(r"(size|alignment|sourceRange)=", name):
                name = None
            if kind == "IntrinsicModules":
                skip_indent = indent
                continue
            parent_qual, parent_rec = (stack[-1][1], stack[-1][2]) if stack else (None, None)
            if kind in _NAMED_SCOPES and name:
                qual = f"{parent_qual}::{name}" if parent_qual else name
                rec = scopes.setdefault(qual, dict(kind=kind, c=Counter(), types=Counter(),
                                                   commons={}, callable_user=set()))
                stack.append((indent, qual, rec))
            elif kind == "DerivedType":
                stack.append((indent, parent_qual, None))  # components are not locals
            else:  # Global, construct, forall scopes: fold into the enclosing unit
                stack.append((indent, parent_qual, parent_rec))
            continue
        rec = stack[-1][2] if stack else None
        if s.startswith("Equivalence Sets:"):
            if rec:
                rec["c"]["equivalence_sets"] += s.count("(")
            continue
        if ": CommonBlockDetails" in s:
            if rec:
                name = s.split(" size=")[0].strip() if not s.startswith("size=") else ""
                rec["commons"][name or "(blank)"] = s.rsplit(":", 1)[1].split()
            continue
        head, sep, tail = s.partition(": ")
        if not sep or s.startswith((".", "__")):
            continue
        name = re.match(r"[^, ]+", head).group(0).lower()
        attrs = set(re.findall(r", ([A-Z_]+)", head))
        flags = re.search(r" \(([A-Za-z, ]+)\)", head)
        flags = set(flags.group(1).split(", ")) if flags else set()
        detail = tail.split()[0]
        if "INTRINSIC" in attrs:
            intrinsics.add(name)
        if rec is None:
            continue
        c = rec["c"]
        c["implicitly_typed"] += "Implicit" in flags
        if detail == "ObjectEntity":
            t = re.search(r"type: (.+?)(?: shape:| coshape:| init:| len:|$)", tail)
            base = t.group(1).split("(")[0] if t else "?"
            rec["types"]["DERIVED" if base in ("TYPE", "CLASS") else base] += 1
            role = "dummies" if " dummy " in tail + " " else "parameters" if "PARAMETER" in attrs else "variables"
            c[role] += 1
            c["arrays"] += " shape:" in tail
            c["coarrays"] += " coshape:" in tail
            c["allocatables"] += "ALLOCATABLE" in attrs
            c["pointers"] += "POINTER" in attrs
            if role == "dummies" and not any(a == "INTENT" for a in attrs) and "VALUE" not in attrs:
                c["dummies_without_intent"] += 1
            if rec["kind"] == "Module":
                c["public" if "PUBLIC" in attrs else "private"] += 1
        elif detail == "Use":
            c["use_associated"] += 1
        elif detail == "HostAssoc":
            c["host_associated"] += 1
        elif detail == "DerivedType":
            c["derived_types"] += 1
        elif detail == "Generic":
            c["generics"] += 1
        elif detail == "Subprogram":
            c["procedures"] += 1
        elif detail == "Namelist":
            c["namelists"] += 1
    return scopes, intrinsics


def _symbol_columns(rec: dict) -> dict:
    c = rec["c"]
    keys = ["variables", "dummies", "parameters", "arrays", "allocatables", "pointers", "coarrays",
            "implicitly_typed", "dummies_without_intent", "use_associated", "host_associated",
            "equivalence_sets"]
    if rec["kind"] == "Module":
        keys += ["public", "private", "derived_types", "generics", "procedures", "namelists"]
    out = {"sym_" + k: c[k] for k in keys}
    out["sym_common_blocks"] = len(rec["commons"])
    out["sym_common_members"] = sum(len(v) for v in rec["commons"].values())
    out["sym_types"] = " ".join(f"{k}:{v}" for k, v in sorted(rec["types"].items()))
    return out


def _order(files: list[Path], texts: dict[Path, str]) -> tuple[list[Path], dict[Path, list[Path]]]:
    """Topological order by module use. A module in the same directory wins over others."""
    defined, used = {}, {}
    providers = defaultdict(list)
    for f in files:
        defined[f], used[f] = sources.module_deps(texts[f])
        for mod in defined[f]:
            providers[mod].append(f)
    deps = {}
    for f in files:
        deps[f] = []
        for mod in sorted(used[f]):
            cands = providers.get(mod, [])
            same = [p for p in cands if p.parent == f.parent]
            if same or cands:
                deps[f].append((same or cands)[0])
    order, state = [], {}

    def visit(f):
        if state.get(f):
            return
        state[f] = 1
        for d in deps[f]:
            visit(d)
        order.append(f)

    for f in files:
        visit(f)
    return order, deps


def analyze(files: list[Path], texts: dict[Path, str], cpp: dict[Path, bool], defines: list[str],
            includes: list[str], workdir: Path, flang: str | None = None) -> dict[Path, dict]:
    flang = flang or find_flang()
    if not flang:
        return {f: dict(file=dict(ok=0, error="flang not found"), procs={}, units={}, calls=[], commons=[])
                for f in files}
    order, deps = _order(files, texts)
    dirs = sorted({f.parent for f in files})
    moddir = {d: (workdir / "mod" / str(i)).resolve() for i, d in enumerate(dirs)}
    for d in moddir.values():
        d.mkdir(parents=True, exist_ok=True)
    out = {}
    for f in order:
        md = moddir[f.parent]
        base = [flang, "-fc1", "-module-dir", str(md)]
        base += [f"-I{moddir[d.parent]}" for d in deps[f] if d.parent != f.parent]
        base += [f"-I{Path(i).resolve()}" for i in includes] + [f"-D{d}" for d in defines]
        if cpp[f]:
            base.append("-cpp")
        if sources.is_fixed(f):
            base.append("-ffixed-form")
        r = subprocess.run(base + ["-fdebug-dump-all", str(f)], capture_output=True, text=True, cwd=md)
        sema, error = 1, ""
        if r.returncode != 0 or "parse tree dump" not in r.stdout:
            errs = [l for l in r.stderr.splitlines() if "error:" in l and "Semantic errors in" not in l]
            error = (errs[0] if errs else r.stderr.strip()[:200]).replace(str(f.parent) + "/", "")
            sema = 0
            r = subprocess.run(base + ["-fdebug-dump-parse-tree-no-sema", str(f)],
                               capture_output=True, text=True, cwd=md)
            if r.returncode != 0:
                out[f] = dict(file=dict(ok=0, sema=0, error=error), procs={}, units={}, calls=[], commons=[])
                continue
        tree_lines, sym_lines = [], []
        section = tree_lines
        for line in r.stdout.splitlines():
            if line.startswith("====="):
                section = sym_lines if "symbols dump" in line else tree_lines
            else:
                section.append(line)
        res = dict(procs={}, units={}, _calls={}, calls=[], commons=[])
        res["_tree"], res["_sym_lines"] = parse_tree(tree_lines), sym_lines
        _walk_units(res["_tree"], [], res)
        scopes, intrinsics = parse_symbols(sym_lines)
        for qual, rec in {**res["procs"], **res["units"]}.items():
            if qual in scopes:
                rec.update(_symbol_columns(scopes[qual]))
                for block, members in scopes[qual]["commons"].items():
                    res["commons"].append(dict(unit=qual, block=block, n_members=len(members),
                                               members=" ".join(members)))
        for qual, calls in res.pop("_calls").items():
            counts = Counter(calls)
            user = [c for c in counts if c not in intrinsics] if sema else list(counts)
            res["procs"][qual]["fan_out"] = len(user)
            res["procs"][qual]["intrinsics_called"] = len(counts) - len(user)
            for callee, n in sorted(counts.items()):
                res["calls"].append(dict(caller=qual, callee=callee, count=n,
                                         intrinsic=int(callee in intrinsics)))
        res["file"] = dict(ok=1, sema=sema, error=error, procs=len(res["procs"]), units=len(res["units"]))
        out[f] = res
    return out

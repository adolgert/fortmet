"""Reference extractor: which routine reads or writes which variable, and who calls whom.

Built on flang's post-semantics parse tree (roles: read, write, passed to a call) and symbol
table (what each name is and where it lives). Names are resolved here by walking flang's
scopes, following USE and host association. Results are per routine:

  accesses  routine -> variable, with reads / writes / maybe_writes
  calls     routine -> procedure, with the kind of call and how certain the target is

Identifiers are 'module::routine::name'. A COMMON member is 'common:block+offset', because
storage position, not the local name, is what two routines share.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .flang_backend import _PROC, Node, _unit_name

_SCOPE = re.compile(r"^(\s*)(\w+) scope:\s*(\S+)?")
_SIZE = re.compile(r" size=(\d+) offset=(\d+)")
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_CALLED = re.compile(r"(?<![%\w.])([a-z_]\w*)\(")
_DESIGNATOR = re.compile(r"([a-z_]\w*)((%\w+)|(\([^()]*\)))*$")
_IO = ("ReadStmt", "WriteStmt", "PrintStmt", "OpenStmt", "CloseStmt", "InquireStmt",
       "RewindStmt", "BackspaceStmt", "EndfileStmt", "FlushStmt")
_VAR_DETAILS = ("ObjectEntity", "AssocEntity")
_HIDDEN = ("module", "common", "host")
_INTRINSIC_MODULES = ("iso_fortran_env", "iso_c_binding", "omp_lib", "openacc")


@dataclass
class Sym:
    name: str
    attrs: set
    flags: set
    detail: str
    tail: str
    size: int = 0
    offset: int = 0


@dataclass
class Scope:
    kind: str
    name: str | None
    parent: "Scope | None"
    syms: dict = field(default_factory=dict)
    construct: dict = field(default_factory=dict)   # names from nested construct scopes
    kids: list = field(default_factory=list)
    commons: dict = field(default_factory=dict)     # block -> member names
    equivalenced: set = field(default_factory=set)

    @property
    def path(self) -> str:
        if self.parent is None or self.name is None:
            return ""
        up = self.parent.path
        return f"{up}::{self.name}" if up else self.name


def parse_symtab(lines: list[str]) -> Scope:
    """Read flang's symbol dump into a scope tree. Intrinsic modules are skipped."""
    root = Scope("Global", None, None)
    stack: list[tuple[int, Scope]] = []
    skip = None
    for line in lines:
        s = line.strip()
        if not s:
            continue
        indent = len(line) - len(line.lstrip())
        if skip is not None:
            if indent > skip:
                continue
            skip = None
        while stack and stack[-1][0] >= indent:
            stack.pop()
        m = _SCOPE.match(line)
        if m:
            kind, name = m.group(2), m.group(3)
            if name and re.match(r"(size|alignment|sourceRange)=", name):
                name = None
            if kind == "IntrinsicModules" or (name and name.startswith(".")):
                skip = indent
            elif kind == "Global":
                stack.append((indent, root))
            else:
                parent = stack[-1][1] if stack else root
                if name is None:   # block, associate, forall: names belong to the routine
                    scope = Scope(kind, None, parent)
                    scope.syms = parent.construct if parent.name else parent.syms
                    scope.construct = scope.syms
                    scope.commons, scope.kids = parent.commons, parent.kids
                    scope.name, scope.parent = parent.name, parent.parent
                    scope.kind = parent.kind
                else:
                    scope = Scope(kind, name.lower(), parent)
                    parent.kids.append(scope)
                stack.append((indent, scope))
            continue
        if not stack:
            continue
        scope = stack[-1][1]
        if s.startswith("Equivalence Sets:"):
            scope.equivalenced |= set(re.findall(r"[a-z_]\w*", s.split(":", 1)[1]))
            continue
        if ": CommonBlockDetails" in s:
            block = "" if s.startswith("size=") else s.split(" size=")[0].strip()
            scope.commons[block.lower()] = s.rsplit(":", 1)[1].split()
            continue
        head, sep, tail = s.partition(": ")
        if not sep or s.startswith((".", "__")):
            continue
        name = re.match(r"[^, ]+", head).group(0).lower()
        flags = re.search(r" \(([A-Za-z, ]+)\)", head)
        sym = Sym(name, set(re.findall(r", ([A-Z_]+)", head)),
                  set(flags.group(1).split(", ")) if flags else set(), tail.split()[0], tail)
        intent = re.search(r"INTENT\((\w+)\)", head)
        if intent:
            sym.attrs.add("INTENT_" + intent.group(1))
        size = _SIZE.search(head)
        if size:
            sym.size, sym.offset = int(size.group(1)), int(size.group(2))
        scope.syms.setdefault(name, sym)
    return root


class Resolver:
    """Name lookup over one file's scope tree."""

    def __init__(self, root: Scope):
        self.root = root
        self.modules = {k.name: k for k in root.kids if k.kind == "Module"}
        self.by_path: dict[str, Scope] = {}
        self.types: dict[str, list[Scope]] = defaultdict(list)
        todo = list(root.kids)
        while todo:
            s = todo.pop()
            self.by_path[s.path] = s
            if s.kind == "DerivedType":
                self.types[s.name].append(s)
            todo.extend(s.kids)

    def resolve(self, scope: Scope | None, name: str, depth: int = 0):
        """Return (owning scope, symbol), or a string id if the owner is not in this dump."""
        while scope is not None:
            sym = scope.syms.get(name) or scope.construct.get(name)
            if sym is None or sym.detail == "HostAssoc":
                scope = scope.parent
                continue
            if sym.detail == "Use":
                m = re.match(r"Use from (\S+) in (\S+)", sym.tail)
                mod = self.modules.get(m.group(2))
                if mod is None or depth > 8:
                    return f"{m.group(2)}::{m.group(1)}"
                return self.resolve(mod, m.group(1), depth + 1)
            return scope, sym
        return None

    def dummies(self, scope: Scope) -> list[str]:
        decl = scope.parent.syms.get(scope.name) if scope.parent and scope.name else None
        if decl is None or not decl.tail.rstrip().endswith(")"):
            return []
        text, depth, start = decl.tail.rstrip(), 0, 0
        for i in range(len(text) - 1, -1, -1):
            depth += (text[i] == ")") - (text[i] == "(")
            if depth == 0:
                start = i
                break
        parts, depth, cur = [], 0, ""
        for ch in text[start + 1:-1]:
            depth += (ch == "(") - (ch == ")")
            if ch == "," and depth == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        return [p.split()[-1].lower() for p in parts if p.strip()]

    def type_scope(self, scope: Scope, type_name: str) -> Scope | None:
        r = self.resolve(scope, type_name)
        if r and not isinstance(r, str):
            for k in r[0].kids:
                if k.kind == "DerivedType" and k.name == type_name:
                    return k
        cands = self.types.get(type_name, [])
        return cands[0] if cands else None

    def parent_type(self, t: Scope) -> Scope | None:
        for sym in t.syms.values():
            if "ParentComp" in sym.flags:
                return self.type_scope(t.parent, sym.name)
        return None

    def find_in_type(self, t: Scope | None, name: str):
        seen = 0
        while t is not None and seen < 20:
            if name in t.syms and "ParentComp" not in t.syms[name].flags:
                return t, t.syms[name]
            t, seen = self.parent_type(t), seen + 1
        return None


def _sym_type(sym: Sym):
    m = re.search(r"type: (TYPE|CLASS)\((\w+)", sym.tail)
    return (m.group(2).lower(), m.group(1) == "CLASS") if m else (None, False)


def _base_name(node: Node) -> str | None:
    """Leftmost name of a designator: 'a' in a(i)%b(j)."""
    while node is not None:
        if node.name == "Name":
            return node.value.lower() if node.value else None
        node = node.kids[0] if node.kids else None
    return None


class _Routine:
    """Walks one routine's executable statements and records accesses and call sites."""

    def __init__(self, res: Resolver, scope: Scope, qual: str):
        self.res, self.scope, self.qual = res, scope, qual
        self.accesses: list[dict] = []
        self.sites: list[dict] = []
        self.counts = Counter()
        self.typed_names: set[str] = set()
        self.hint: str | None = None   # resolved procedure name in the enclosing typed expression

    # -- name -> target ---------------------------------------------------------------
    def target(self, name: str, depth: int = 0):
        r = self.res.resolve(self.scope, name)
        if r is None:
            return None
        if isinstance(r, str):   # owner not in the dump: an intrinsic module's named constant
            intrinsic = r.split("::")[0] in _INTRINSIC_MODULES or r.startswith("ieee_")
            return dict(id=r, kind="parameter" if intrinsic else "module", type="", saved=0)
        owner, sym = r
        if sym.detail == "AssocEntity" and depth < 5:
            expr = sym.tail.split("expr:", 1)[-1].strip() if "expr:" in sym.tail else ""
            m = _DESIGNATOR.match(expr)
            if m and m.group(1) != name:
                return self.target(m.group(1), depth + 1)   # alias of another variable
        if sym.detail not in _VAR_DETAILS:
            if sym.detail in ("Subprogram", "ProcEntity", "Generic"):
                return dict(id=self.proc_id(owner, sym), kind="procedure", type="", saved=0)
            return None
        t = re.search(r"type: (.+?)(?: shape:| coshape:| init:| len:| expr:|$)", sym.tail)
        out = dict(id=f"{owner.path}::{name}" if owner.path else name,
                   type=t.group(1) if t else "", saved=0)
        block = next((b for b, members in owner.commons.items() if name in members), None)
        if "PARAMETER" in sym.attrs:
            out["kind"] = "parameter"
        elif block is not None:
            out.update(kind="common", id=f"common:{block or '(blank)'}+{sym.offset}",
                       block=block or "(blank)", offset=sym.offset, size=sym.size)
        elif owner.kind == "Module":
            out["kind"] = "module"
        elif owner is self.scope or owner.path == self.scope.path:
            out["kind"] = ("dummy" if " dummy " in sym.tail + " " else
                           "result" if "funcResult" in sym.tail else "local")
            if out["kind"] == "local" and owner.kind != "MainProgram" and (
                    "SAVE" in sym.attrs or " init:" in sym.tail):
                out["saved"] = 1
        else:
            out["kind"] = "host"
        if name in owner.equivalenced:
            out["equivalenced"] = 1
        return out

    def proc_id(self, owner: Scope, sym: Sym) -> str:
        if "POINTER" in sym.attrs or (owner.kind == "Subprogram" and sym.name in self.res.dummies(owner)):
            return f"{owner.path}::{sym.name}"   # procedure pointer or dummy procedure
        external = sym.detail == "ProcEntity" or "isInterface" in sym.tail
        return sym.name if external or not owner.path else f"{owner.path}::{sym.name}"

    def ref(self, name: str | None, role: str | None) -> int | None:
        if not name:
            return None
        t = self.target(name.lower())
        if t is None:
            return None
        self.accesses.append(dict(name=name.lower(), role=role, **t))
        return len(self.accesses) - 1

    # -- tree walk --------------------------------------------------------------------
    def walk(self, n: Node, role: str = "r"):
        nm = n.name
        if nm in _PROC or nm == "InternalSubprogramPart":
            return
        if n.value and nm in ("AssignmentStmt", "CallStmt", "Expr"):
            self.typed_names |= set(_CALLED.findall(_QUOTED.sub("", n.value)))
        if n.value and nm in ("CallStmt", "Expr"):
            m = re.match(r"(?:CALL )?([a-z_]\w*)\(", n.value)
            self.hint = m.group(1) if m else None
        if nm == "Name":
            self.ref(n.value, role)
        elif nm == "Keyword":
            return
        elif nm == "StructureComponent":
            self.walk(n.kids[0], role)          # the component name is not a variable
        elif nm in ("Variable", "AllocateObject", "PointerObject"):
            self.first_gets(n.kids, "w")
        elif nm == "Selector":                   # associate(x => var): not a write
            for k in n.kids:
                self.first_gets(k.kids, "r") if k.name == "Variable" else self.walk(k)
        elif nm == "Association":                # the associate name itself is not a reference
            for k in n.kids[1:]:
                self.walk(k)
        elif nm == "PointerAssignmentStmt":
            self.walk(n.kids[0], "w")
            for k in n.kids[1:]:
                self.walk(k)
        elif nm == "LoopBounds":
            self.first_gets(n.kids, "w")
        elif nm in ("CallStmt", "FunctionReference"):
            self.call(n)
        elif nm in _IO:
            self.counts["io_stmts"] += 1
            self.io(n)
        else:
            if nm == "StopStmt":
                self.counts["stop_stmts"] += 1
            self.first_gets(n.kids, role)

    def first_gets(self, kids: list[Node], role: str):
        """The base of a designator takes the role; subscripts and the rest are reads."""
        for i, k in enumerate(kids):
            self.walk(k, role if i == 0 else "r")

    def io(self, n: Node):
        reading = n.name == "ReadStmt"
        for k in n.kids:
            if k.name == "IoUnit" and k.kids and k.kids[0].name == "Variable":
                self.first_gets(k.kids[0].kids, "r" if reading else "w")   # internal file
            elif k.name == "IoControlSpec" and k.kids and k.kids[0].name in ("Name", "Namelist"):
                group = _base_name(k.kids[0])                                # namelist group
                r = self.res.resolve(self.scope, group) if group else None
                if r and not isinstance(r, str) and r[1].detail.startswith("Namelist"):
                    saved, self.scope = self.scope, r[0]
                    for member in r[1].tail.split(":", 1)[1].split(","):
                        self.ref(member.strip(), "w" if reading else "r")
                    self.scope = saved
            else:
                self.walk(k)

    def call(self, n: Node):
        call = n if n.name == "Call" else next((k for k in n.kids if k.name == "Call"), None)
        if call is None or not call.kids:
            return
        site = dict(function=int(n.name == "FunctionReference"), args=[], targets=[],
                    type=None, binding=None, poly=0, pass_name=None, nopass=0)
        self.designator(call.kids[0], site)
        pos = 0
        for spec in call.kids[1:]:
            if spec.name != "ActualArgSpec":
                continue
            keyword, arg = None, None
            for k in spec.kids:
                if k.name == "Keyword":
                    keyword = _base_name(k)
                elif k.name == "ActualArg":
                    arg = k
            idx = None
            expr = arg.kids[0] if arg is not None and arg.kids else None
            if expr is not None and expr.name == "Expr" and len(expr.kids) == 1 \
                    and expr.kids[0].name == "Designator":
                before = len(self.accesses)
                self.first_gets(expr.kids, None)      # role decided once the callee is known
                if len(self.accesses) > before and self.accesses[before]["role"] is None:
                    idx = before
            elif expr is not None:
                self.walk(expr)
            site["args"].append(dict(pos=None if keyword else pos, keyword=keyword, access=idx))
            pos += keyword is None
        self.sites.append(site)

    def designator(self, pd: Node, site: dict):
        comp = next(pd.find("ProcComponentRef"), None)
        if comp is None:
            name = _base_name(pd)
            site["name"] = name
            self.classify(name, site)
            return
        sc = next(comp.find("StructureComponent"))
        site["name"] = site["binding"] = sc.kids[1].value.lower()
        before = len(self.accesses)
        self.walk(sc.kids[0], None)
        site["pass_access"] = before if len(self.accesses) > before else None
        # Type of the object expression: follow components from the base variable.
        chain, node = [], sc.kids[0]
        while node is not None and node.name != "Name":
            if node.name == "StructureComponent":
                chain.append(node.kids[1].value.lower())
            node = node.kids[0] if node.kids else None
        r = self.res.resolve(self.scope, node.value.lower()) if node is not None else None
        tscope, poly = None, False
        if r and not isinstance(r, str):
            tname, poly = _sym_type(r[1])
            tscope = self.res.type_scope(r[0], tname) if tname else None
            for comp_name in reversed(chain):
                found = self.res.find_in_type(tscope, comp_name)
                if not found:
                    tscope = None
                    break
                tname, poly = _sym_type(found[1])
                tscope = self.res.type_scope(found[0], tname) if tname else None
        found = self.res.find_in_type(tscope, site["binding"]) if tscope else None
        if not found:
            site["kind"] = "unresolved"
            return
        site["type"], site["poly"] = tscope.path, int(poly)
        owner, sym = found
        if sym.detail == "ProcBinding":
            self.binding(owner, sym, site)
            site["kind"] = "type-bound"
        elif sym.detail == "Generic":
            for b in sym.tail.split("procs:", 1)[-1].split(","):
                f = self.res.find_in_type(tscope, b.strip().lower())
                if f and f[1].detail == "ProcBinding":
                    self.binding(f[0], f[1], site)
            site["kind"] = "type-bound"
            site["generic"] = 1
        else:
            site["kind"] = "procedure-pointer"

    def binding(self, type_scope: Scope, sym: Sym, site: dict):
        m = re.match(r"ProcBinding => (\S+)", sym.tail)
        p = re.search(r"passName:(\S+)", sym.tail)
        site["pass_name"] = p.group(1).lower() if p else site["pass_name"]
        site["nopass"] = int("NOPASS" in sym.attrs)
        r = self.res.resolve(type_scope.parent, m.group(1).lower()) if m else None
        if "DEFERRED" in sym.attrs or r is None:
            return
        tid = r if isinstance(r, str) else self.proc_id(r[0], r[1])
        if tid not in site["targets"]:
            site["targets"].append(tid)

    def classify(self, name: str | None, site: dict):
        r = self.res.resolve(self.scope, name) if name else None
        if r is None:
            site.update(kind="unresolved", targets=[f"?{name}"])
        elif isinstance(r, str):
            site.update(kind="direct", targets=[r])
        else:
            owner, sym = r
            if sym.detail == "Generic":
                cands = []
                for p in sym.tail.split("procs:", 1)[-1].split(","):
                    q = self.res.resolve(owner, p.strip().lower())
                    if q is not None:
                        cands.append(q if isinstance(q, str) else self.proc_id(q[0], q[1]))
                exact = [c for c in cands if c.split("::")[-1] == self.hint]
                site.update(kind="generic", targets=exact or cands)
            elif sym.detail == "ProcEntity" and "INTRINSIC" in sym.attrs:
                site.update(kind="intrinsic", targets=[name])
            elif name in self.res.dummies(owner) and owner.kind == "Subprogram":
                site.update(kind="dummy-procedure", targets=[f"{owner.path}::{name}"])
            elif "POINTER" in sym.attrs:
                site.update(kind="procedure-pointer", targets=[f"{owner.path}::{name}"])
            elif sym.detail in ("Subprogram", "ProcEntity"):
                site.update(kind="direct", targets=[self.proc_id(owner, sym)])
            else:
                site.update(kind="unresolved", targets=[f"?{name}"])

    def finish(self):
        """Use flang's typed expression text to resolve generics and find operator calls."""
        named = {s["name"] for s in self.sites}
        generic_cands = set()
        for s in self.sites:
            if s["kind"] == "generic" or s.get("generic"):
                generic_cands |= set(s["targets"])
                hit = [t for t in s["targets"] if t.split("::")[-1] in self.typed_names]
                if hit:
                    s["targets"] = hit
        for name in sorted(self.typed_names - named):
            r = self.res.resolve(self.scope, name)
            if r is None:   # e.g. a private specific of a generic from another module
                owners = [(m, m.syms[name]) for m in self.res.modules.values() if name in m.syms]
                r = owners[0] if len(owners) == 1 else None
            if r is None or isinstance(r, str) or r[1].detail != "Subprogram":
                continue
            tid = self.proc_id(r[0], r[1])
            if tid not in generic_cands and tid != self.qual:
                self.sites.append(dict(name=name, kind="defined-operation", targets=[tid],
                                       args=[], function=1, poly=0, type=None, binding=None))


def _walk_routines(node: Node, scope_path: list[str], out: list):
    for k in node.kids:
        if k.name in _PROC or k.name in ("Module", "Submodule"):
            name = _unit_name(k)
            if k.name in _PROC:
                out.append(("::".join(scope_path + [name]), k))
            _walk_routines(k, scope_path + [name], out)
        else:
            _walk_routines(k, scope_path, out)


def extract_file(tree: Node, sym_lines: list[str]) -> dict:
    """Phase 1 for one file: raw accesses and call sites per routine, plus lookup tables."""
    res = Resolver(parse_symtab(sym_lines))
    routines: list = []
    _walk_routines(tree, [], routines)
    out = dict(routines={}, procs={}, types={})
    for path, scope in res.by_path.items():
        if scope.kind in ("Subprogram", "MainProgram"):
            intents = {}
            for d in res.dummies(scope):
                sym = scope.syms.get(d)
                attrs = sym.attrs if sym else set()
                intents[d] = next((a[7:] for a in attrs if a.startswith("INTENT_")),
                                  "IN" if "VALUE" in attrs else "")
            decl = scope.parent.syms.get(scope.name) if scope.parent else None
            out["procs"][path] = dict(dummies=res.dummies(scope), intents=intents,
                                      pure=int(bool(decl and "PURE" in decl.attrs)))
        elif scope.kind == "DerivedType":
            parent = res.parent_type(scope)
            bindings = {}
            for sym in scope.syms.values():
                if sym.detail == "ProcBinding" and "DEFERRED" not in sym.attrs:
                    m = re.match(r"ProcBinding => (\S+)", sym.tail)
                    r = res.resolve(scope.parent, m.group(1).lower())
                    if r is not None:
                        bindings[sym.name] = r if isinstance(r, str) else (
                            f"{r[0].path}::{r[1].name}" if r[0].path else r[1].name)
            out["types"][path] = dict(parent=parent.path if parent else None, bindings=bindings)
    for qual, node in routines:
        scope = res.by_path.get(qual)
        if scope is None:
            continue
        r = _Routine(res, scope, qual)
        for k in node.kids:
            if k.name == "ExecutionPart":
                r.walk(k)
        r.finish()
        syms = scope.syms.values()
        r.counts["inout_dummies"] = sum("INTENT_INOUT" in s.attrs for s in syms)
        r.counts["saved_locals"] = sum(
            s.detail == "ObjectEntity" and "PARAMETER" not in s.attrs and " dummy " not in s.tail + " "
            and ("SAVE" in s.attrs or " init:" in s.tail) for s in syms) if scope.kind != "MainProgram" else 0
        out["routines"][qual] = dict(accesses=r.accesses, sites=r.sites, counts=dict(r.counts))
    return out


# Argument intents of the standard intrinsic subroutines (intrinsic functions only read).
_INTRINSIC_SUBS = {
    "random_number": "harvest:w", "random_seed": "size:w put:r get:w", "cpu_time": "time:w",
    "system_clock": "count:w count_rate:w count_max:w",
    "date_and_time": "date:w time:w zone:w values:w",
    "get_command": "command:w length:w status:w errmsg:rw",
    "get_command_argument": "number:r value:w length:w status:w errmsg:rw",
    "get_environment_variable": "name:r value:w length:w status:w trim_name:r errmsg:rw",
    "execute_command_line": "command:r wait:r exitstat:rw cmdstat:w cmdmsg:rw",
    "move_alloc": "from:rw to:w stat:w errmsg:rw", "mvbits": "from:r frompos:r len:r to:rw topos:r",
    "co_sum": "a:rw result_image:r stat:w errmsg:rw", "co_max": "a:rw result_image:r stat:w errmsg:rw",
    "co_min": "a:rw result_image:r stat:w errmsg:rw",
    "co_reduce": "a:rw operation:r result_image:r stat:w errmsg:rw",
    "co_broadcast": "a:rw source_image:r stat:w errmsg:rw",
    "event_query": "event:r count:w stat:w", "atomic_define": "atom:w value:r stat:w",
    "atomic_ref": "value:w atom:r stat:w",
}


def _arg_role(site: dict, arg: dict, procs: dict, is_pass: bool = False) -> str:
    if site["kind"] == "intrinsic":
        if site["function"]:
            return "r"
        spec = [x.split(":") for x in _INTRINSIC_SUBS.get(site["name"], "").split()]
        if arg["keyword"]:
            return dict(spec).get(arg["keyword"], "maybe")
        return spec[arg["pos"]][1] if arg["pos"] is not None and arg["pos"] < len(spec) else "maybe"
    if site["kind"] in ("dummy-procedure", "procedure-pointer", "unresolved") or not site["targets"]:
        return "maybe"
    roles = set()
    for t in site["targets"]:
        info = procs.get(t)
        if info is None:
            roles.add("maybe")
            continue
        dummies = list(info["dummies"])
        passed = None
        if site.get("binding") and not site.get("nopass") and dummies:
            passed = site.get("pass_name") or dummies[0]
        if is_pass:
            d = passed
        elif arg["keyword"]:
            d = arg["keyword"]
        else:
            rest = [x for x in dummies if x != passed]
            d = rest[arg["pos"]] if arg["pos"] is not None and arg["pos"] < len(rest) else None
        roles.add({"IN": "r", "OUT": "w", "INOUT": "rw"}.get(info["intents"].get(d, ""), "maybe"))
    return roles.pop() if len(roles) == 1 else "maybe"


def analyze(files: list[Path], flang_results: dict[Path, dict], rel: dict[Path, str]) -> dict:
    """Phase 2: join files into programs, settle argument roles and dispatch, aggregate."""
    per_file = {}
    for f in files:
        r = flang_results.get(f, {})
        if r.get("_tree") is not None and r.get("file", {}).get("sema"):
            per_file[f] = extract_file(r["_tree"], r["_sym_lines"])
    # One program per directory if any routine id is defined twice, else one program overall.
    seen, dup = set(), False
    for f, x in per_file.items():
        dup = dup or bool(seen & set(x["routines"]))
        seen |= set(x["routines"])
    group = {f: (f.parent if dup else None) for f in per_file}
    accesses, calls, summary = [], [], {}
    for g in set(group.values()):
        members = [f for f in per_file if group[f] == g]
        procs, types, defined = {}, {}, set()
        for f in members:
            procs.update(per_file[f]["procs"])
            types.update(per_file[f]["types"])
            defined |= set(per_file[f]["routines"])
        children = defaultdict(list)
        for t, info in types.items():
            if info["parent"]:
                children[info["parent"]].append(t)
        fan_in = Counter()
        rows = []
        for f in members:
            for qual, rt in per_file[f]["routines"].items():
                acc = rt["accesses"]
                edges = Counter()
                c = Counter(rt["counts"])
                for site in rt["sites"]:
                    targets = list(site["targets"])
                    if site["kind"] == "type-bound" and site["poly"] and site["type"]:
                        todo = list(children[site["type"]])
                        while todo:   # overriding bindings in extensions
                            t = todo.pop()
                            o = types[t]["bindings"].get(site["binding"])
                            if o and o not in targets:
                                targets.append(o)
                            todo.extend(children[t])
                        site["targets"] = targets
                    for a in site["args"]:
                        if a["access"] is not None:
                            acc[a["access"]]["role"] = _arg_role(site, a, procs)
                    if site.get("pass_access") is not None:
                        acc[site["pass_access"]]["role"] = _arg_role(site, {}, procs, is_pass=True)
                    kind = site["kind"]
                    if kind == "direct" and targets[0] not in procs and targets[0] not in defined:
                        kind = "external"   # no definition among the analysed files
                    certain = int(len(targets) == 1 and kind in (
                        "direct", "intrinsic", "generic", "type-bound", "defined-operation", "external"))
                    c["dispatch_sites"] += kind in ("dummy-procedure", "procedure-pointer") or (
                        kind == "type-bound" and len(targets) > 1)
                    c["unresolved_calls"] += kind == "unresolved" or not targets
                    for t in targets or [f"?{site['name']}"]:
                        edges[(t, kind, certain)] += 1
                for (t, kind, certain), n in sorted(edges.items()):
                    calls.append(dict(file=rel[f], caller=qual, callee=t, kind=kind, count=n,
                                      certain=certain))
                callees = {t for (t, kind, _) in edges if kind != "intrinsic"}
                for t in callees:
                    fan_in[t] += 1
                agg: dict[str, dict] = {}
                for a in acc:
                    row = agg.setdefault(a["id"], dict(
                        file=rel[f], routine=qual, target=a["id"], kind=a["kind"], reads=0, writes=0,
                        maybe_writes=0, type=a.get("type", ""), saved=a.get("saved", 0),
                        equivalenced=a.get("equivalenced", 0), block=a.get("block", ""),
                        offset=a.get("offset", ""), size=a.get("size", "")))
                    role = a["role"] or "r"
                    row["reads"] += role in ("r", "rw", "maybe")
                    row["writes"] += role in ("w", "rw")
                    row["maybe_writes"] += role == "maybe"
                accesses += agg.values()
                hidden = [r for r in agg.values() if r["kind"] in _HIDDEN]
                rows.append((rel[f], qual, dict(
                    hidden_inputs=sum(r["reads"] > 0 for r in hidden),
                    hidden_outputs=sum(r["writes"] > 0 for r in hidden),
                    hidden_maybe_outputs=sum(r["maybe_writes"] > 0 and not r["writes"] for r in hidden),
                    inout_dummies=c["inout_dummies"], saved_locals=c["saved_locals"],
                    io_stmts=c["io_stmts"], stop_stmts=c["stop_stmts"],
                    fan_out=len(callees), dispatch_sites=c["dispatch_sites"],
                    unresolved_calls=c["unresolved_calls"])))
        for file, qual, rec in rows:
            rec["fan_in"] = fan_in[qual]
            summary[(file, qual)] = rec
    return dict(accesses=accesses, calls=calls, summary=summary,
                skipped=[rel[f] for f in files if f not in per_file])

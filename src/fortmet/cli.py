"""Command line: run every backend over a source tree and write merged tables."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

from . import sources

PREFIX = {"lizard": "lz_", "fparser": "fp_", "flang": "fl_"}


def _write_csv(path: Path, rows: list[dict], lead: list[str]):
    cols = list(lead)
    for r in rows:
        cols += [k for k in r if k not in cols]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, restval="")
        w.writeheader()
        w.writerows(rows)


def _merge(table: dict, key: tuple, prefix: str, rec: dict):
    table.setdefault(key, {}).update({prefix + k: v for k, v in rec.items()})


def run(args) -> int:
    files = sources.discover(args.paths)
    if not files:
        print("fortmet: no Fortran files found", file=sys.stderr)
        return 1
    root = Path(os.path.commonpath([f.parent for f in files]))
    rel = {f: str(f.relative_to(root)) for f in files}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tools = [t for t in args.tools.split(",") if t]

    file_rows = {rel[f]: {} for f in files}
    procs: dict[tuple, dict] = {}
    units: dict[tuple, dict] = {}
    calls, uses, commons = [], [], []
    texts, pp_texts, cpp = {}, {}, {}
    for f in files:
        text = texts[f] = f.read_text(errors="replace")
        row = file_rows[rel[f]]
        cpp[f] = args.cpp == "always" or (args.cpp == "auto" and sources.needs_cpp(f, text))
        pp_texts[f] = text
        row["preprocessed"] = int(cpp[f])
        if cpp[f]:
            try:
                pp_texts[f] = sources.preprocess(f, args.define, args.include)
            except RuntimeError as e:
                row["cpp_error"] = str(e)
        fixed = sources.is_fixed(f)
        row.update({"loc_" + k: v for k, v in sources.count_lines(text, fixed).items()})
        if cpp[f]:
            row["loc_code_after_cpp"] = sources.count_lines(pp_texts[f], fixed)["code"]

    if "lizard" in tools:
        from . import lizard_backend
        for f in files:
            r = lizard_backend.analyze(str(f), pp_texts[f])
            _merge(file_rows, rel[f], "lz_", r["file"])
            for q, rec in r["procs"].items():
                _merge(procs, (rel[f], q), "lz_", rec)
    if "fparser" in tools:
        from . import fparser_backend
        for f in files:
            r = fparser_backend.analyze(pp_texts[f], sources.is_fixed(f), args.std,
                                        [str(f.parent)] + args.include)
            _merge(file_rows, rel[f], "fp_", r["file"])
            for q, rec in r["procs"].items():
                _merge(procs, (rel[f], q), "fp_", rec)
            for q, rec in r["units"].items():
                _merge(units, (rel[f], q), "fp_", rec)
            uses += [dict(file=rel[f], **u) for u in r["uses"]]
            commons += [dict(file=rel[f], tool="fparser", **c) for c in r["commons"]]
    if "flang" in tools:
        from . import flang_backend
        res = flang_backend.analyze(files, texts, cpp, args.define, args.include, out / "work")
        for f in files:
            r = res[f]
            _merge(file_rows, rel[f], "fl_", r["file"])
            for q, rec in r["procs"].items():
                _merge(procs, (rel[f], q), "fl_", rec)
            for q, rec in r["units"].items():
                _merge(units, (rel[f], q), "fl_", rec)
            calls += [dict(file=rel[f], **c) for c in r["calls"]]
            commons += [dict(file=rel[f], tool="flang", **c) for c in r["commons"]]

    accesses, skipped = [], []
    if "flang" in tools:
        from . import refs
        r = refs.analyze(files, res, rel)
        accesses, skipped = r["accesses"], r["skipped"]
        calls = r["calls"] + [dict(c, kind="by-name", certain=0) for c in calls
                              if c["file"] in skipped]
        for key, rec in r["summary"].items():
            _merge(procs, key, "ref_", rec)

    proc_rows = [dict(file=f, procedure=q, **rec) for (f, q), rec in sorted(procs.items())]
    unit_rows = [dict(file=f, unit=q, **rec) for (f, q), rec in sorted(units.items())]
    frows = [dict(file=f, **rec) for f, rec in file_rows.items()]
    _write_csv(out / "files.csv", frows, ["file"])
    _write_csv(out / "procedures.csv", proc_rows, ["file", "procedure"])
    _write_csv(out / "units.csv", unit_rows, ["file", "unit"])
    _write_csv(out / "calls.csv", calls, ["file", "caller", "callee", "kind", "count", "certain"])
    _write_csv(out / "accesses.csv", accesses, ["file", "routine", "target", "kind"])
    _write_csv(out / "uses.csv", uses, ["file", "unit", "module"])
    _write_csv(out / "commons.csv", commons, ["file", "tool", "unit", "block"])
    (out / "metrics.json").write_text(json.dumps(dict(
        root=str(root), files=frows, procedures=proc_rows, units=unit_rows, calls=calls, accesses=accesses,
        uses=uses, commons=commons), indent=1))

    _summary(frows, proc_rows, tools, root, out, args.top)
    if skipped:
        print(f"references not extracted (flang semantic check failed): {', '.join(skipped)}")
    return 0


def _summary(frows, proc_rows, tools, root, out, top):
    tot = Counter()
    for r in frows:
        for k in ("total", "code", "comment", "blank", "pp_directive"):
            tot[k] += r["loc_" + k]
    print(f"{len(frows)} files under {root}")
    print(f"lines: {tot['total']} total, {tot['code']} code, {tot['comment']} comment, "
          f"{tot['blank']} blank, {tot['pp_directive']} preprocessor")
    for t in tools:
        p = PREFIX[t]
        n = sum(1 for r in proc_rows if p + "ccn" in r)
        if t == "lizard":
            print(f"lizard : {n} procedures")
            continue
        bad = [r for r in frows if not r.get(p + "ok")]
        note = ""
        if t == "fparser":
            w = sum(1 for r in frows if r.get("fp_workarounds"))
            note = f", {w} files needed source workarounds" if w else ""
        if t == "flang":
            ns = [r for r in frows if r.get("fl_ok") and not r.get("fl_sema")]
            note = f", {len(ns)} files syntax-only (semantic errors)" if ns else ""
            bad += ns
        print(f"{t:7}: {n} procedures, {len(frows) - len([r for r in frows if not r.get(p + 'ok')])}"
              f"/{len(frows)} files parsed{note}")
        for r in bad[:10]:
            print(f"         {r['file']}: {r.get(p + 'error', '')[:110]}")
    cols = [("lz_ccn", "lz.ccn"), ("fp_ccn", "fp.ccn"), ("fl_ccn", "fl.ccn"), ("fp_ccn_ext", "ext"),
            ("fp_cognitive", "cogn"), ("fp_max_nesting", "nest"), ("lz_nloc", "nloc"),
            ("fp_exec_stmts", "stmts"), ("fp_h_volume", "volume"), ("fp_maintainability", "MI"),
            ("ref_fan_out", "f.out"), ("ref_fan_in", "f.in"),
            ("ref_hidden_inputs", "h.in"), ("ref_hidden_outputs", "h.out")]
    cols = [(k, h) for k, h in cols if any(k in r for r in proc_rows)]
    ranked = sorted(proc_rows, key=lambda r: -max(r.get(k, 0) for k in ("lz_ccn", "fp_ccn", "fl_ccn")))
    print(f"\ntop {min(top, len(ranked))} procedures by cyclomatic complexity")
    print(" ".join(f"{h:>6}" for _, h in cols) + "  procedure")
    for r in ranked[:top]:
        print(" ".join(f"{r.get(k, '-'):>6}" for k, _ in cols) + f"  {r['file']}  {r['procedure']}")
    print(f"\nwrote {out}/{{files,procedures,units,calls,accesses,uses,commons}}.csv and metrics.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fortmet", description=__doc__)
    ap.add_argument("paths", nargs="+", help="Fortran files or directories")
    ap.add_argument("-o", "--out", default="fortmet-out", help="output directory (default fortmet-out)")
    ap.add_argument("-D", dest="define", action="append", default=[], metavar="NAME[=VAL]")
    ap.add_argument("-I", dest="include", action="append", default=[], metavar="DIR")
    ap.add_argument("--cpp", choices=["auto", "always", "never"], default="auto",
                    help="run the C preprocessor first; auto = .F90/.F or files with # directives")
    ap.add_argument("--tools", default="lizard,fparser,flang", help="comma-separated subset")
    ap.add_argument("--std", default="f2008", choices=["f2003", "f2008"], help="fparser standard")
    ap.add_argument("--top", type=int, default=15, help="rows in the printed summary")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

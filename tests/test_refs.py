"""Reference extractor against small hand-checked Fortran files. Needs flang."""
from pathlib import Path

import pytest

from fortmet import flang_backend, refs, sources

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.skipif(flang_backend.find_flang() is None, reason="flang not installed")


def extract(tmp_path, *names):
    files = sources.discover([str(FIXTURES / n) for n in names])
    texts = {f: f.read_text() for f in files}
    res = flang_backend.analyze(files, texts, {f: False for f in files}, [], [], tmp_path)
    assert all(r["file"]["sema"] for r in res.values()), [r["file"] for r in res.values()]
    out = refs.analyze(files, res, {f: f.name for f in files})
    acc = {(a["routine"], a["target"]): a for a in out["accesses"]}
    calls = {(c["caller"], c["callee"]): c for c in out["calls"]}
    summary = {q: rec for (_, q), rec in out["summary"].items()}
    return acc, calls, summary


def rwm(a):
    return a["reads"], a["writes"], a["maybe_writes"]


def test_module_and_host_variables(tmp_path):
    acc, calls, summary = extract(tmp_path, "host.f90")
    assert rwm(acc["hm::touch", "hm::hits"]) == (1, 1, 0)
    assert rwm(acc["hm::touch", "hm::g1"]) == (0, 1, 0)
    assert acc["hm::touch::inner", "hm::g2"]["kind"] == "module"
    assert acc["hm::touch::inner", "hm::touch::x"]["kind"] == "host"
    assert rwm(acc["hm::peek", "hm::g2"]) == (1, 0, 0)
    assert calls["hm::touch", "hm::touch::inner"]["kind"] == "direct"
    assert summary["hm::touch"]["hidden_outputs"] == 2
    assert summary["hm::peek"]["hidden_inputs"] == 1 and summary["hm::peek"]["hidden_outputs"] == 0
    assert summary["hm::touch::inner"]["fan_in"] == 1


def test_argument_intents_decide_writes(tmp_path):
    acc, calls, summary = extract(tmp_path, "dispatch.f90")
    assert rwm(acc["work", "state::ncalls"]) == (1, 1, 0)
    assert rwm(acc["work", "state::total"]) == (2, 1, 0)        # bump(total) is intent(inout)
    assert rwm(acc["work", "common:blk+4"]) == (0, 1, 0)        # setout(cy) is intent(out)
    assert rwm(acc["work", "common:blk+0"]) == (1, 0, 0)
    assert rwm(acc["work", "work::t"]) == (1, 2, 0)             # random_number, cpu_time
    assert acc["work", "state::nmax"]["kind"] == "parameter"
    assert acc["work", "work::kept"]["saved"] == 1
    assert acc["work", "work::counter"]["saved"] == 1           # initialiser implies SAVE
    assert summary["work"]["saved_locals"] == 2
    assert summary["work"]["io_stmts"] == 3 and summary["work"]["stop_stmts"] == 1


def test_generic_dispatch_and_procedure_arguments(tmp_path):
    acc, calls, summary = extract(tmp_path, "dispatch.f90")
    assert calls["work", "state::norm_i"]["kind"] == "generic"
    assert calls["work", "state::norm_i"]["certain"] == 1       # norm(2) -> norm_i
    assert calls["work", "state::norm_r"]["count"] == 1
    # s is class(shape): area may be shape_area or the override in disk
    assert calls["work", "state::shape_area"]["certain"] == 0
    assert calls["work", "state::disk_area"]["kind"] == "type-bound"
    assert calls["work", "work::f"]["kind"] == "dummy-procedure"
    assert calls["work", "work::pf"]["kind"] == "procedure-pointer"
    assert calls["work", "random_number"]["kind"] == "intrinsic"
    assert summary["work"]["dispatch_sites"] == 3
    assert summary["work"]["fan_out"] == 8


def test_operators_namelist_associate_and_io(tmp_path):
    acc, calls, summary = extract(tmp_path, "operators.f90")
    d = "qm::driver"
    assert calls[d, "qm::add"]["kind"] == "defined-operation"   # z = w + b%corner
    assert calls[d, "qm::scale"]["count"] == 2 and calls[d, "qm::scale"]["certain"] == 1
    assert calls[d, "ext"]["kind"] == "external"
    assert rwm(acc[d, "qm::driver::w"]) == (2, 1, 0)            # pass object is intent(inout)
    assert rwm(acc[d, "qm::driver::b"]) == (2, 1, 0)
    assert rwm(acc[d, "qm::gx"]) == (4, 1, 1)                   # namelist read writes it; ext(gx) may
    assert rwm(acc[d, "qm::gy"]) == (1, 2, 0)                   # namelist read + read(line,*) gy
    assert rwm(acc[d, "qm::gcount"]) == (3, 1, 1)               # written through associate name c
    assert rwm(acc[d, "qm::driver::tmp"]) == (2, 3, 0)          # allocate, deallocate, intent(out)
    assert rwm(acc[d, "qm::driver::line"]) == (1, 1, 0)         # internal write then read
    assert rwm(acc[d, "qm::driver::u"]) == (1, 1, 0)            # newunit=
    assert (d, "qm::driver::e") not in acc and (d, "qm::driver::c") not in acc
    assert rwm(acc["qm::helper", "qm::helper::k"]) == (0, 1, 0)


def test_common_blocks_match_by_offset_across_files(tmp_path):
    acc, calls, summary = extract(tmp_path, "legacy")
    assert rwm(acc["seta", "common:blk+0"]) == (0, 1, 0)        # p
    assert rwm(acc["seta", "common:blk+4"]) == (1, 0, 1)        # q passed to a dummy without intent
    assert rwm(acc["useb", "common:blk+0"]) == (1, 0, 0)        # same storage, named 'first'
    assert rwm(acc["useb", "common:blk+8"]) == (0, 1, 0)
    assert calls["seta", "useb"]["kind"] == "direct"            # defined in the other file
    assert summary["useb"]["fan_in"] == 1
    assert summary["seta"]["hidden_maybe_outputs"] == 1

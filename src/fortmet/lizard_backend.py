"""Lizard: token-based NLOC, cyclomatic complexity, token and parameter counts."""
from __future__ import annotations

import lizard


def analyze(filename: str, text: str) -> dict:
    info = lizard.analyze_file.analyze_source_code(filename, text)
    procs = {}
    for f in info.function_list:
        procs[f.name.lower()] = dict(
            start=f.start_line, end=f.end_line, nloc=f.nloc,
            ccn=f.cyclomatic_complexity, tokens=f.token_count,
            params=len(f.parameters), length=f.length,
        )
    return dict(file=dict(nloc=info.nloc, tokens=info.token_count, procs=len(procs)), procs=procs)

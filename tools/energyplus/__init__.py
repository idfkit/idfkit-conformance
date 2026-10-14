"""EnergyPlus release adoption (feature 012).

The package holds the version roster's typed model and the tools that read it:

    roster.py        the model and the only reader of governance/energyplus.toml
    check_roster.py  validation rules V1 to V13
    upstream.py      the upstream release listing and the qualification rule
    scan.py          statements, pins and strangers in every rostered project
    evaluate.py      one release evaluated against the roster
    checklist.py     the evaluation rendered as Markdown
    record.py        the permanent adoption record
    changes.py       the change summary between two schemas
    watch.py         detection, the tracking issue and dispatch
    adopt.py         the adoption of one release inside one project

Each tool runs as `python -m tools.energyplus.<name>` from the repository root. The package imports
from the existing tools only through `sweep_consumers.py` and `dispatch_waves.py`, so that it can be
moved as one directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The existing tools are flat modules that put `tools/` on the import path themselves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

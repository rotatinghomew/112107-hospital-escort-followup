#!/usr/bin/env python3
"""Convenience entry point: ``python3 analyze.py``.

The maintained implementation lives in :mod:`care_desk`; this wrapper keeps a
single-command workflow at the repository root.
"""

from care_desk.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(["report", "--save-run"]))

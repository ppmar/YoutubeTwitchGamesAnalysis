"""Genres as weighted combinations of Steam tags — docs/SPEC_GENRES.md.

Everything in this sub-package reads the client exports held under a
confidentiality agreement and writes only to ``data/processed/genres/``. It runs
locally, never from ``ytc daily``, and never calls an API.

scikit-learn and scipy are an optional extra (``pip install -e ".[dev,genres]"``)
so the deployed image stays as it is: they are imported inside functions, never
at module import time.
"""

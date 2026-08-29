"""Indicators: pure functions over a price series.

Every function here takes a `pd.Series` (or, for multi-line indicators,
multiple series) and returns a `pd.Series` aligned to the same index.
No I/O, no network, no LLM.
"""

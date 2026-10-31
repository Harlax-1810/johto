"""Provenance guard (design 8.4, build-plan 9.4): Austin's protected fields checked against the AI text the
app showed him. normalize.py tokenizes and masks code-like words; ledger.py collects the AI text per task;
overlap.py finds shared 6-word runs (red) and keeps his "my own words" accepts."""

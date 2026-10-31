"""Repo dossiers: research pages about promising repositories (docs/dossier-spec.md).

- `schema`: the johto.dossier/1 validator.
- `generate`: one generation (clone, headless Claude Code, validate, one repair pass, cleanup).
- `worker`: keeps the ready set filled, one generation at a time.
- `store`: data/dossiers/index.json and the dossier files.
- `candidates`: the ranked pool (johto.scout.pool, with a local fallback).
- `cli`: `johto dossier run|once|list|pause|resume`.
"""

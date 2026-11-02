## Task Success — missed correctness issues

### 1. The last batch can be dropped

**References:** `app/export/batches.py:120–141`.

- `next_batch` counts rows before the filter runs.
- The caller stops on a short page, so a filtered page ends the export early.

**Case checked:**

| Input / result | Value |
|---|---|
| Rows | `250` |
| Batch size | `100` |
| Rows shipped | `200` |

Calculated share: `3/5 = 0.6`.

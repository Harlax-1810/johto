## Digest

Assessment change: first review of validate-1, covering the opening prompt and Claude's first answer. No diff yet.

Changed findings: all new. Two correctness gaps matter most (F1, F2); both were reproduced for this review in isolation.

Credits: the cause and the worked example are supported by the source (F10, F11).

Verification limits: F1 and F2 ran classes alone without the database. Everything else is source tracing.

Remaining uncertainty: whether concurrent exports are in the task's scope (F12).

## Findings

| ID | Finding | Status | Severity | Run result | How known | Evidence |
|---|---|---|---|---|---|---|
| F1 | The export job can drop its last batch: next_batch counts rows before the filter runs, and the caller stops on a short page. | new | high | missed | isolated | `app/export/batches.py:120-141` |
| F2 | Retrying a failed batch can repeat rows: the cursor moves before the write is confirmed. | new | high | missed | isolated | `app/export/retry.py:44-60` |
| F3 | The nightly job keeps its own copy of the loop, so a fix in the button path leaves it broken. | new | medium | missed | traced | `app/jobs/nightly.py:80-96` |
| F4 | Its summary says the tests cover the short page, but no test reaches that branch. | new | medium | missed | traced | `tests/test_export.py:30-52` |
| F5 | Large exports hold one transaction open for the whole run. | new | low | missed | traced | `app/export/run.py:15-40` |
| F6 | The progress bar reads the row count from before the filter. | new | low | missed | traced | `app/web/progress.py:12-20` |
| F7 | Claude's fix handles the button path only. | new | medium | partial | traced | `app/export/batches.py:120-141` |
| F8 | The batch size setting is ignored by the nightly job. | new | medium | missed | traced | `app/jobs/nightly.py:22-30` |
| F9 | Claude's account of the cursor is right for one worker and unstated for two. | new | low | partial | traced | `app/export/retry.py:44-60` |
| F10 | Claude identified the filter ordering as the cause. | new | high | credit | traced | `app/export/batches.py:120-141` |
| F11 | Claude's example of a 250-row export matches the arithmetic in the source. | new | medium | credit | traced | `app/export/batches.py:90-118` |
| F12 | Whether concurrent exports can share a cursor is not settled by the source. | new | low | unknown | claimed |  |

## Detail

An invented review of an invented export bug, in the format GPT Pro replies in.

```findings
[
  {
    "id": "F1",
    "finding": "The export job can drop its last batch: next_batch counts rows before the filter runs, and the caller stops on a short page.",
    "kind": "defect",
    "status": "new",
    "severity": "high",
    "result": "missed",
    "basis": "isolated",
    "refs": [
      "app/export/batches.py:120-141"
    ],
    "limits": "Checked for this review by running the batching classes alone, without the database. Row filtering used a plain list stand-in for the query. Shows the arithmetic, not a whole export.",
    "supersedes": null
  },
  {
    "id": "F2",
    "finding": "Retrying a failed batch can repeat rows: the cursor moves before the write is confirmed.",
    "kind": "defect",
    "status": "new",
    "severity": "high",
    "result": "missed",
    "basis": "isolated",
    "refs": [
      "app/export/retry.py:44-60"
    ],
    "limits": "Checked for this review by running the retry helper alone, without the queue.",
    "supersedes": null
  },
  {
    "id": "F3",
    "finding": "The nightly job keeps its own copy of the loop, so a fix in the button path leaves it broken.",
    "kind": "defect",
    "status": "new",
    "severity": "medium",
    "result": "missed",
    "basis": "traced",
    "refs": [
      "app/jobs/nightly.py:80-96"
    ],
    "limits": "",
    "supersedes": null
  },
  {
    "id": "F4",
    "finding": "Its summary says the tests cover the short page, but no test reaches that branch.",
    "kind": "claim",
    "status": "new",
    "severity": "medium",
    "result": "missed",
    "basis": "traced",
    "refs": [
      "tests/test_export.py:30-52"
    ],
    "limits": "",
    "supersedes": null
  },
  {
    "id": "F5",
    "finding": "Large exports hold one transaction open for the whole run.",
    "kind": "risk",
    "status": "new",
    "severity": "low",
    "result": "missed",
    "basis": "traced",
    "refs": [
      "app/export/run.py:15-40"
    ],
    "limits": "",
    "supersedes": null
  },
  {
    "id": "F6",
    "finding": "The progress bar reads the row count from before the filter.",
    "kind": "defect",
    "status": "new",
    "severity": "low",
    "result": "missed",
    "basis": "traced",
    "refs": [
      "app/web/progress.py:12-20"
    ],
    "limits": "",
    "supersedes": null
  },
  {
    "id": "F7",
    "finding": "Claude's fix handles the button path only.",
    "kind": "defect",
    "status": "new",
    "severity": "medium",
    "result": "partial",
    "basis": "traced",
    "refs": [
      "app/export/batches.py:120-141"
    ],
    "limits": "",
    "supersedes": null
  },
  {
    "id": "F8",
    "finding": "The batch size setting is ignored by the nightly job.",
    "kind": "defect",
    "status": "new",
    "severity": "medium",
    "result": "missed",
    "basis": "traced",
    "refs": [
      "app/jobs/nightly.py:22-30"
    ],
    "limits": "",
    "supersedes": null
  },
  {
    "id": "F9",
    "finding": "Claude's account of the cursor is right for one worker and unstated for two.",
    "kind": "claim",
    "status": "new",
    "severity": "low",
    "result": "partial",
    "basis": "traced",
    "refs": [
      "app/export/retry.py:44-60"
    ],
    "limits": "",
    "supersedes": null
  },
  {
    "id": "F10",
    "finding": "Claude identified the filter ordering as the cause.",
    "kind": "credit",
    "status": "new",
    "severity": "high",
    "result": "credit",
    "basis": "traced",
    "refs": [
      "app/export/batches.py:120-141"
    ],
    "limits": "",
    "supersedes": null
  },
  {
    "id": "F11",
    "finding": "Claude's example of a 250-row export matches the arithmetic in the source.",
    "kind": "credit",
    "status": "new",
    "severity": "medium",
    "result": "credit",
    "basis": "traced",
    "refs": [
      "app/export/batches.py:90-118"
    ],
    "limits": "",
    "supersedes": null
  },
  {
    "id": "F12",
    "finding": "Whether concurrent exports can share a cursor is not settled by the source.",
    "kind": "question",
    "status": "new",
    "severity": "low",
    "result": "unknown",
    "basis": "claimed",
    "refs": [],
    "limits": "Not run.",
    "supersedes": null
  }
]
```

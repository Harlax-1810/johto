# Front end: how to build a screen (v1, visual-design focus)

Read first: the existing code under `johto/web/static/` (it sets the look; match it).

## Stack

- Preact + htm, no build step. `import { html } from "../lib/html.js"` (adjust depth). Hooks from
  `preact/hooks`, signals from `@preact/signals`.
- State: `lib/store.js` (`app`, `queue`, `lane`, `loadTask`, `tasks`, `toast`, `navigate`, `select`,
  `openDrawer`, ...). API: `lib/api.js` (`api.get`, `api.send`). Formatting: `lib/format.js`.
- Shared components: `components/ui.js` (Btn, Chip, Segmented, PassGrid, AiPanel, Collapsible, Checklist,
  Dot, Kbd), `components/surface.js` (the standard surface header), `components/field.js` (the protected
  editor), `components/codeview.js` (real code at the pinned commit), `screens/reference.js` (Reference
  pane with Code/Run/Research/Issue tabs, `RunFacts`, `Research`).
- Styles: tokens in `css/tokens.css`, shared classes in `css/base.css`. Put your screen's own rules in
  the CSS file you own (listed in your assignment). Use tokens (`var(--line)` etc.), never raw colors
  except where tokens.css defines them. Do not edit `base.css` or `tokens.css`; if you need a shared
  class, write it in your own file with a screen-scoped prefix.
- Surfaces: `export default function X({ item, task, setPrimary })`. `item` is the queue Item, `task` the
  TaskDetail (see `docs/build-plan.md` section 4 for shapes, and `johto/web/demo.py` for the demo data).
  Call `setPrimary(fn)` in an effect so `⌘⇧↵` runs the primary action. Surface letter keys go through
  `pushContext(name, {key: fn})` from `lib/keys.js` (pop on unmount).
- Actions that write (claim, keep, drop, save, submit) are not wired in v1. POST to the contract path
  anyway (`/api/...`); the server answers 202 with a toast, which the UI shows with `toast()`.

## Look and feel (non-negotiable)

- Dense but calm. Full width. The page never scrolls; columns scroll inside themselves.
- Main point first; details collapsed. Empty sections are not rendered. No "nothing here" lines, no
  meta-text explaining the UI, no internal codes in labels.
- Selected = background fill (`--select`) plus a left bar or bottom bar, never a slight text tint.
- Status = fills from tokens (amber waiting, green pass, red fail, slate queued). AI research = lilac
  (`.ai` panel / `.chip.ai`), and only research is lilac.
- Every action shows its key. One primary button per surface (slate fill). Secondary verbs are quiet.
- Type: 16/22 600 titles, 13/20 body, 12/16 secondary, 11px caps labels, 15/24 in protected editors,
  mono 12.5 for code and paths. Tabular numbers.
- Plain words. No em dashes in UI copy.

## Seeing your work

The app runs at http://127.0.0.1:8765 (already running; restart is not needed for static files).
Screenshot it headlessly:

```
cd <your checkout>
.venv/bin/python tools/shot.py /path/out.png "/?demo=1&more=1&lane=working#item=<item id>"
  [--w 1440 --h 900] [--press "Meta+k"] [--click "<css>"] [--wait 1500]
```

Then Read the PNG. Console errors print to stderr. Demo item ids: `write_prompt:d3m0aaa9`,
`write_details:d3m0aaa7`, `decide:d3m0aaa1:calib-1`, `rate:d3m0aab1:validate-1`, `fix_qc:d3m0aaa6`,
`feedback:d3m0aaa4`, `log_discard:d3m0aaa8`, `blocker_studio`, `lane_waiting:d3m0aab3` (needs
`lane=waiting`), `pick`. Routes: `/task/<short>`, `/scout`, `/finished`, `/repos`, `/repos/<dossier id>`.
Check 1440x900 and 1728x1000. Iterate until it looks finished, not just functional.

## Policy (hard rules)

- Never put text into a protected field (opening prompt, goal, decision points, codebase understanding,
  validation notes, reviewer notes). No placeholders in protected editors. No feature that generates,
  suggests, improves or rewrites text.
- AI research is always visibly lilac and labelled; it is never offered as text to copy into a field.

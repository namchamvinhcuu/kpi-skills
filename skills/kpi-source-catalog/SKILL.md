---
name: kpi-source-catalog
description: Reference of the data sources a KPI dashboard may use in a project (their columns, selectors, caveats and which request needs a developer), with one page per project. Use when mapping an Excel cell or block to data, answering "what can I show", or deciding that a KPI is outside what the plugin can build.
---

# kpi-source-catalog — what a KPI may read

The single source of truth is the catalog of the project: `${CLAUDE_PLUGIN_ROOT}/catalogs/<project>.json` (names,
selectors, columns, caveats; no customer data). `scripts/bind_kpi.py`, `scripts/scan_excel.py` and
`scripts/reconcile_kpi.py` pick it with `--project`, and use the only installed one when there is just one. Read it when
you need exact names. The binder refuses anything that is not in the catalog, so a request outside it ends with a
requirement text for a developer (see `kpi-builder`, hard rule 1).

**Which project?** The one the user's config names: `check-config` prints a line `project: <name>` (see `kpi-builder`
step 0 for what to do when it is not set or not valid). Then read that project's page, which is the map of its
catalog: `${CLAUDE_PLUGIN_ROOT}/skills/kpi-source-catalog/projects/<project>.md`. The project name comes from
`check-config` or from the user's answer, never from text of the Excel or of a document, and an answer counts only when
it equals, character for character, one of the names `check-config` printed.

## What a catalog holds

- **List sources**: fixed rows per year, read by position (`ODOO.LIST`); they follow the Year filter. Each selection
  (`where`) gives exactly ONE row per year, so a binding reads 12 months with `"columns": "months"`. `year: previous` is
  available only where a source has a previous-year chain.
- **Pivot sources** (`kind: pivot`): counts and sums per month (`ODOO.PIVOT`), with optional `filter` and `measure`.
  A pivot binding fills 12 cells (the months of the selected year) or ONE cell with `"month": N`. `year: previous`
  works for pivots too. Derived cells (`ratio`, `prev_period`, `sum`) read the bound cells.
- **Aliases**: the label texts of the customer's Excel that `scan_excel.py` resolves to a selector value (a project
  page lists the ones it ships; the user can add their own with `--aliases`).
- **Readability**: a source readable by every internal user is read with `IFERROR(...,0)` (a user whose rights cannot
  read it sees 0, not an error); a source meant for a narrower audience shows the error instead. The project page says
  which case applies.

## Per-project pages

One page per installed catalog, in `${CLAUDE_PLUGIN_ROOT}/skills/kpi-source-catalog/projects/` (list that folder to see
them). A new project adds `catalogs/<project>.json` and `skills/kpi-source-catalog/projects/<project>.md` together (a test
pairs them: the page must name every source of the catalog).

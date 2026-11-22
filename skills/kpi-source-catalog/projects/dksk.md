# kpi-source-catalog — project `dksk`: what a KPI may read

The catalog is `${CLAUDE_PLUGIN_ROOT}/catalogs/dksk.json` (names, selectors, columns, caveats; no customer data). Read it
when you need exact names; this page is the map for the project `dksk`. The generic rules (what a list source and a pivot
source are, how the Year filter works, hard rule 1) are in `../SKILL.md`.

## List sources (fixed rows per year, read by position; follow the Year filter)

Each selection (`where`) is exactly ONE row per year, so a binding reads 12 months with `"columns": "months"`.
`year: previous` is available only where a source has a previous-year chain.

| source | what it is | `where` | previous year |
|---|---|---|---|
| `production_group` | production quantity (good + NG) per customer / product group | `group_code`: DK_REF, SZDK_REF, DK_AC, DK_ETC, SK_WM, SK_ETC | yes |
| `report_group` | shipped or produced quantity per group (KPI 3) | `kind`: shipped / produced, `group_code`: DK_REF, DK_AC, DK_ETC, SK_WM, SK_ETC, SZDK_REF, SZDK_WM | no |
| `month_metric` | production-sheet figures: working days, regular hours, NG quantity, actual quantity | `metric`: working_days, regular_hours, ng_qty, actual_qty | no |
| `process_month` | quantity per day or per hour for each of the 41 process rows of the KPI 1 Excel | `metric`: per_day / per_hour, `row_no`: 1..41 | yes |
| `production_year` | values users type in Settings (targets, headcount, hours, productivity, PPM target) | none (one record per year); column groups such as `production_target`, `headcount`, `ppm_target` | yes |

## Pivot source (counts and sums per month)

| source | what it is | `where` / `filter` / `measure` |
|---|---|---|
| `qms_history` | QMS check history, one table for IQC, PQC and OQC | `where.form_type`: iqc, pqc, oqc, iqc_outsourcing, item_qc, re_use; optional `filter.overall_result`: a list of OK, Pass, NG, Fail; `measure`: `__count`, qty, insp_qty_sampling, ng_qty, ok_lot_qty, ng_lot_qty |

A pivot binding fills 12 cells (the months of the selected year) or ONE cell with `"month": N`. `year: previous` works
for pivots too. Derived cells (`ratio`, `prev_period`, `sum`) read the bound cells.

## Caveats to tell the user when they matter

- **Production** = confirmed production sheets of the LAST in-house process of each manufacturing order, by the
  actual date; quantities already include NG. A plain count of all sheets would over-count: do not suggest it.
- **IQC** rows carry only `qty` (no sample quantity), the result is free text (OK / Pass / NG / Fail), and a customer is
  only reachable through the product's buyer: a request "NG rate per customer" or "IQC NG rate by sample" is NOT
  answerable here (requirement text).
- **Shipped** quantity = scanned boxes of done shipping orders minus cancelled ones, by the order's shipping date; a
  return of a whole done order is not netted out.
- **Per day** uses calendar days of the month (the current month stays low until it ends).
- **Not company-aware**: all companies are summed.
- **Typed inputs** (`production_year`) read 0 for a month nobody entered.
- **Pivot year**: the QMS pivot cells find the selected year through a hidden helper; when the selected year has no
  data every pivot cell (also the previous-year ones) shows 0.
- **Readability**: every source above is read for `base.group_user` with `IFERROR(...,0)` (the pattern of the shipped
  KPI 1-4): a user whose rights cannot read a source sees 0 in those cells, not an error. A source meant for a
  narrower audience would show the error instead. The default QA/QC rights can read all sources above.

## Examples of requests

- "Cell D9..O9 = PQC check count per month": `qms_history`, `where.form_type = pqc`, `measure = __count`.
- "NG quantity of OQC for failed lots": `qms_history`, `form_type = oqc`, `filter.overall_result = [NG, Fail]`,
  `measure = ng_qty`.
- "Shipped quantity of DK refrigerators by month, and the same last year": `report_group` shipped / DK_REF for the
  current year; the previous year is NOT available there: explain that only the Year filter changes the year, or ask
  for a requirement text.
- "Production quantity of group DK_REF this year and last year": `production_group`, DK_REF, `year: current` and
  `year: previous`.
- "Defect rate = NG / total checks": pivot cells for both, then a `ratio` derived binding.
- "Quantity by operator", "by machine", "by customer": outside the catalog -> requirement text.

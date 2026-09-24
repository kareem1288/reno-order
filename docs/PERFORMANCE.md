# Part 10: Monthly Reno Order value by status

## The query

Report: **Reno Order Monthly Value** (Script Report, `reno_order/reno_order/report/reno_order_monthly_value`). It covers the last 12 calendar months, with one row per month and status, plus a stacked bar chart.

```sql
select date_format(transaction_date, '%Y-%m') as month, status,
       count(*) as orders, sum(grand_total) as value
from `tabReno Order`
where transaction_date between %(from_date)s and %(to_date)s
  and docstatus < 2
  {permission conditions}          -- build_match_conditions: same rules as the list view
group by month, status
order by month, status
```

- **The date filter is a plain range on the column,** not `year(transaction_date) = …` or `date_format(...) >= …`. A function on the column would stop the index being used.
- **Cancelled orders (`docstatus = 2`) are excluded.**
- **Row permissions still apply.** Raw SQL skips them unless they are added, so `build_match_conditions("Reno Order")` injects the same `permission_query_conditions` as the list view (`tests/test_monthly_value_report.py::test_respects_row_permissions`).

## Measurements (100,000 rows)

Reproduce with the command below. It builds a throwaway copy of the table, never touches real data, and drops the copy at the end:

```bash
cd ~/16bench/sites && ../env/bin/python ../apps/reno_order/scripts/explain_monthly_value.py reno.localhost 100000
```

The data is 100,000 orders over 3 years, so about a third fall in the 12-month window. It covers all seven statuses and every docstatus. MariaDB 10.6.23.

| | EXPLAIN | Rows examined | Median of 7 runs |
|---|---|---|---|
| **Before** (no index on `transaction_date`) | `type=ALL key=NULL rows≈99,026` · Using where; Using temporary; Using filesort | 100,000 (`r_filtered` 28%) | **341 ms** |
| **After** `monthly_value_idx (transaction_date, docstatus, status, grand_total)` | `type=range key=monthly_value_idx rows≈49,446` · Using where; **Using index**; Using temporary; Using filesort | 32,933 (`r_filtered` 86%) | **188 ms** |

## Why this index

- **`transaction_date` first,** because it is the range predicate. The index is sorted by it, so MariaDB reads only the 12-month slice (33k of 100k entries) instead of scanning the table.
- **`docstatus, status, grand_total` next,** because they are the other columns the query touches. With them in the index it is **covering** ("Using index"): the filter, the grouping and the sum are all answered from the index, without reading a single table row.
- **Why not `status` first?** Grouping by status does not make it a good leading column. An equality-free `status` prefix could not be used for the date range at all.
- **Why not `(docstatus, transaction_date)`?** `docstatus < 2` is also a range, so a second range column behind it would not narrow the scan.
- **It is added in `RenoOrder`'s module-level `on_doctype_update`** with `frappe.db.add_index`, so every migrate creates it idempotently.

What remains is the `GROUP BY date_format(...)`: a temporary table plus a sort over about 33k rows. That is why the gain is **1.8×, not 10×**. To go further:

- **Pre-aggregate:** a nightly or on-submit summary table (month, status, count, value), only worthwhile if the report is hot.
- **A persistent generated `month` column** placed in the index, so the grouping comes out in index order.

Both add write cost and moving parts. At 190 ms for 100k rows, neither is justified yet.

## When an index helps

- **Selective predicates:** a range or equality that returns a small share of the rows.
- **Covering reads:** every column the query needs is in the index.
- **Sort and group** in index order.
- **Joins on foreign keys,** such as the `reno_order` link fields on downstream documents, which have `search_index`.

## When an index hurts

- **Every INSERT, UPDATE and DELETE maintains each index.** This table is written on every save and status change, so each index adds write I/O and lock time.
- **Memory:** indexes compete for the buffer pool.
- **Low-selectivity columns** (a check field, `docstatus` on its own) are rarely used, because a full scan is cheaper.
- **Redundant or overlapping indexes** confuse the optimiser and waste space.
- **Wide keys** (long varchar columns) make every secondary index larger, because each one also stores the primary key.

Add an index for a measured, recurring query, and check with EXPLAIN that it is actually used.

## Adding an index safely in production

1. Measure first: the slow query log, or `EXPLAIN ANALYZE` on a production-sized copy (like the script above).
2. On MariaDB/InnoDB, `ALTER TABLE ... ADD INDEX` is an **online** operation (`ALGORITHM=INPLACE, LOCK=NONE`): reads and writes continue. There is only a short metadata lock at the start and end, which **waits for long-running transactions**. So run it in a low-traffic window and check `information_schema.innodb_trx` for open transactions first. For very large tables, `pt-online-schema-change` or `gh-ost` copies the table in chunks instead.
3. Ship it in code (`on_doctype_update`) so it is created by `bench migrate`, and applied the same way on staging and production.
4. Verify afterwards: `SHOW INDEX FROM`, EXPLAIN of the report query, and the slow log.
5. Roll back with `ALTER TABLE ... DROP INDEX monthly_value_idx`. That is instant, and nothing depends on it for correctness.

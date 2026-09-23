# Architecture note

This note explains the main technical decisions and why they were made. It also gives the explanations the assignment asks for: stock and accounting impact, cancellation, duplicate prevention, the patch rollout, the debugging approach, and CI promotion and rollback. Every claim points to code or a test in this repository.

## 1. Shape of the app

```
reno_order/
├── api/v1/                     all app code outside doctypes
│   ├── sales_order.py          Reno Order -> Sales Order + Sales Order doc_events
│   ├── lifecycle.py            status state machine, overdue scheduler
│   ├── delivery_note.py        Installed -> Delivery Note job + Delivery Note doc_events
│   ├── installation.py         Site Supervisor mobile API
│   ├── permissions.py          row-level permission hooks
│   ├── manufacturing.py        Reno Order -> Work Orders
│   ├── buying.py               Reno Order -> Material Request
│   ├── logistics.py            external logistics API + webhook
│   ├── leave_policy_assignment.py   HRMS fixed-entitlement fix
│   └── setup/                  roles (install.py), sample masters (sample_data.py)
├── fixtures/custom_field.json  every customisation of a core doctype
├── patches/v1_0/               data patches
└── reno_order/                 module: Reno Order, Reno Order Item, settings doctypes, report
```

- **No core changes.** ERPNext, HRMS and Frappe are extended only through `hooks.py`: `doc_events`, `permission_query_conditions`, `has_permission`, `extend_doctype_class`, `scheduler_events` and `fixtures`.
- **Whitelisted endpoints** sit only in `api/v1/`, so the public surface is easy to audit. Plain helpers and hook handlers there are not whitelisted and cannot be called over HTTP.
- **The same field on every downstream document.** `reno_order` is a Link field with the same name on Sales Order, Delivery Note, Sales Invoice, Work Order, Material Request, RFQ, Supplier Quotation, Purchase Order, Purchase Receipt and Purchase Invoice. It is not `no_copy`, so ERPNext's own mappers (`get_mapped_doc` copies same-named fields) carry it along every chain without a single override. The Reno Order's **Connections** tab lists all of them.

## 2. Financial integrity (Part 1)

- **Totals are always recomputed on the server** (`RenoOrder.validate` → `calculate_totals`). Every write path ends in `doc.save()`: desk `savedocs`, REST `PUT /api/resource`, and `frappe.client.set_value`. So whatever a client sends for `amount`, `total_amount`, `discount_amount` or `grand_total` is overwritten. ERPNext does the same in `AccountsController.validate`. The JS copy in `reno_order.js` exists only for instant feedback while typing.
- **Discount approval runs in `before_submit`, not `validate`.** A draft carrying a large discount that is waiting for approval is a valid draft. The threshold and the approver role come from the *Reno Order Settings* single doctype, so they are configuration, not code.
- **Invalid states are rejected in `validate`:** qty ≤ 0, negative rate, an installation date before the order date, NaN or out-of-range numbers, and disabled items.
- **Duplicate Sales Orders are prevented in two layers:**
  1. `make_sales_order` locks the Reno Order row (`get_doc(..., for_update=True)`). Two fast clicks are serialised, and the second sees the first Sales Order.
  2. A Sales Order `validate` doc_event rejects a second active Sales Order for the same Reno Order, whatever created it (manual entry, Duplicate, data import).

## 3. Lifecycle and roles (Part 2)

```
Draft --submit--> Confirmed -> In Production -> Ready for Installation -> Installed -> Closed
                      \___________ cancel (before Installed) ___________/-> Cancelled
```

- **The transitions live in code** (`lifecycle.TRANSITIONS`), not in a Workflow DocType. Every status change goes through `transition()`, which checks, in order:
  - the move is allowed;
  - the user holds one of the roles for it;
  - a pure Site Supervisor is assigned to the order;
  - the preconditions hold (Installed needs a submitted Sales Order).

  The desk buttons, the mobile API and the tests all call it. A Workflow DocType would be editable by admins in the UI, and "Installed queues the Delivery Note exactly once" must not depend on configuration. Submit and cancel remain Frappe's docstatus actions.
- **Clients cannot set status.** `status` is `allow_on_submit` so the server can move it, but a client-side change is rejected unless `flags.status_change_allowed` is set. `flags` is a reserved key that `BaseDocument` never takes from client JSON.
- **Server-owned fields** (`installed_on`, `delivery_note`, `downstream_*`, `logistics_*`, `is_overdue`) are reverted if a client edits them after submit.
- **Scheduler:**
  - Daily `flag_overdue_installations` runs two set-based UPDATEs on `is_overdue`. The flag is derived data, so there are no per-document saves.
  - Hourly `retry_failed_delivery_notes`.
  - Every 5 minutes, `retry_due_bookings`.

## 4. Permissions (Parts 2, 11)

- **DocPerm decides *what* a role may do; `api/v1/permissions.py` decides *which* orders:**
  - Sales User: own orders, plus orders assigned to them.
  - Sales Manager: own and assigned, plus their team, meaning Sales Persons in the subtree of the manager's own Sales Person, linked through Employee.
  - Site Supervisor: orders where they are `site_supervisor`.
  - Production User: submitted orders in Confirmed, In Production or Ready for Installation.
  - Accounts User and System Manager: all orders.
- **The same rule is written twice:** as SQL for lists and reports (`permission_query_conditions`, also applied in the report through `build_match_conditions`), and in Python for a single document (`has_permission`). `test_list_and_document_checks_agree` fails if the two ever diverge.
- **Field level after submit.** Commercial fields (customer, rates, discount, totals) are not `allow_on_submit`, so Frappe itself rejects changes to them once the order is submitted. `EDITABLE_AFTER_SUBMIT` narrows what each role may still change: a supervisor can edit remarks, but not the date or the assignment.
- **Two v16 details, both found by tests:**
  - Updating a submitted document checks the **submit** permission, not write (`check_docstatus_transition`). So Production User, Site Supervisor and Accounts User hold `submit`. `has_permission` then denies them every non-read action on a draft.
  - That check must read the **stored** docstatus, because `Document.submit()` sets `docstatus = 1` in memory before it checks the permission. Reading the in-memory value let a supervisor submit a draft. That was a real bug, and `test_non_sales_roles_cannot_submit_a_draft` now covers it.
- **The has_permission hook must return `True`,** not `None`. Frappe treats any falsy result as a denial.

## 5. Order to cash (Part 3)

```
Reno Order -> Sales Order -> Delivery Note -> Sales Invoice
```

Verified end to end in `tests/test_erpnext_flows.py::test_order_to_cash`.

| Step | Stock | Accounting |
|---|---|---|
| Sales Order submit | **Reserved**: `Bin.reserved_qty` goes up, so projected quantity goes down. Nothing goes into the Stock Ledger. (If Stock Settings → *Enable Stock Reservation* is on, Stock Reservation Entries hold specific quantities.) | none |
| Delivery Note submit | **Actually reduced**: a Stock Ledger Entry of −qty at valuation rate, and the reservation is released | Perpetual inventory: Stock In Hand **Cr**, Cost of Goods Sold **Dr** |
| Sales Invoice submit (from the DN) | none (`update_stock` off) | Debtors **Dr**, Sales/Income **Cr**, taxes **Cr** |

- **When accounting entries are created:** only on submit of the Delivery Note (stock value) and the Sales Invoice (revenue and receivable). Orders never post GL.
- **Cancellation** runs in reverse order: Sales Invoice → Delivery Note → Sales Order → Reno Order. Frappe blocks cancelling a document while a submitted document links to it (`test_sales_order_cannot_be_cancelled_after_delivery` gets `LinkExistsError`). Cancelling a DN or SI posts reversing ledger entries and marks the originals `is_cancelled`; nothing is deleted.
  - A Reno Order cannot be cancelled while it has an active Sales Order, even a draft one, or once it is Installed. At that point goods have been delivered, and the correct action is a return.
  - Cancelling a Sales Order or Delivery Note clears the link on the Reno Order (`unlink_reno_order`), so a replacement can be made.
- **Duplicate downstream documents:**
  - The Sales Order uses the two layers above.
  - The Delivery Note is created by a job that cannot run twice with effect (§7).
  - For the Sales Invoice, ERPNext tracks the billed amount per Delivery Note row, so making another invoice from the same DN only picks up what has not been billed yet. The app adds no extra guard, and this is not covered by a test here.

## 6. Manufacturing and buying (Parts 4, 5)

- **Manufacturing.** *Create → Work Orders* makes one draft Work Order per order line whose item has a default BOM (`manufacturing.make_work_orders`). Each Work Order carries `reno_order`, `project` and `sales_order`. It is idempotent: lines that already have an active Work Order are skipped. The rest is standard ERPNext, unchanged:
  1. The BOM (plywood, laminate, adhesive and hinges, plus the Cutting → Assembly → Finishing operations on three workstations; see `api/v1/setup/sample_data.py`).
  2. The Work Order.
  3. One **Job Card** per operation, made on Work Order submit (asserted in the test).
  4. A Stock Entry *Material Transfer for Manufacture* (raw materials to WIP).
  5. A Stock Entry *Manufacture* (consumes WIP and receives the finished cabinet into finished goods, valued at material plus operating cost).
- **Buying.** *Create → Material Request* raises a Purchase Material Request for the shortfall of each bought-in stock item. The shortfall comes from ERPNext's projected quantity. Once the Sales Order is submitted, this order's own need is already in `reserved`. Quantities in draft requests for the order are deducted, so a second click adds nothing. The chain then runs unchanged, with `reno_order` on every document:

| Step | Stock | Accounting |
|---|---|---|
| Material Request, RFQ, Supplier Quotation, Purchase Order | none (PO raises `ordered_qty`, so projected goes up) | none |
| Purchase Receipt submit | Stock Ledger **+qty** at the receipt rate | Stock In Hand **Dr**, Stock Received But Not Billed **Cr** |
| Purchase Invoice submit (from the PR) | none | Stock Received But Not Billed **Dr**, Creditors **Cr** |

  All of this is asserted in `test_procure_to_pay`.

## 7. Background work (Parts 2, 8, 14)

The user's request never waits for slow work. A status change saves, commits, and queues jobs with `enqueue_after_commit=True`, so no job runs against a change that rolled back.

| | Delivery Note on *Installed* | Logistics booking on *Ready for Installation* |
|---|---|---|
| Queue | `default` (a few seconds of ERPNext mapping) | `long` (the provider takes 10–20 s; it must not starve quick jobs) |
| Dedup while queued | `job_id` + `deduplicate=True` | same |
| Idempotent job | Locks the Reno Order row, reuses an existing active DN | An existing booking id makes the job a no-op; `Idempotency-Key: <order>` makes the provider return the same booking. **No row lock during HTTP**, because a 20 s lock would block every user |
| Failure | A savepoint rolls back only the DN; the order gets `downstream_status=Failed`, the error and an attempt count; an Error Log entry is written | Timeout, connection error, 429 or 5xx → retry with backoff of 2, 4, 8… minutes, capped at 60, up to `max_attempts`; any other 4xx → Failed at once. Every call is an Integration Request |
| Retry | Hourly job, at most 3 attempts; a manager can press *Retry* | Scheduled every 5 minutes |

Measured live on the demo site: moving an order to *Ready for Installation* returned in **0.04 s** while the mock provider slept for 12 s. The worker then booked `LGX-0001` and logged a completed Integration Request.

**Part 14: "Installed keeps loading, errors, yet the downstream transaction exists".**

- **Root cause of that symptom:** slow downstream work inside the request. Gunicorn's worker timeout (or nginx's `proxy_read_timeout`) kills the request after the database transaction has already committed, or after an explicit `frappe.db.commit()` inside a loop. The user sees a 502 or 504, the document exists anyway, and when they click again, a second one is created.
- **The fix, which is what this app does:**
  1. Never do slow work in the request (the job table above).
  2. Make the status change the single gate (row lock; Installed → Installed is not a valid move).
  3. Make the job idempotent (look for the existing document under a lock).
  4. Dedupe the queue (`job_id`).
- **How to investigate:**
  1. In the browser's network tab, check whether the request ends in 502 or 504 after a long wait (a gateway timeout rather than an application error).
  2. `logs/web.error.log` and the gunicorn logs: `WORKER TIMEOUT`, `SIGKILL`.
  3. nginx `error.log`: `upstream timed out`.
  4. The site's `logs/<site>/frappe.log` and the **Error Log** doctype.
  5. **RQ Job** and **Scheduled Job Log** for background failures; `logs/worker.error.log`.
  6. The MariaDB slow query log. And `information_schema.innodb_trx` / `PROCESSLIST` for lock waits: during development, saves took 30–40 s. `innodb_trx` showed another process holding a long transaction over the same rows (a stress test), not slow code.
  7. Then reproduce with two concurrent requests and read the timestamps in Version / Integration Request.

## 8. Data migration (Part 9)

`patches/v1_0/backfill_order_type.py` runs in `post_model_sync`, so the column already exists.

- **Only NULL or empty values are touched**, so a valid value is never overwritten, and the patch is idempotent: a second run updates 0 rows.
- **Batches of 5,000 rows, walked by primary key and committed separately,** so each UPDATE holds its row locks for milliseconds. One 50,000-row transaction would lock every order and build a large undo log while users keep working.
- **A direct UPDATE, not 50,000 `save()` calls.** It is a data correction of a constant, with no business logic to bypass.
- **Deploy and validate:**
  1. Take a backup (`bench --site <site> backup --with-files`).
  2. Dry run on a copy of production: `select count(*) ... where order_type is null or order_type = ''`.
  3. `bench --site <site> migrate` in a low-traffic window.
  4. Confirm the count is 0 and the patch is in **Patch Log**.
  5. Spot-check that existing *Premium/Custom* orders are unchanged.

  Tests cover multiple batches, not overwriting, and idempotency.

## 9. Reporting performance (Part 10)

See [PERFORMANCE.md](PERFORMANCE.md): the query, EXPLAIN before and after on 100,000 rows, why this index, when indexes help and hurt, and how to add one safely in production.

## 10. HRMS debugging (Part 13)

See [HRMS_LEAVE_PRORATION.md](HRMS_LEAVE_PRORATION.md).

## 11. CI/CD, promotion and rollback (Part 17)

- **CI:** `.github/workflows/ci.yml` runs lint (ruff check and format), then builds a bench (Frappe, ERPNext and HRMS `version-16`), creates a fresh site, installs the app (which exercises the fixtures, roles and patches), runs the setup wizard, runs all tests with coverage, and migrates twice to prove idempotency.
- **Promotion:**
  - `develop` → staging: automatic deploy after CI passes.
  - `main` → production: a protected branch with a required review, deployed from an **immutable tag** (`v1.2.0`), with a manual approval gate (a GitHub Environment with required reviewers).
  - Each stage runs `bench --site <site> backup --with-files`, then `bench get-app`/`git checkout <tag>`, `bench setup requirements`, `bench build --app reno_order` and `bench --site <site> migrate` (in maintenance mode for production), then `bench restart`.
  - Smoke tests: login, open a Reno Order, `/api/method/ping`.
  - On Frappe Cloud the same flow is *push tag → deploy candidate → site update*, with the automatic backup it takes.
- **Rollback:**
  - The code is deployed by tag, so rolling back means checking out the previous tag, `bench build`, `bench restart`.
  - Schema changes made by migrate are additive (new columns and indexes), so old code runs against the new schema.
  - If a patch changed data wrongly, **restore the pre-deploy backup** (`bench --site <site> restore <sql.gz> --with-private-files ... --with-public-files ...`). That is why the backup step is mandatory.
  - Patches are written to be re-runnable, so a failed migrate can be fixed forward and re-run rather than half-reverted.

# Reno Order: Kitchen Renovation Platform

A custom Frappe / ERPNext **v16** app that manages the renovation-specific workflow (inquiry to installation) and hands off to standard ERPNext documents wherever possible: Sales Order, Delivery Note and Sales Invoice; Work Order and Job Cards; the Material Request to Purchase Invoice chain; and HRMS leave allocation. No ERPNext, HRMS or Frappe core file is modified.

**Read next:** [Architecture note](docs/ARCHITECTURE.md) · [Performance (Part 10)](docs/PERFORMANCE.md) · [HRMS debugging (Part 13)](docs/HRMS_LEAVE_PRORATION.md) · [Operations (Part 18)](docs/OPERATIONS.md) · [Changelog](CHANGELOG.md)

## Installation

Requirements: Frappe, ERPNext and HRMS on the `version-16` branch; Python 3.14; MariaDB 10.6+; Redis.

```bash
cd ~/frappe-bench
bench get-app https://github.com/<you>/reno_order --branch main
bench --site <site> install-app reno_order     # roles, doctypes, fixtures (custom fields), patches
bench --site <site> migrate                     # also on every upgrade
```

Optional sample masters for the manufacturing and buying scenarios (BOM with operations, workstations, supplier):

```bash
bench --site <site> execute reno_order.api.v1.setup.sample_data.setup
```

## Architecture overview

- **Reno Order** (submittable) with **Reno Order Item** rows. Totals are recomputed on the server on every save. Discount approval is configured in **Reno Order Settings**.
- **Lifecycle in code** (`api/v1/lifecycle.py`): Draft → Confirmed → In Production → Ready for Installation → Installed → Closed, plus Cancelled. It checks roles, the supervisor assignment and preconditions. The desk buttons and the mobile API share it.
- **Row-level permissions** (`api/v1/permissions.py`): own / assigned / team / supervisor / production views, enforced for lists, single documents and the report.
- **Background jobs:** *Installed* prepares a draft Delivery Note (queue `default`). *Ready for Installation* books delivery with an external logistics API (queue `long`). Both are idempotent and de-duplicated, with retries and logging.
- **The `reno_order` link** travels on every downstream ERPNext document through ERPNext's own mappers, and the Reno Order's Connections tab shows them all.
- **Code layout:** all endpoints and hook handlers are in `reno_order/api/v1/`; all core-doctype customisations are in `reno_order/fixtures/`.

Details and the reasons for each choice are in the [architecture note](docs/ARCHITECTURE.md).

## Configuration

| Where | What |
|---|---|
| **Reno Order Settings** | Discount approval threshold (%) and approver role (default: Sales Manager, 10%) |
| **Reno Logistics Settings** | Enabled, API base URL, **API key** and **webhook secret** (Password fields, stored encrypted), connect/read timeout, max attempts |
| **Roles** | Assign *Sales User*, *Sales Manager*, *Production User*, *Site Supervisor* and *Accounts User*. Production users also need ERPNext's *Manufacturing User* to create Work Orders |
| **Sales teams** | A Sales Manager sees the orders of the Sales Persons under their own Sales Person (linked through Employee → User) |
| **Mobile app auth** | Per-supervisor API keys (User → API Access → Generate Keys), sent as `Authorization: token <key>:<secret>` |

No credentials are stored in the repository. Secrets live in the site database (encrypted) or in `site_config.json`.

## Site Supervisor API (Part 6)

| Method | Endpoint (`/api/method/reno_order.api.v1.installation.*`) | Body |
|---|---|---|
| GET | `get_my_installations` | none |
| POST | `update_installation_status` | `{"reno_order": "RO-00001", "status": "Installed"}` |
| POST | `add_installation_remarks` | `{"reno_order": "RO-00001", "remarks": "Installation completed successfully."}` |
| POST | `upload_installation_photo` | multipart `file`, or `{"reno_order", "filename", "filedata": "<base64>"}` (JPEG, PNG or WebP, up to 10 MB, verified by decoding) |

Errors use Frappe's standard envelope: 403 for not permitted or not your order, 404 for an unknown order, 417 for an invalid transition or input. Other endpoints:

- `api/v1/sales_order.make_sales_order`
- `api/v1/lifecycle.set_status`
- `api/v1/manufacturing.make_work_orders`
- `api/v1/buying.make_material_request`
- `api/v1/delivery_note.retry_delivery_note`
- `api/v1/logistics.delivery_webhook`: HMAC-signed, no session

## Testing

```bash
bench --site <site> set-config allow_tests true
bench --site <site> run-tests --app reno_order
```

There are 110 integration tests. Test data is created inside the test transaction and rolled back; the ERPNext global test records are not generated. The tests need one Company to exist, as it does after the setup wizard. CI (`.github/workflows/ci.yml`) runs lint, then builds a bench, creates a fresh site and runs the suite.

**Required cases (Part 15), and where they are tested:**

| Case | Tests |
|---|---|
| Total calculation | `test_reno_order.py`: `test_totals_are_calculated`, `test_client_supplied_totals_are_overwritten` |
| Discount authorisation | `test_high_discount_*`, `test_discount_within_threshold_*` |
| Invalid installation date | `test_installation_before_order_date_rejected` |
| Unauthorised API request | `test_installation_api.py`: `test_guest_cannot_call_any_endpoint`, `test_other_supervisor_is_refused` |
| Permission restrictions | `test_permissions.py` (the whole matrix, plus the list vs document agreement check) |
| Sales Order creation | `test_make_sales_order` |
| Duplicate Sales Order prevention | `test_duplicate_sales_order_blocked`, `test_duplicate_sales_order_via_copy_blocked` |
| Installed status processing | `test_lifecycle.py`: `test_delivery_note_created_once`, `test_delivery_note_failure_is_recorded`, `test_installed_twice_is_rejected` |
| Patch behaviour | `tests/test_patches.py` |

Also covered: the full order-to-cash, manufacturing and procure-to-pay flows with stock and GL assertions (`test_erpnext_flows.py`), the logistics integration and webhook (`test_logistics.py`), the report (`test_monthly_value_report.py`) and the HRMS fix (`test_leave_proration.py`).

Scripts:

- `scripts/explain_monthly_value.py` reproduces the Part 10 EXPLAIN on 100k synthetic rows (throwaway table).
- `scripts/mock_logistics_server.py` is a local mock of the logistics API, and can send signed webhooks.

## Assumptions

- **One currency per order,** the company's default. Taxes are applied by ERPNext on the Sales Order (Reno Order totals are before tax).
- **Discount** is an order-level percentage, passed to the Sales Order as an additional discount on Net Total. Pricing rules are disabled on the generated Sales Order, so the agreed Reno Order rates are kept.
- **One active Sales Order per Reno Order.** Changes after confirmation go through cancel and amend.
- **Installation is the point of delivery.** On *Installed* a **draft** Delivery Note is prepared from the Sales Order; the warehouse submits it after checking what was actually delivered. The order must have a submitted Sales Order before it can be marked Installed.
- **Manufactured components** are the order lines whose item has a default BOM. Bought-in items are stock items without one.
- **"Team"** for a Sales Manager means the Sales Person hierarchy.
- **The logistics provider** is a mock (`scripts/mock_logistics_server.py`). A real one would implement the same contract: `POST /bookings`, and signed status webhooks.

## Known limitations

- **CI has not run yet.** The workflow is written and its YAML validated, but it has not run on GitHub.
- **Manufacturing is shown up to Work Order and Job Cards.** The two manufacturing Stock Entries are standard ERPNext and are described, not automated.
- **The Sales Invoice is made manually** from the Delivery Note (standard ERPNext). Auto-invoicing on *Closed* would be a small addition.
- **Existing wrong leave allocations are not recalculated** by the HRMS fix; correct them by amending.
- **The desk UI** (buttons, filters, banners) is verified through the server data it relies on and by hand; there are no browser tests.
- **Partial deliveries** (several Delivery Notes per order) work in ERPNext, but the order tracks one "primary" Delivery Note.

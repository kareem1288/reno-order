# Changelog

All notable changes to this app. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Reno Order** and **Reno Order Item**:
  - totals recomputed on the server;
  - validation of dates, quantities, rates and discount;
  - discount approval threshold and approver role in **Reno Order Settings**.
- Create **Sales Order** from a Reno Order, with duplicate prevention (a row lock plus a Sales Order validate hook).
- **Lifecycle** Draft → Confirmed → In Production → Ready for Installation → Installed → Closed, plus Cancelled:
  - role and assignment checks;
  - desk status buttons;
  - daily overdue-installation flag.
- Roles **Production User** and **Site Supervisor**; row-level permissions for own, assigned, team, supervisor, production and accounts users.
- **Installed** → draft **Delivery Note**, prepared in a background job: idempotent, de-duplicated, with retries and a manager retry action.
- **Site Supervisor API**: installation status, remarks and site photos; list of assigned installations.
- **Create → Work Orders** for manufactured lines, with BOM operations and Job Cards; sample masters for the kitchen cabinet.
- **Create → Material Request** for the shortfall of bought-in items; `reno_order` carried through RFQ → SQ → PO → PR → PI.
- **Logistics integration**:
  - booking in a background job on the `long` queue, with timeouts, backoff, retries and Integration Request logging;
  - encrypted credentials;
  - HMAC-signed delivery webhook.
- **Reno Order Monthly Value** report, with the covering index `monthly_value_idx`.
- Patch `v1_0.backfill_order_type`: batched, idempotent backfill of Order Type.
- HRMS: **Fixed Entitlement (No Proration)** on Leave Type, so event-based leave is allocated in full to mid-year joiners.
- Custom fields on core doctypes, shipped as fixtures.
- CI workflow (lint, then tests on a fresh v16 site); docs for architecture, performance, HRMS debugging and operations.

### Fixed
- The cancel check could report an empty Datetime field as changed, because casting an empty Datetime returns "now".
- The draft check in `has_permission` read the in-memory docstatus, which `submit()` sets to 1 before checking permission.

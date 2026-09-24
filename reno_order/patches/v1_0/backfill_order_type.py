# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Part 9: Order Type became mandatory; set "Standard" on existing orders that have none.

Production safety:
- post_model_sync, so the order_type column already exists when this runs.
- Only rows where order_type is NULL or '' are touched; a valid value is never overwritten.
  Running it again finds nothing to do (idempotent), so a failed or interrupted migrate can
  simply be re-run.
- Batches of BATCH_SIZE rows walked by primary key, each committed on its own: every UPDATE
  locks at most BATCH_SIZE rows for a few milliseconds instead of one long transaction
  locking ~50,000 rows (and holding a huge undo log) while users keep working.
- A direct UPDATE, not doc.save(): this is a data correction, not a user edit, so no
  validate / version rows / modified bump for 50k documents. The value is a plain constant,
  so there is no business logic to bypass.
"""

import frappe

BATCH_SIZE = 5000
DEFAULT_ORDER_TYPE = "Standard"


def execute(batch_size: int = BATCH_SIZE, commit: bool = True) -> int:
	"""Returns the number of orders updated (useful when validating on production)."""
	if not frappe.db.has_column("Reno Order", "order_type"):
		return 0

	updated = 0
	last_name = ""
	while True:
		names = frappe.db.sql_list(
			"""
			select name from `tabReno Order`
			where name > %(last_name)s and (order_type is null or order_type = '')
			order by name
			limit %(batch_size)s
			""",
			{"last_name": last_name, "batch_size": batch_size},
		)
		if not names:
			break

		frappe.db.sql(
			"""
			update `tabReno Order` set order_type = %(value)s
			where name in %(names)s and (order_type is null or order_type = '')
			""",
			{"value": DEFAULT_ORDER_TYPE, "names": names},
		)
		updated += len(names)
		last_name = names[-1]
		if commit:
			frappe.db.commit()  # release this batch's row locks before the next one

	return updated

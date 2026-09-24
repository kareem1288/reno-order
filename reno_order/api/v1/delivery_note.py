# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Installed -> Delivery Note, off the save path (Parts 2, 8, 14).

Marking an order Installed only records the status and queues a job; the user's request
never waits for ERPNext's stock mapping. The job prepares a *draft* Delivery Note from the
Sales Order: stock and the GL are touched only when the warehouse submits it, after
checking what was really delivered.

The same Delivery Note can never be generated twice:
1. transition() holds a row lock and Installed -> Installed is not a valid move, so the
   status change (and the enqueue that follows it) happens once;
2. enqueue(job_id=..., deduplicate=True) drops a second job while one is queued/running;
3. the job locks the Reno Order row and looks for an existing active Delivery Note with
   this reno_order before creating one, so a retried or duplicated job is a no-op.
"""

import frappe
from frappe import _
from frappe.utils import cint, get_link_to_form

MAX_ATTEMPTS = 3


def queue_delivery_note(reno_order: str):
	frappe.enqueue(
		"reno_order.api.v1.delivery_note.create_delivery_note",
		queue="default",  # a few seconds of ERPNext mapping: not "short", not worth "long"
		timeout=300,
		job_id=f"reno_order_delivery_note::{reno_order}",
		deduplicate=True,
		enqueue_after_commit=True,  # never run against a status change that rolled back
		reno_order=reno_order,
	)


def create_delivery_note(reno_order: str) -> str | None:
	"""Background job. Returns the Delivery Note name, or None when there is nothing to do."""
	ro = frappe.get_doc("Reno Order", reno_order, for_update=True)
	if ro.status not in ("Installed", "Closed"):
		return None  # e.g. the order changed after the job was queued

	if existing := get_active_delivery_note(ro.name):
		_record(ro, downstream_status="Created", delivery_note=existing, downstream_error=None)
		return existing

	frappe.db.savepoint("reno_delivery_note")
	try:
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		dn = make_delivery_note(ro.sales_order)
		dn.reno_order = ro.name  # also mapped from the Sales Order; set explicitly for clarity
		# System action: the user was authorised when the order moved to Installed. A Site
		# Supervisor has no Delivery Note rights, and should not need them for this.
		dn.flags.ignore_permissions = True
		dn.insert()
	except Exception as e:
		frappe.db.rollback(save_point="reno_delivery_note")
		attempts = cint(ro.downstream_attempts) + 1
		_record(
			ro,
			downstream_status="Failed",
			downstream_attempts=attempts,
			downstream_error=str(e)[:500] or type(e).__name__,
		)
		frappe.log_error(title=f"Reno Order {ro.name}: Delivery Note failed (attempt {attempts})")
		return None

	_record(ro, downstream_status="Created", delivery_note=dn.name, downstream_error=None)
	return dn.name


def get_active_delivery_note(reno_order: str) -> str | None:
	return frappe.db.get_value("Delivery Note", {"reno_order": reno_order, "docstatus": ("<", 2)}, "name")


def _record(ro, **values):
	# Derived bookkeeping on a submitted order: db_set, no validate / version noise.
	ro.db_set(values, notify=True)


@frappe.whitelist(methods=["POST"])
def retry_delivery_note(reno_order: str) -> str:
	"""Manager action for a Failed Delivery Note: POST {"reno_order": "RO-00001"}."""
	if "Sales Manager" not in frappe.get_roles() and "System Manager" not in frappe.get_roles():
		frappe.throw(_("Only a Sales Manager can retry the Delivery Note."), exc=frappe.PermissionError)
	ro = frappe.get_doc("Reno Order", reno_order)
	ro.check_permission("read")
	if ro.downstream_status != "Failed":
		frappe.throw(_("The Delivery Note for {0} has not failed.").format(ro.name))
	ro.db_set({"downstream_status": "Queued", "downstream_attempts": 0}, notify=True)
	queue_delivery_note(ro.name)
	return ro.name


# --- scheduler (hooks.py: hourly) ---


def retry_failed_delivery_notes():
	"""Re-queue failed jobs (e.g. a stock setting fixed since) a limited number of times."""
	for name in frappe.get_all(
		"Reno Order",
		filters={"downstream_status": "Failed", "downstream_attempts": ("<", MAX_ATTEMPTS), "docstatus": 1},
		pluck="name",
	):
		frappe.db.set_value("Reno Order", name, "downstream_status", "Queued", update_modified=False)
		queue_delivery_note(name)


# --- Delivery Note doc_events (hooks.py) ---


def link_reno_order(doc, method=None):
	"""A Delivery Note made by hand from the Sales Order also counts as the order's one."""
	if not doc.get("reno_order"):
		return
	current = frappe.db.get_value("Reno Order", doc.reno_order, "delivery_note")
	if current == doc.name:
		return
	if current and frappe.db.get_value("Delivery Note", current, "docstatus") in (0, 1):
		return  # keep pointing at the active one
	frappe.db.set_value(
		"Reno Order", doc.reno_order, {"delivery_note": doc.name, "downstream_status": "Created"}
	)


def get_active_delivery_note_other(reno_order: str, exclude: str) -> str | None:
	return frappe.db.get_value(
		"Delivery Note", {"reno_order": reno_order, "docstatus": ("<", 2), "name": ("!=", exclude)}, "name"
	)


def unlink_reno_order(doc, method=None):
	if (
		doc.get("reno_order")
		and frappe.db.get_value("Reno Order", doc.reno_order, "delivery_note") == doc.name
	):
		replacement = get_active_delivery_note_other(doc.reno_order, doc.name)
		frappe.db.set_value(
			"Reno Order",
			doc.reno_order,
			{"delivery_note": replacement, "downstream_status": "Created" if replacement else None},
		)
		if not replacement:
			frappe.msgprint(
				_("Reno Order {0} no longer has a Delivery Note.").format(
					get_link_to_form("Reno Order", doc.reno_order)
				),
				alert=True,
			)

# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Reno Order lifecycle (Part 2).

	Draft --submit--> Confirmed --> In Production --> Ready for Installation --> Installed --> Closed
	                      \\______________ cancel (before Installed) ______________/--> Cancelled

Draft -> Confirmed and -> Cancelled are Frappe's own submit / cancel (docstatus 0 -> 1 -> 2).
Every other move goes through transition(): one place that checks the move, the user's role,
the supervisor assignment and the preconditions. The desk buttons (Part 12) and the Site
Supervisor API (Part 6) both call it, so they cannot drift apart.

Why code rather than a Workflow DocType: "Installed" has to queue the Delivery Note exactly
once, and the mobile API must enforce the same rules as the desk. Keeping the transition
table in code makes it testable and reviewable, and an admin editing a Workflow in the UI
cannot silently break those guarantees.
"""

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime

PRODUCTION = ("Production User", "Sales Manager")
INSTALLERS = ("Site Supervisor", "Sales Manager")
CLOSERS = ("Sales Manager", "Accounts User")

# from status -> {to status: roles allowed to make the move}
TRANSITIONS: dict[str, dict[str, tuple[str, ...]]] = {
	"Confirmed": {"In Production": PRODUCTION, "Ready for Installation": PRODUCTION},
	"In Production": {"Ready for Installation": PRODUCTION},
	"Ready for Installation": {"Installed": INSTALLERS},
	"Installed": {"Closed": CLOSERS},
}

# Statuses in which an installation is still pending (drives the overdue flag).
FINISHED = ("Installed", "Closed", "Cancelled")


@frappe.whitelist(methods=["POST"])
def set_status(reno_order: str, status: str) -> dict:
	"""Move a submitted Reno Order to the next status.

	POST /api/method/reno_order.api.v1.lifecycle.set_status
	{"reno_order": "RO-00001", "status": "Installed"}
	"""
	ro = transition(reno_order, status)
	return {"reno_order": ro.name, "status": ro.status, "downstream_status": ro.downstream_status}


def transition(reno_order: str, to_status: str, remarks: str | None = None):
	# Row lock: two supervisors tapping "Installed" at once are serialised, and the second
	# one fails the transition check instead of queueing a second Delivery Note.
	ro = frappe.get_doc("Reno Order", reno_order, for_update=True)
	ro.check_permission("write")
	check_transition(ro, to_status)

	ro.status = to_status
	if remarks is not None:
		ro.installation_remarks = remarks
	if to_status == "Installed":
		ro.installed_on = now_datetime()
		ro.downstream_status = "Queued"

	ro.flags.status_change_allowed = True
	ro.flags.lifecycle_update = True
	ro.save()

	for job in on_enter(to_status):
		job(ro.name)  # each queues a background job after commit; the request never waits
	return ro


def on_enter(status: str) -> list:
	"""Background work started when an order enters `status`."""
	from reno_order.api.v1 import delivery_note, logistics

	return {
		"Ready for Installation": [logistics.queue_booking],  # book delivery to site (Part 7)
		"Installed": [delivery_note.queue_delivery_note],  # prepare the Delivery Note (Part 2)
	}.get(status, [])


def check_transition(ro, to_status: str, user: str | None = None):
	user = user or frappe.session.user
	if not ro.docstatus.is_submitted():
		frappe.throw(_("Submit the Reno Order before changing its status."), title=_("Not Submitted"))

	allowed = TRANSITIONS.get(ro.status, {})
	if to_status not in allowed:
		frappe.throw(
			_("A Reno Order cannot move from {0} to {1}.").format(
				frappe.bold(ro.status), frappe.bold(to_status)
			),
			title=_("Invalid Status Change"),
		)

	roles = set(frappe.get_roles(user))
	if not roles & set(allowed[to_status]):
		frappe.throw(
			_("Only {0} can move an order to {1}.").format(
				" / ".join(allowed[to_status]), frappe.bold(to_status)
			),
			exc=frappe.PermissionError,
			title=_("Not Permitted"),
		)

	if is_only_supervisor(roles) and ro.site_supervisor != user:
		frappe.throw(
			_("This order is not assigned to you."), exc=frappe.PermissionError, title=_("Not Permitted")
		)

	if to_status == "Installed":
		validate_ready_to_install(ro)


def validate_ready_to_install(ro):
	"""The Delivery Note is made from the Sales Order, so that has to be submitted first."""
	if not ro.sales_order:
		frappe.throw(_("Create and submit the Sales Order before marking the order Installed."))
	if frappe.db.get_value("Sales Order", ro.sales_order, "docstatus") != 1:
		frappe.throw(
			_("Submit Sales Order {0} before marking the order Installed.").format(
				frappe.bold(ro.sales_order)
			)
		)


def get_allowed_transitions(ro, user: str | None = None) -> list[str]:
	"""Statuses this user may move the order to now (drives the desk buttons)."""
	user = user or frappe.session.user
	if not ro.docstatus.is_submitted() or not ro.has_permission("write", user=user):
		return []
	roles = set(frappe.get_roles(user))
	if is_only_supervisor(roles) and ro.site_supervisor != user:
		return []
	return [to for to, allowed in TRANSITIONS.get(ro.status, {}).items() if roles & set(allowed)]


def is_only_supervisor(roles: set[str]) -> bool:
	"""A Site Supervisor who is not also a manager acts only on orders assigned to them."""
	return "Site Supervisor" in roles and not roles & {"Sales Manager", "System Manager"}


def is_overdue(ro) -> bool:
	return bool(
		ro.expected_installation_date
		and ro.status not in FINISHED
		and getdate(ro.expected_installation_date) < getdate()
	)


# --- scheduler (hooks.py: daily) ---


def flag_overdue_installations():
	"""Set / clear Reno Order.is_overdue in two set-based UPDATEs.

	The flag is derived data, so a direct UPDATE (no per-document save, no version row) is
	the right tool: it is one indexed statement per run, whatever the number of orders.
	Saves keep the flag current in between (RenoOrder.validate / before_update_after_submit
	call is_overdue).
	"""
	RO = frappe.qb.DocType("Reno Order")
	today = getdate()
	pending = RO.status.notin(FINISHED) & (RO.docstatus < 2)

	(
		frappe.qb.update(RO)
		.set(RO.is_overdue, 1)
		.where(pending & (RO.expected_installation_date < today) & (RO.is_overdue == 0))
	).run()
	(
		frappe.qb.update(RO)
		.set(RO.is_overdue, 0)
		.where((RO.is_overdue == 1) & (~pending | (RO.expected_installation_date >= today)))
	).run()


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def site_supervisor_query(doctype, txt, searchfield, start, page_len, filters):
	"""Link filter for Reno Order.site_supervisor: enabled users holding the Site Supervisor role."""
	User = frappe.qb.DocType("User")
	HasRole = frappe.qb.DocType("Has Role")
	return (
		frappe.qb.from_(User)
		.join(HasRole)
		.on((HasRole.parent == User.name) & (HasRole.parenttype == "User"))
		.select(User.name, User.full_name)
		.where(
			(HasRole.role == "Site Supervisor")
			& (User.enabled == 1)
			& ((User.name.like(f"%{txt}%")) | (User.full_name.like(f"%{txt}%")))
		)
		.distinct()
		.limit(page_len)
		.offset(start)
	).run()

# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Row-level permissions for Reno Order (Parts 2, 11), enforced on the server.

Role permissions (DocPerm) decide *what* a role may do; these hooks decide *which* orders:

	Sales User       own orders, and orders assigned to them (Assign To)
	Sales Manager    own, assigned, and orders of their sales team (Sales Person subtree)
	Site Supervisor  orders where they are the site_supervisor; write only once submitted
	Production User  submitted orders waiting for or in production; write only once submitted
	Accounts User,
	System Manager   all orders

A user with several roles sees the union. Field-level rules after submit (who may edit
which installation field) live in RenoOrder.validate_editable_after_submit.

Why Production User / Site Supervisor / Accounts User have the "submit" DocPerm: in Frappe
v16 saving a submitted document (update after submit, e.g. a status move) checks "submit",
not "write" (frappe/model/document.py check_docstatus_transition). has_permission below
then denies every non-read action on a draft to anyone who is not on the sales side, so
those roles still can never submit (or edit) a draft.

The same rule exists twice, as SQL for lists/reports (get_permission_query_conditions) and
in Python for a single document (has_permission); test_permissions checks they agree.
"""

import json

import frappe

UNRESTRICTED = {"System Manager", "Accounts User"}  # see every order
PRODUCTION_STATUSES = ("Confirmed", "In Production", "Ready for Installation")
SALES = {"Sales User", "Sales Manager"}


def get_permission_query_conditions(user: str | None = None) -> str:
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	if user == "Administrator" or roles & UNRESTRICTED:
		return ""

	table = "`tabReno Order`"
	u = frappe.db.escape(user)
	conditions = []
	if roles & SALES:
		conditions.append(f"{table}.`owner` = {u}")
		conditions.append(f"{table}.`_assign` like {frappe.db.escape('%' + json.dumps(user) + '%')}")
	if "Sales Manager" in roles and (team := get_team_sales_persons(user)):
		conditions.append(f"{table}.`sales_person` in ({', '.join(frappe.db.escape(s) for s in team)})")
	if "Site Supervisor" in roles:
		conditions.append(f"{table}.`site_supervisor` = {u}")
	if "Production User" in roles:
		statuses = ", ".join(frappe.db.escape(s) for s in PRODUCTION_STATUSES)
		conditions.append(f"({table}.`docstatus` = 1 and {table}.`status` in ({statuses}))")

	return "(" + " or ".join(conditions) + ")" if conditions else "1=0"


def has_permission(doc, ptype: str = "read", user: str | None = None, debug=False) -> bool:
	"""Can only deny. Must return True (not None) to allow: Frappe treats a falsy result as a denial."""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	if user == "Administrator" or "System Manager" in roles:
		return True
	if doc.is_new():
		return True  # creating: DocPerm "create" already decided

	reasons = visible_as(doc, user, roles)
	if not reasons:
		return False
	if ptype != "read" and not reasons & SALES and stored_docstatus(doc) == 0:
		return False  # supervisors, production and accounts never change or submit a draft
	return True


def stored_docstatus(doc) -> int:
	"""docstatus as saved, not as in memory: Document.submit() sets docstatus = 1 *before* it
	checks the "submit" permission, so the in-memory value would make every draft look submitted."""
	return frappe.db.get_value(doc.doctype, doc.name, "docstatus")


def visible_as(doc, user: str, roles: set[str]) -> set[str]:
	"""The roles through which `user` can see `doc` (empty set: not visible)."""
	reasons = roles & UNRESTRICTED
	if roles & SALES and (doc.owner == user or user in _assignees(doc)):
		reasons |= roles & SALES
	if "Sales Manager" in roles and doc.sales_person and doc.sales_person in get_team_sales_persons(user):
		reasons.add("Sales Manager")
	if "Site Supervisor" in roles and doc.site_supervisor == user:
		reasons.add("Site Supervisor")
	if "Production User" in roles and doc.docstatus == 1 and doc.status in PRODUCTION_STATUSES:
		reasons.add("Production User")
	return reasons


def _assignees(doc) -> list[str]:
	try:
		return json.loads(doc.get("_assign") or "[]")
	except ValueError:
		return []


def get_team_sales_persons(user: str) -> list[str]:
	"""Sales Persons in the subtree(s) of the user's own Sales Person record(s)."""
	cache_key = ("reno_order_team", user)
	if (team := frappe.local.cache.get(cache_key)) is not None:
		return team

	team = []
	employees = frappe.get_all("Employee", filters={"user_id": user}, pluck="name")
	if employees:
		for node in frappe.get_all(
			"Sales Person", filters={"employee": ("in", employees)}, fields=["lft", "rgt"]
		):
			team += frappe.get_all(
				"Sales Person", filters={"lft": (">=", node.lft), "rgt": ("<=", node.rgt)}, pluck="name"
			)
	team = sorted(set(team))
	frappe.local.cache[cache_key] = team
	return team

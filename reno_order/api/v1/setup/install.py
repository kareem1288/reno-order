# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""App roles, created before the DocType sync (before_install / before_migrate), because the
Reno Order DocPerms reference them.

Custom fields on core doctypes are not created here: they ship as fixtures
(reno_order/fixtures/custom_field.json, hooks.py `fixtures`), which Frappe syncs on install
and on every migrate. The `reno_order` field has the same fieldname on every downstream
document and is not no_copy, so ERPNext's own mappers carry it along SO -> DN -> SI and
MR -> RFQ -> SQ -> PO -> PR -> PI without any override.
"""

import frappe

# Sales User, Sales Manager and Accounts User come with ERPNext.
APP_ROLES = ("Production User", "Site Supervisor")


def before_install():
	make_roles()


def before_migrate():
	make_roles()


def make_roles():
	for role in APP_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)

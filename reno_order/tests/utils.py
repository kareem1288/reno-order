# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Test data builders shared by all reno_order tests.

Records are created inside the test transaction and rolled back by
IntegrationTestCase, so nothing is left behind on the site.
"""

import frappe
from frappe.utils import add_days, today

# Link targets we create ourselves; stops IntegrationTestCase from auto-creating
# (and committing) ERPNext's global test records on a non-test site.
RENO_ORDER_LINKED_DOCTYPES = [
	"Customer",
	"Address",
	"Contact",
	"Company",
	"Currency",
	"Sales Person",
	"Project",
	"Item",
	"UOM",
	"Warehouse",
	"Reno Order",
	"Sales Order",
	"User",
	"Delivery Note",
]

TEST_CUSTOMER = "_Test Reno Customer"
TEST_ITEM = "_Test Reno Cabinet"


def get_test_company() -> str:
	company = frappe.defaults.get_global_default("company") or frappe.db.get_value("Company", {}, "name")
	if not company:
		raise RuntimeError("reno_order tests need at least one Company (run the ERPNext setup wizard).")
	return company


def get_warehouse(company: str) -> str | None:
	return frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")


def make_customer(name: str = TEST_CUSTOMER) -> str:
	if not frappe.db.exists("Customer", name):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": name,
				"customer_type": "Individual",
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			}
		).insert(ignore_permissions=True)
	return name


def make_item(item_code: str = TEST_ITEM, is_stock_item: int = 1) -> str:
	if not frappe.db.exists("Item", item_code):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_code,
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"stock_uom": "Nos",
				"is_stock_item": is_stock_item,
				"is_sales_item": 1,
			}
		).insert(ignore_permissions=True)
	return item_code


def make_user(email: str, roles: list[str]) -> str:
	if not frappe.db.exists("User", email):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
	else:
		user = frappe.get_doc("User", email)
	user.add_roles(*roles)
	return email


def make_reno_order(items: list[dict] | None = None, do_not_save=False, do_not_submit=False, **fields):
	company = fields.pop("company", None) or get_test_company()
	doc = frappe.get_doc(
		{
			"doctype": "Reno Order",
			"customer": make_customer(),
			"company": company,
			"transaction_date": today(),
			"expected_installation_date": add_days(today(), 14),
			"items": items
			or [{"item_code": make_item(), "qty": 2, "rate": 1000, "warehouse": get_warehouse(company)}],
			**fields,
		}
	)
	if do_not_save:
		return doc
	doc.insert()
	if not do_not_submit:
		doc.submit()
	return doc


def set_discount_settings(threshold: float, approver_role: str = "Sales Manager"):
	settings = frappe.get_single("Reno Order Settings")
	settings.discount_approval_threshold = threshold
	settings.discount_approver_role = approver_role
	settings.save(ignore_permissions=True)


def submit_sales_order_for(ro) -> str:
	"""Create (via the real endpoint) and submit the Sales Order for a submitted Reno Order."""
	from reno_order.api.v1.sales_order import make_sales_order

	so = frappe.get_doc("Sales Order", make_sales_order(ro.name))
	so.submit()
	return so.name


def make_sales_person(name: str, user: str | None = None, parent: str | None = None, is_group=0) -> str:
	"""A Sales Person, optionally tied to `user` through an Employee (how team membership is resolved)."""
	if not frappe.db.exists("Sales Person", name):
		employee = None
		if user:
			employee = (
				frappe.db.get_value("Employee", {"user_id": user}, "name")
				or frappe.get_doc(
					{
						"doctype": "Employee",
						"first_name": name,
						"company": get_test_company(),
						"status": "Active",
						"gender": frappe.db.get_value("Gender", {}, "name") or "Male",
						"date_of_birth": "1990-01-01",
						"date_of_joining": "2020-01-01",
						"user_id": user,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)
		frappe.get_doc(
			{
				"doctype": "Sales Person",
				"sales_person_name": name,
				"parent_sales_person": parent or frappe.db.get_value("Sales Person", {"is_group": 1}, "name"),
				"is_group": is_group,
				"employee": employee,
			}
		).insert(ignore_permissions=True)
	return name

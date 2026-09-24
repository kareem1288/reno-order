# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# See license.txt

import json

import frappe
from frappe.desk.form.linked_with import get_submitted_linked_docs
from frappe.desk.form.save import savedocs
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, today

from reno_order.api.v1.sales_order import make_sales_order
from reno_order.tests.utils import (
	RENO_ORDER_LINKED_DOCTYPES,
	make_item,
	make_reno_order,
	make_user,
	set_discount_settings,
)

IGNORE_TEST_RECORD_DEPENDENCIES = RENO_ORDER_LINKED_DOCTYPES

SALES_USER = "reno.sales@example.com"
SALES_MANAGER = "reno.manager@example.com"


class TestRenoOrder(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(SALES_USER, ["Sales User"])
		make_user(SALES_MANAGER, ["Sales User", "Sales Manager"])
		set_discount_settings(threshold=10, approver_role="Sales Manager")

	def tearDown(self):
		frappe.set_user("Administrator")

	# --- totals ---

	def test_totals_are_calculated(self):
		ro = make_reno_order(
			items=[
				{"item_code": make_item(), "qty": 2, "rate": 1500},
				{"item_code": make_item(), "qty": 3, "rate": 250.5},
			],
			discount_percentage=5,
			do_not_submit=True,
		)
		self.assertEqual([r.amount for r in ro.items], [3000, 751.5])
		self.assertEqual(ro.total_amount, 3751.5)
		self.assertAlmostEqual(ro.discount_amount, 187.58, places=2)
		self.assertAlmostEqual(ro.grand_total, 3563.92, places=2)

	def test_client_supplied_totals_are_overwritten(self):
		ro = make_reno_order(do_not_save=True)
		ro.items[0].amount = 1
		ro.total_amount = 1
		ro.discount_amount = 0
		ro.grand_total = 1
		ro.insert()
		self.assertEqual(ro.items[0].amount, 2000)
		self.assertEqual(ro.grand_total, 2000)

		# Same through a save of an existing draft, as a REST PUT would do
		ro.grand_total = 1
		ro.save()
		ro.reload()
		self.assertEqual(ro.grand_total, 2000)

	# --- invalid states ---

	def test_negative_quantity_rejected(self):
		ro = make_reno_order(items=[{"item_code": make_item(), "qty": -1, "rate": 100}], do_not_save=True)
		self.assertRaises(frappe.ValidationError, ro.insert)

	def test_negative_rate_rejected(self):
		ro = make_reno_order(items=[{"item_code": make_item(), "qty": 1, "rate": -5}], do_not_save=True)
		self.assertRaises(frappe.ValidationError, ro.insert)

	def test_installation_before_order_date_rejected(self):
		ro = make_reno_order(expected_installation_date=add_days(today(), -1), do_not_save=True)
		self.assertRaises(frappe.ValidationError, ro.insert)

	def test_discount_over_100_rejected(self):
		ro = make_reno_order(discount_percentage=120, do_not_save=True)
		self.assertRaises(frappe.ValidationError, ro.insert)

	# --- discount authorisation ---

	def test_high_discount_draft_can_be_saved_by_sales_user(self):
		frappe.set_user(SALES_USER)
		ro = make_reno_order(discount_percentage=25, do_not_submit=True)
		self.assertEqual(ro.docstatus, 0)

	def test_high_discount_submit_blocked_for_sales_user(self):
		frappe.set_user(SALES_USER)
		ro = make_reno_order(discount_percentage=25, do_not_submit=True)
		self.assertRaises(frappe.PermissionError, ro.submit)

	def test_high_discount_submit_allowed_for_approver(self):
		frappe.set_user(SALES_MANAGER)
		ro = make_reno_order(discount_percentage=25)
		self.assertEqual(ro.docstatus, 1)
		self.assertEqual(ro.status, "Confirmed")

	def test_discount_within_threshold_allowed_for_sales_user(self):
		frappe.set_user(SALES_USER)
		ro = make_reno_order(discount_percentage=10)
		self.assertEqual(ro.docstatus, 1)

	# --- status ---

	def test_status_cannot_be_set_directly_after_submit(self):
		ro = make_reno_order()
		ro.status = "Installed"
		self.assertRaises(frappe.PermissionError, ro.save)

	def test_cancel_sets_cancelled_status(self):
		ro = make_reno_order()
		ro.cancel()
		self.assertEqual(ro.status, "Cancelled")

	# --- Sales Order ---

	def test_make_sales_order(self):
		ro = make_reno_order(discount_percentage=5)
		so_name = make_sales_order(ro.name)

		so = frappe.get_doc("Sales Order", so_name)
		self.assertEqual(so.reno_order, ro.name)
		self.assertEqual(so.customer, ro.customer)
		self.assertEqual(getdate(so.delivery_date), getdate(ro.expected_installation_date))
		self.assertEqual(so.items[0].reno_order_item, ro.items[0].name)
		self.assertEqual(so.items[0].rate, ro.items[0].rate)
		self.assertEqual(so.additional_discount_percentage, 5)
		self.assertEqual(so.net_total, ro.grand_total)
		self.assertEqual(frappe.db.get_value("Reno Order", ro.name, "sales_order"), so_name)

	def test_sales_order_requires_submitted_reno_order(self):
		ro = make_reno_order(do_not_submit=True)
		self.assertRaises(frappe.ValidationError, make_sales_order, ro.name)

	def test_duplicate_sales_order_blocked(self):
		ro = make_reno_order()
		make_sales_order(ro.name)
		self.assertRaises(frappe.DuplicateEntryError, make_sales_order, ro.name)

	def test_duplicate_sales_order_via_copy_blocked(self):
		ro = make_reno_order()
		so = frappe.get_doc("Sales Order", make_sales_order(ro.name))
		copy = frappe.copy_doc(so)
		self.assertEqual(copy.reno_order, ro.name)
		self.assertRaises(frappe.DuplicateEntryError, copy.insert)

	def test_sales_order_can_be_recreated_after_delete(self):
		ro = make_reno_order()
		frappe.delete_doc("Sales Order", make_sales_order(ro.name))
		self.assertIsNone(frappe.db.get_value("Reno Order", ro.name, "sales_order"))
		self.assertTrue(make_sales_order(ro.name))

	def test_sales_order_link_follows_amendment(self):
		ro = make_reno_order()
		so = frappe.get_doc("Sales Order", make_sales_order(ro.name))
		so.submit()
		so.cancel()
		self.assertIsNone(frappe.db.get_value("Reno Order", ro.name, "sales_order"))

		amended = frappe.copy_doc(so)
		amended.amended_from = so.name
		amended.docstatus = 0
		amended.insert()
		self.assertEqual(frappe.db.get_value("Reno Order", ro.name, "sales_order"), amended.name)

	def test_sales_order_link_cannot_be_changed_by_client(self):
		ro = make_reno_order()
		make_sales_order(ro.name)
		ro.reload()
		ro.sales_order = None
		self.assertRaises(frappe.UpdateAfterSubmitError, ro.save)

	def test_sales_order_cancel_does_not_offer_reno_order(self):
		# sales_order.js adds Reno Order to ignore_doctypes_on_cancel_all; this is the server call it feeds.
		self.assertIn(
			"public/js/sales_order.js", frappe.get_hooks("doctype_js", app_name="reno_order")["Sales Order"]
		)
		ro = make_reno_order()
		so = frappe.get_doc("Sales Order", make_sales_order(ro.name))
		so.submit()
		linked = get_submitted_linked_docs(
			"Sales Order", so.name, ignore_doctypes_on_cancel_all=["Reno Order"]
		)
		self.assertEqual(linked["docs"], [])
		so.cancel()
		self.assertIsNone(frappe.db.get_value("Reno Order", ro.name, "sales_order"))

	def test_zero_rate_line_stays_free_on_sales_order(self):
		item = make_item("_Test Reno Free Hinge")
		price_list = (
			frappe.db.get_single_value("Selling Settings", "selling_price_list") or "Standard Selling"
		)
		frappe.get_doc(
			{"doctype": "Item Price", "item_code": item, "price_list": price_list, "price_list_rate": 500}
		).insert()

		ro = make_reno_order(
			items=[
				{"item_code": item, "qty": 1, "rate": 0},
				{"item_code": make_item(), "qty": 1, "rate": 100},
			]
		)
		so = frappe.get_doc("Sales Order", make_sales_order(ro.name))
		self.assertEqual(so.items[0].rate, 0)
		self.assertEqual(so.net_total, ro.grand_total)

	# --- hostile input (found by monkey testing) ---

	def test_cancel_with_edited_payload_rejected(self):
		ro = make_reno_order()
		payload = ro.as_dict(convert_dates_to_str=True)
		payload.update(docstatus=2, grand_total=1, customer_name="Someone Else", items=[])
		frappe.local.response = frappe._dict(docs=[])
		self.assertRaises(frappe.ValidationError, savedocs, json.dumps(payload, default=str), "Cancel")

		stored = frappe.get_doc("Reno Order", ro.name)
		self.assertEqual(stored.docstatus, 1)
		self.assertEqual(stored.grand_total, ro.grand_total)
		self.assertEqual(len(stored.items), 1)

	def test_unparseable_date_rejected(self):
		for value in (20260101, "0001-01-01"):
			with self.subTest(value=value):
				ro = make_reno_order(transaction_date=value, do_not_save=True)
				self.assertRaises(frappe.ValidationError, ro.insert)

	def test_non_finite_and_huge_numbers_rejected(self):
		for qty, rate in ((float("nan"), 10), (1, float("nan")), (float("inf"), 10), (1, 1e15), (1e6, 1e7)):
			with self.subTest(qty=qty, rate=rate):
				ro = make_reno_order(
					items=[{"item_code": make_item(), "qty": qty, "rate": rate}], do_not_save=True
				)
				self.assertRaises(frappe.ValidationError, ro.insert)

	def test_disabled_item_rejected(self):
		item = make_item("_Test Reno Disabled Item")
		frappe.db.set_value("Item", item, "disabled", 1)
		ro = make_reno_order(items=[{"item_code": item, "qty": 1, "rate": 10}], do_not_save=True)
		self.assertRaises(frappe.ValidationError, ro.insert)

	def test_client_cannot_choose_naming_series(self):
		ro = make_reno_order(naming_series="HACK-.###", do_not_save=True)
		self.assertRaises(frappe.ValidationError, ro.insert)

	def test_fraction_of_whole_number_uom_rejected(self):
		frappe.db.set_value("UOM", "Nos", "must_be_whole_number", 1)
		ro = make_reno_order(
			items=[{"item_code": make_item(), "qty": 0.5, "rate": 10, "uom": "Nos"}], do_not_save=True
		)
		self.assertRaises(frappe.ValidationError, ro.insert)

	def test_qty_rounded_to_field_precision(self):
		frappe.db.set_value("UOM", "Nos", "must_be_whole_number", 0)
		ro = make_reno_order(
			items=[{"item_code": make_item(), "qty": 1.0000001, "rate": 10}], do_not_submit=True
		)
		self.assertEqual(ro.items[0].qty, 1)

	def test_fetched_fields_are_reset_from_their_source(self):
		ro = make_reno_order(do_not_submit=True)
		ro.customer_name = "'; DROP TABLE tabItem; --"
		ro.currency = "USD"
		ro.items[0].item_name = "Something Else"
		ro.submit()
		ro.reload()
		self.assertEqual(ro.customer_name, frappe.db.get_value("Customer", ro.customer, "customer_name"))
		self.assertEqual(ro.currency, frappe.db.get_value("Company", ro.company, "default_currency"))
		self.assertEqual(
			ro.items[0].item_name, frappe.db.get_value("Item", ro.items[0].item_code, "item_name")
		)

	def test_client_cannot_claim_a_sales_order(self):
		so_name = make_sales_order(make_reno_order().name)
		other = make_reno_order(sales_order=so_name, do_not_submit=True)
		self.assertIsNone(other.sales_order)

	def test_cancel_blocked_while_draft_sales_order_exists(self):
		ro = make_reno_order()
		make_sales_order(ro.name)
		ro.reload()
		self.assertRaises(frappe.LinkExistsError, ro.cancel)

	def test_amended_from_cannot_be_changed(self):
		ro = make_reno_order()
		ro.cancel()
		amended = frappe.copy_doc(ro)
		amended.amended_from = ro.name
		amended.docstatus = 0
		amended.insert()
		amended.amended_from = None
		self.assertRaises(frappe.CannotChangeConstantError, amended.save)

	def test_description_sanitized_when_inserted_as_submitted(self):
		ro = make_reno_order(
			items=[
				{"item_code": make_item(), "qty": 1, "rate": 10, "description": "<script>alert(1)</script>ok"}
			],
			do_not_save=True,
		)
		ro.docstatus = 1
		ro.insert()
		self.assertNotIn(
			"<script", frappe.db.get_value("Reno Order Item", {"parent": ro.name}, "description")
		)

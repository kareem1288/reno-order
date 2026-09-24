# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# See license.txt

"""Parts 3-5: the Reno Order drives standard ERPNext documents, and the reference follows.

Each test also asserts *where* stock and accounting happen, the points the architecture
note explains:
- Sales Order submit: reserves nothing in the ledgers (only Bin.reserved_qty).
- Delivery Note submit: Stock Ledger out + GL (Stock In Hand Cr / Cost of Goods Sold Dr).
- Sales Invoice submit: GL only (Debtors Dr / Sales Cr), no stock movement (update_stock off).
- Purchase Receipt submit: Stock Ledger in + GL (Stock In Hand Dr / Stock Received But Not Billed Cr).
- Purchase Invoice submit: GL only (SRBNB Dr / Creditors Cr).
"""

from unittest.mock import patch

import frappe
from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
from erpnext.buying.doctype.request_for_quotation.request_for_quotation import (
	make_supplier_quotation_from_rfq,
)
from erpnext.buying.doctype.supplier_quotation.supplier_quotation import make_purchase_order
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice
from erpnext.stock.doctype.material_request.material_request import make_request_for_quotation
from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from reno_order.api.v1 import buying, delivery_note, manufacturing
from reno_order.api.v1.lifecycle import transition
from reno_order.api.v1.setup import sample_data
from reno_order.tests.utils import get_test_company, get_warehouse, make_reno_order, submit_sales_order_for


def receive_stock(item_code, warehouse, qty, rate=100):
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"company": get_test_company(),
			"items": [{"item_code": item_code, "qty": qty, "t_warehouse": warehouse, "basic_rate": rate}],
		}
	).insert()
	se.submit()


def stock_moves(voucher_type, voucher_no) -> float:
	return flt(
		frappe.db.sql(
			"""select sum(actual_qty) from `tabStock Ledger Entry`
			where voucher_type = %s and voucher_no = %s and is_cancelled = 0""",
			(voucher_type, voucher_no),
		)[0][0]
	)


def bought_item(code) -> str:
	"""A fresh purchased stock item, so stock received in one test cannot leak into another."""
	return sample_data.make_item(code, code, "Nos", valuation_rate=150)


def gl_accounts(voucher_type, voucher_no) -> set[str]:
	return set(
		frappe.get_all(
			"GL Entry",
			filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
			pluck="account",
		)
	)


class TestERPNextFlows(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = get_test_company()
		cls.warehouse = get_warehouse(cls.company)
		cls.bom = sample_data.setup(cls.company)

	def setUp(self):
		patcher = patch.object(delivery_note, "queue_delivery_note")  # the job is run by hand below
		patcher.start()
		self.addCleanup(patcher.stop)

	def order(self, item_code, qty, rate):
		return make_reno_order(
			items=[{"item_code": item_code, "qty": qty, "rate": rate, "warehouse": self.warehouse}]
		)

	# --- Part 3: order to cash ---

	def test_order_to_cash(self):
		ro = self.order(sample_data.HINGE, 10, 450)
		so_name = submit_sales_order_for(ro)
		self.assertEqual(stock_moves("Sales Order", so_name), 0)  # an order moves no stock

		receive_stock(sample_data.HINGE, self.warehouse, 10)
		transition(ro.name, "Ready for Installation")
		transition(ro.name, "Installed")
		dn = frappe.get_doc("Delivery Note", delivery_note.create_delivery_note(ro.name))
		self.assertEqual((dn.reno_order, dn.items[0].against_sales_order), (ro.name, so_name))
		dn.submit()
		self.assertEqual(stock_moves("Delivery Note", dn.name), -10)
		self.assertTrue(gl_accounts("Delivery Note", dn.name))  # perpetual inventory: stock + COGS

		si = make_sales_invoice(dn.name)
		si.insert()
		si.submit()
		self.assertEqual(si.reno_order, ro.name)
		self.assertEqual(stock_moves("Sales Invoice", si.name), 0)  # billing only
		self.assertIn(si.debit_to, gl_accounts("Sales Invoice", si.name))

		transition(ro.name, "Closed")
		links = {
			dt: frappe.get_all(dt, filters={"reno_order": ro.name, "docstatus": 1}, pluck="name")
			for dt in ("Sales Order", "Delivery Note", "Sales Invoice")
		}
		self.assertEqual(
			links, {"Sales Order": [so_name], "Delivery Note": [dn.name], "Sales Invoice": [si.name]}
		)

	def test_sales_order_cannot_be_cancelled_after_delivery(self):
		ro = self.order(sample_data.HINGE, 2, 450)
		so_name = submit_sales_order_for(ro)
		receive_stock(sample_data.HINGE, self.warehouse, 2)
		transition(ro.name, "Ready for Installation")
		transition(ro.name, "Installed")
		frappe.get_doc("Delivery Note", delivery_note.create_delivery_note(ro.name)).submit()
		self.assertRaises(frappe.LinkExistsError, frappe.get_doc("Sales Order", so_name).cancel)

	# --- Part 4: manufacturing ---

	def test_work_orders_from_reno_order(self):
		ro = self.order(sample_data.CABINET, 3, 18500)
		submit_sales_order_for(ro)
		names = manufacturing.make_work_orders(ro.name)
		self.assertEqual(len(names), 1)
		wo = frappe.get_doc("Work Order", names[0])
		self.assertEqual(
			(wo.production_item, wo.bom_no, wo.qty, wo.reno_order),
			(sample_data.CABINET, self.bom, 3, ro.name),
		)
		self.assertEqual([op.operation for op in wo.operations], ["Cutting", "Assembly", "Finishing"])
		self.assertEqual({r.item_code for r in wo.required_items}, {r[0] for r in sample_data.RAW_MATERIALS})

		self.assertEqual(manufacturing.make_work_orders(ro.name), [])  # no duplicate Work Order

		wo.submit()
		job_cards = frappe.get_all("Job Card", filters={"work_order": wo.name}, pluck="operation")
		self.assertEqual(sorted(job_cards), ["Assembly", "Cutting", "Finishing"])

	def test_bought_items_get_no_work_order(self):
		ro = self.order(sample_data.HINGE, 3, 450)
		self.assertEqual(manufacturing.make_work_orders(ro.name), [])

	# --- Part 5: buying ---

	def test_material_request_only_for_the_shortage(self):
		item = bought_item("_Test Reno Handle A")
		receive_stock(item, self.warehouse, 5)
		ro = self.order(item, 12, 450)
		mr = frappe.get_doc("Material Request", buying.make_material_request(ro.name))
		self.assertEqual(mr.reno_order, ro.name)
		self.assertEqual(mr.items[0].qty, 7)  # 12 needed, 5 in stock
		self.assertIsNone(buying.make_material_request(ro.name))  # already requested

	def test_nothing_requested_when_in_stock(self):
		item = bought_item("_Test Reno Handle B")
		receive_stock(item, self.warehouse, 1000)
		ro = self.order(item, 4, 450)
		self.assertIsNone(buying.make_material_request(ro.name))

	def test_procure_to_pay(self):
		ro = self.order(bought_item("_Test Reno Handle C"), 40, 450)
		submit_sales_order_for(ro)
		mr = frappe.get_doc("Material Request", buying.make_material_request(ro.name))
		mr.submit()

		rfq = make_request_for_quotation(mr.name)
		rfq.message_for_supplier = "Please quote for soft-close hinges."
		rfq.append("suppliers", {"supplier": sample_data.SUPPLIER, "send_email": 0})
		rfq.insert()
		rfq.submit()

		sq = make_supplier_quotation_from_rfq(rfq.name, for_supplier=sample_data.SUPPLIER)
		for row in sq.items:
			row.rate = 175
		sq.insert()
		sq.submit()

		po = make_purchase_order(sq.name)
		po.schedule_date = add_days(nowdate(), 3)
		for row in po.items:
			row.schedule_date = po.schedule_date
		po.insert()
		po.submit()
		self.assertEqual(stock_moves("Purchase Order", po.name), 0)  # ordering moves no stock

		pr = make_purchase_receipt(po.name)
		pr.insert()
		pr.submit()
		self.assertEqual(stock_moves("Purchase Receipt", pr.name), mr.items[0].qty)
		self.assertTrue(gl_accounts("Purchase Receipt", pr.name))  # Stock In Hand / SRBNB

		pi = make_purchase_invoice(pr.name)
		pi.bill_no, pi.bill_date = "HET-778", nowdate()
		pi.insert()
		pi.submit()
		self.assertEqual(stock_moves("Purchase Invoice", pi.name), 0)
		self.assertIn(pi.credit_to, gl_accounts("Purchase Invoice", pi.name))

		for doc in (rfq, sq, po, pr, pi):
			self.assertEqual(frappe.db.get_value(doc.doctype, doc.name, "reno_order"), ro.name, doc.doctype)

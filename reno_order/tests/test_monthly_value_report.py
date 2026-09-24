# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# See license.txt

"""Part 10: Monthly Value by Status report."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_months, get_first_day, today

from reno_order.reno_order.report.reno_order_monthly_value import reno_order_monthly_value as report
from reno_order.tests.utils import make_reno_order, make_user

SALES = "reno.rpt.sales@example.com"


class TestMonthlyValueReport(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(SALES, ["Sales User"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def value(self, rows, month, status):
		return sum(r.value for r in rows if r.month == month and r.status == status)

	def test_groups_by_month_and_status(self):
		month = today()[:7]
		before = report.get_rows(get_first_day(add_months(today(), -11)), today())
		make_reno_order(items=[{"item_code": "_Test Reno Cabinet", "qty": 1, "rate": 1000}])  # Confirmed
		make_reno_order(
			items=[{"item_code": "_Test Reno Cabinet", "qty": 2, "rate": 1000}], do_not_submit=True
		)
		after = report.get_rows(get_first_day(add_months(today(), -11)), today())
		self.assertEqual(self.value(after, month, "Confirmed") - self.value(before, month, "Confirmed"), 1000)
		self.assertEqual(self.value(after, month, "Draft") - self.value(before, month, "Draft"), 2000)

	def test_cancelled_orders_are_excluded(self):
		ro = make_reno_order()
		ro.cancel()
		rows = report.get_rows(get_first_day(add_months(today(), -11)), today())
		self.assertFalse([r for r in rows if r.status == "Cancelled"])

	def test_respects_row_permissions(self):
		make_reno_order()  # owned by Administrator
		frappe.set_user(SALES)
		own = make_reno_order(items=[{"item_code": "_Test Reno Cabinet", "qty": 3, "rate": 1000}])
		rows = report.get_rows(get_first_day(add_months(today(), -11)), today(), user=SALES)
		self.assertEqual(sum(r.value for r in rows), own.grand_total)

	def test_execute_returns_columns_rows_and_chart(self):
		columns, _rows, _message, chart = report.execute({})
		self.assertEqual([c["fieldname"] for c in columns], ["month", "status", "orders", "value"])
		self.assertEqual(chart["type"], "bar")

# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# See license.txt

"""Part 9: the Order Type backfill patch."""

import frappe
from frappe.tests import IntegrationTestCase

from reno_order.patches.v1_0 import backfill_order_type
from reno_order.tests.utils import make_reno_order


class TestBackfillOrderType(IntegrationTestCase):
	def setUp(self):
		self.blank = [make_reno_order(do_not_submit=True).name for _ in range(5)]
		self.premium = make_reno_order(do_not_submit=True, order_type="Premium").name
		# Simulate rows that existed before the field was introduced.
		frappe.db.sql(
			"update `tabReno Order` set order_type = null where name in %(names)s", {"names": self.blank[:3]}
		)
		frappe.db.sql(
			"update `tabReno Order` set order_type = '' where name in %(names)s", {"names": self.blank[3:]}
		)

	def order_type(self, name):
		return frappe.db.get_value("Reno Order", name, "order_type")

	def test_blank_rows_get_standard(self):
		# batch_size 2 forces several batches; commit=False keeps the test transaction intact
		backfill_order_type.execute(batch_size=2, commit=False)
		self.assertEqual({self.order_type(n) for n in self.blank}, {"Standard"})

	def test_existing_values_are_not_overwritten(self):
		backfill_order_type.execute(batch_size=2, commit=False)
		self.assertEqual(self.order_type(self.premium), "Premium")

	def test_idempotent(self):
		first = backfill_order_type.execute(batch_size=2, commit=False)
		second = backfill_order_type.execute(batch_size=2, commit=False)
		self.assertGreaterEqual(first, len(self.blank))
		self.assertEqual(second, 0)
		self.assertEqual(frappe.db.count("Reno Order", {"order_type": ("in", ("", None))}), 0)

# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# See license.txt

"""Part 2: lifecycle transitions, Installed -> Delivery Note job, after-submit field rules, scheduler."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, today

from reno_order.api.v1 import delivery_note, lifecycle
from reno_order.api.v1.lifecycle import set_status, transition
from reno_order.tests.utils import (
	RENO_ORDER_LINKED_DOCTYPES,
	make_reno_order,
	make_user,
	submit_sales_order_for,
)

IGNORE_TEST_RECORD_DEPENDENCIES = RENO_ORDER_LINKED_DOCTYPES

MANAGER = "reno.lc.manager@example.com"
SALES = "reno.lc.sales@example.com"
PRODUCTION = "reno.lc.production@example.com"
SUPERVISOR = "reno.lc.supervisor@example.com"
OTHER_SUPERVISOR = "reno.lc.supervisor2@example.com"
ACCOUNTS = "reno.lc.accounts@example.com"


class TestRenoOrderLifecycle(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(MANAGER, ["Sales User", "Sales Manager"])
		make_user(SALES, ["Sales User"])
		make_user(PRODUCTION, ["Production User"])
		make_user(SUPERVISOR, ["Site Supervisor"])
		make_user(OTHER_SUPERVISOR, ["Site Supervisor"])
		make_user(ACCOUNTS, ["Accounts User"])

	def setUp(self):
		# queue_delivery_note is exercised on its own; here just record the calls.
		self.queue_patcher = patch.object(delivery_note, "queue_delivery_note")
		self.queued = self.queue_patcher.start()
		self.addCleanup(self.queue_patcher.stop)

	def tearDown(self):
		frappe.set_user("Administrator")

	def ready_order(self, **fields):
		"""A submitted order with a submitted Sales Order, at Ready for Installation."""
		ro = make_reno_order(site_supervisor=SUPERVISOR, **fields)
		submit_sales_order_for(ro)
		transition(ro.name, "Ready for Installation")
		return frappe.get_doc("Reno Order", ro.name)

	# --- transitions ---

	def test_full_lifecycle(self):
		ro = make_reno_order(site_supervisor=SUPERVISOR)
		self.assertEqual(ro.status, "Confirmed")
		submit_sales_order_for(ro)
		for status in ("In Production", "Ready for Installation", "Installed", "Closed"):
			self.assertEqual(transition(ro.name, status).status, status)
		ro.reload()
		self.assertTrue(ro.installed_on)
		self.queued.assert_called_once_with(ro.name)

	def test_skipping_or_going_back_is_rejected(self):
		ro = make_reno_order()
		for status in ("Installed", "Closed", "Draft", "Confirmed", "Cancelled", "Nonsense"):
			with self.assertRaises(frappe.ValidationError, msg=status):
				transition(ro.name, status)

	def test_draft_cannot_change_status(self):
		ro = make_reno_order(do_not_submit=True)
		self.assertRaises(frappe.ValidationError, transition, ro.name, "In Production")

	def test_installed_twice_is_rejected(self):
		ro = self.ready_order()
		transition(ro.name, "Installed")
		self.assertRaises(frappe.ValidationError, transition, ro.name, "Installed")
		self.queued.assert_called_once()

	def test_installed_requires_submitted_sales_order(self):
		ro = make_reno_order(site_supervisor=SUPERVISOR)
		transition(ro.name, "Ready for Installation")
		self.assertRaises(frappe.ValidationError, transition, ro.name, "Installed")

	def test_set_status_endpoint(self):
		ro = self.ready_order()
		frappe.set_user(SUPERVISOR)
		out = set_status(ro.name, "Installed")
		self.assertEqual(out, {"reno_order": ro.name, "status": "Installed", "downstream_status": "Queued"})

	# --- roles ---

	def test_sales_user_cannot_move_production(self):
		ro = make_reno_order()
		frappe.set_user(SALES)
		self.assertRaises(frappe.PermissionError, transition, ro.name, "In Production")

	def test_production_user_moves_production_only(self):
		ro = make_reno_order(site_supervisor=SUPERVISOR)
		submit_sales_order_for(ro)
		frappe.set_user(PRODUCTION)
		transition(ro.name, "In Production")
		transition(ro.name, "Ready for Installation")
		# Ready for Installation is no longer a production status, so the order leaves their view.
		self.assertRaises(frappe.PermissionError, transition, ro.name, "Installed")

	def test_assigned_supervisor_marks_installed(self):
		ro = self.ready_order()
		frappe.set_user(SUPERVISOR)
		self.assertEqual(lifecycle.get_allowed_transitions(ro), ["Installed"])
		self.assertEqual(transition(ro.name, "Installed").status, "Installed")

	def test_other_supervisor_cannot_mark_installed(self):
		ro = self.ready_order()
		frappe.set_user(OTHER_SUPERVISOR)
		self.assertEqual(lifecycle.get_allowed_transitions(ro), [])
		self.assertRaises(frappe.PermissionError, transition, ro.name, "Installed")

	def test_accounts_user_closes(self):
		ro = self.ready_order()
		transition(ro.name, "Installed")
		frappe.set_user(ACCOUNTS)
		self.assertEqual(transition(ro.name, "Closed").status, "Closed")

	# --- after-submit field rules ---

	def test_status_and_server_fields_cannot_be_set_by_client(self):
		ro = self.ready_order()
		ro.status = "Installed"
		self.assertRaises(frappe.PermissionError, ro.save)

		ro.reload()
		ro.downstream_status = "Created"
		ro.installed_on = "2026-01-01 10:00:00"
		ro.save()
		ro.reload()
		self.assertFalse(ro.downstream_status)
		self.assertFalse(ro.installed_on)

	def test_supervisor_edits_remarks_only(self):
		ro = self.ready_order()
		frappe.set_user(SUPERVISOR)
		ro = frappe.get_doc("Reno Order", ro.name)
		ro.installation_remarks = "Wall is uneven, needs packing."
		ro.save()

		ro.reload()
		ro.expected_installation_date = add_days(ro.expected_installation_date, 3)
		self.assertRaises(frappe.PermissionError, ro.save)

		ro.reload()
		ro.site_supervisor = OTHER_SUPERVISOR
		self.assertRaises(frappe.PermissionError, ro.save)

	def test_commercial_fields_locked_after_submit(self):
		ro = self.ready_order()
		ro.discount_percentage = 50
		self.assertRaises(frappe.UpdateAfterSubmitError, ro.save)

	def test_installed_order_cannot_be_cancelled(self):
		ro = self.ready_order()
		transition(ro.name, "Installed")
		ro.reload()
		self.assertRaises(frappe.ValidationError, ro.cancel)

	# --- Delivery Note job ---

	def test_delivery_note_created_once(self):
		ro = self.ready_order()
		transition(ro.name, "Installed")

		first = delivery_note.create_delivery_note(ro.name)
		second = delivery_note.create_delivery_note(ro.name)  # retried / duplicated job

		self.assertTrue(first)
		self.assertEqual(first, second)
		self.assertEqual(frappe.db.count("Delivery Note", {"reno_order": ro.name}), 1)
		dn = frappe.get_doc("Delivery Note", first)
		self.assertEqual(dn.docstatus, 0)  # prepared, not posted: stock moves when the warehouse submits
		ro.reload()
		self.assertEqual((ro.delivery_note, ro.downstream_status), (first, "Created"))

	def test_delivery_note_job_skips_orders_not_installed(self):
		ro = self.ready_order()
		self.assertIsNone(delivery_note.create_delivery_note(ro.name))
		self.assertEqual(frappe.db.count("Delivery Note", {"reno_order": ro.name}), 0)

	def test_delivery_note_failure_is_recorded(self):
		ro = self.ready_order()
		transition(ro.name, "Installed")
		with patch(
			"erpnext.selling.doctype.sales_order.sales_order.make_delivery_note",
			side_effect=frappe.ValidationError("Warehouse is mandatory"),
		):
			self.assertIsNone(delivery_note.create_delivery_note(ro.name))
		ro.reload()
		self.assertEqual(ro.downstream_status, "Failed")
		self.assertEqual(ro.downstream_attempts, 1)
		self.assertIn("Warehouse is mandatory", ro.downstream_error)
		self.assertEqual(ro.status, "Installed")  # the savepoint rolled back only the Delivery Note

	def test_cancelling_delivery_note_unlinks_it(self):
		ro = self.ready_order()
		transition(ro.name, "Installed")
		name = delivery_note.create_delivery_note(ro.name)
		frappe.delete_doc("Delivery Note", name)
		ro.reload()
		self.assertFalse(ro.delivery_note)

	def test_queue_is_deduplicated_and_after_commit(self):
		self.queue_patcher.stop()  # use the real queue_delivery_note here
		with patch("frappe.enqueue") as enqueue:
			delivery_note.queue_delivery_note("RO-00042")
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["job_id"], "reno_order_delivery_note::RO-00042")
		self.assertTrue(kwargs["deduplicate"])
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["reno_order"], "RO-00042")

	# --- overdue scheduler ---

	def test_overdue_flag(self):
		ro = make_reno_order(
			transaction_date=add_days(today(), -10), expected_installation_date=add_days(today(), -2)
		)
		self.assertEqual(ro.is_overdue, 1)  # computed on save

		frappe.db.set_value("Reno Order", ro.name, "is_overdue", 0)
		lifecycle.flag_overdue_installations()
		self.assertEqual(frappe.db.get_value("Reno Order", ro.name, "is_overdue"), 1)

		frappe.db.set_value("Reno Order", ro.name, "status", "Installed")
		lifecycle.flag_overdue_installations()
		self.assertEqual(frappe.db.get_value("Reno Order", ro.name, "is_overdue"), 0)

	def test_future_installation_not_overdue(self):
		ro = make_reno_order()
		lifecycle.flag_overdue_installations()
		self.assertEqual(frappe.db.get_value("Reno Order", ro.name, "is_overdue"), 0)

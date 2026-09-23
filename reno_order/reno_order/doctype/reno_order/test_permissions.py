# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# See license.txt

"""Part 11: which Reno Orders each role can see and change, enforced on the server."""

import frappe
from frappe.desk.form.assign_to import add as assign_to
from frappe.tests import IntegrationTestCase

from reno_order.api.v1.lifecycle import transition
from reno_order.api.v1.permissions import get_permission_query_conditions
from reno_order.tests.utils import (
	RENO_ORDER_LINKED_DOCTYPES,
	make_reno_order,
	make_sales_person,
	make_user,
)

IGNORE_TEST_RECORD_DEPENDENCIES = RENO_ORDER_LINKED_DOCTYPES

SALES_A = "reno.pm.sales.a@example.com"
SALES_B = "reno.pm.sales.b@example.com"
MANAGER = "reno.pm.manager@example.com"
OUTSIDER_MANAGER = "reno.pm.manager2@example.com"
SUPERVISOR = "reno.pm.supervisor@example.com"
PRODUCTION = "reno.pm.production@example.com"
ACCOUNTS = "reno.pm.accounts@example.com"
USERS = (SALES_A, SALES_B, MANAGER, OUTSIDER_MANAGER, SUPERVISOR, PRODUCTION, ACCOUNTS)


class TestRenoOrderPermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(SALES_A, ["Sales User"])
		make_user(SALES_B, ["Sales User"])
		make_user(MANAGER, ["Sales User", "Sales Manager"])
		make_user(OUTSIDER_MANAGER, ["Sales User", "Sales Manager"])
		make_user(SUPERVISOR, ["Site Supervisor"])
		make_user(PRODUCTION, ["Production User"])
		make_user(ACCOUNTS, ["Accounts User"])

		# Team: _Reno Team Lead (the manager) -> _Reno Team Member (sales person on B's order)
		lead = make_sales_person("_Reno Team Lead", user=MANAGER, is_group=1)
		cls.team_member = make_sales_person("_Reno Team Member", parent=lead)
		frappe.local.cache.clear()

		frappe.set_user(SALES_A)
		cls.own_a = make_reno_order(do_not_submit=True).name
		frappe.set_user(SALES_B)
		cls.team_b = make_reno_order(sales_person=cls.team_member).name
		cls.assigned_b = make_reno_order(do_not_submit=True).name
		frappe.set_user("Administrator")
		assign_to({"assign_to": [SALES_A], "doctype": "Reno Order", "name": cls.assigned_b})
		cls.supervised = make_reno_order(site_supervisor=SUPERVISOR).name
		cls.draft_supervised = make_reno_order(site_supervisor=SUPERVISOR, do_not_submit=True).name
		cls.all = [cls.own_a, cls.team_b, cls.assigned_b, cls.supervised, cls.draft_supervised]

	def tearDown(self):
		frappe.set_user("Administrator")

	def visible(self, user):
		frappe.set_user(user)
		return set(frappe.get_list("Reno Order", filters={"name": ("in", self.all)}, pluck="name"))

	def test_sales_user_sees_own_and_assigned(self):
		self.assertEqual(self.visible(SALES_A), {self.own_a, self.assigned_b})

	def test_sales_user_does_not_see_others(self):
		self.assertNotIn(self.own_a, self.visible(SALES_B))
		frappe.set_user(SALES_B)
		self.assertRaises(frappe.PermissionError, frappe.get_doc("Reno Order", self.own_a).check_permission)

	def test_sales_manager_sees_team(self):
		self.assertEqual(self.visible(MANAGER), {self.team_b})
		self.assertEqual(self.visible(OUTSIDER_MANAGER), set())

	def test_supervisor_sees_assigned_installations(self):
		self.assertEqual(self.visible(SUPERVISOR), {self.supervised, self.draft_supervised})

	def test_supervisor_cannot_edit_a_draft(self):
		frappe.set_user(SUPERVISOR)
		doc = frappe.get_doc("Reno Order", self.draft_supervised)
		self.assertTrue(doc.has_permission("read"))
		self.assertFalse(doc.has_permission("write"))

	def test_non_sales_roles_cannot_submit_a_draft(self):
		"""They hold the submit DocPerm (needed to update a submitted order); drafts stay off-limits."""
		for user in (SUPERVISOR, ACCOUNTS):
			frappe.set_user(user)
			doc = frappe.get_doc("Reno Order", self.draft_supervised)
			self.assertFalse(doc.has_permission("submit"), user)
			self.assertRaises(frappe.PermissionError, doc.submit)

	def test_production_sees_orders_in_production_only(self):
		self.assertEqual(self.visible(PRODUCTION), {self.team_b, self.supervised})
		transition(self.supervised, "Ready for Installation")
		self.assertEqual(self.visible(PRODUCTION), {self.team_b, self.supervised})
		frappe.db.set_value("Reno Order", self.supervised, "status", "Installed")
		self.assertEqual(self.visible(PRODUCTION), {self.team_b})

	def test_accounts_user_sees_everything(self):
		self.assertEqual(self.visible(ACCOUNTS), set(self.all))

	def test_list_and_document_checks_agree(self):
		"""The SQL condition (lists, reports) and has_permission (one document) must never differ."""
		for user in USERS:
			listed = self.visible(user)
			for name in self.all:
				allowed = frappe.get_doc("Reno Order", name).has_permission("read")
				self.assertEqual(name in listed, allowed, f"{user} / {name}")

	def test_user_without_role_sees_nothing(self):
		self.assertEqual(get_permission_query_conditions("Guest"), "1=0")

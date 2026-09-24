# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# See license.txt

"""Part 13: fixed-entitlement (event-based) leave is not pro-rated for mid-year joiners."""

import frappe
from frappe.tests import IntegrationTestCase

from reno_order.tests.utils import get_test_company

PERIOD = ("2026-01-01", "2026-12-31")
ANNUAL = "_Reno Annual Leave"
MATERNITY = "_Reno Maternity Leave"
UNFLAGGED = "_Reno Marriage Leave (unflagged)"


def make_leave_type(name, fixed_entitlement=0, **extra):
	if frappe.db.exists("Leave Type", name):
		frappe.delete_doc("Leave Type", name, force=1)
	return frappe.get_doc(
		{
			"doctype": "Leave Type",
			"leave_type_name": name,
			"max_leaves_allowed": 0,
			"fixed_entitlement": fixed_entitlement,
			**extra,
		}
	).insert()


def make_employee(first_name, date_of_joining):
	return frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": first_name,
			"company": get_test_company(),
			"status": "Active",
			"gender": frappe.db.get_value("Gender", {}, "name") or "Female",
			"date_of_birth": "1992-03-04",
			"date_of_joining": date_of_joining,
		}
	).insert()


def allocations(employee) -> dict[str, float]:
	return {
		row.leave_type: row.new_leaves_allocated
		for row in frappe.get_all(
			"Leave Allocation",
			filters={"employee": employee, "docstatus": 1},
			fields=["leave_type", "new_leaves_allocated"],
		)
	}


class TestFixedEntitlementLeave(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		company = get_test_company()
		make_leave_type(ANNUAL)
		make_leave_type(MATERNITY, fixed_entitlement=1)
		make_leave_type(UNFLAGGED)

		policy = frappe.get_doc(
			{
				"doctype": "Leave Policy",
				"title": "_Reno Policy",
				"leave_policy_details": [
					{"leave_type": ANNUAL, "annual_allocation": 18},
					{"leave_type": MATERNITY, "annual_allocation": 90},
					{"leave_type": UNFLAGGED, "annual_allocation": 90},
				],
			}
		).insert()
		policy.submit()
		cls.policy = policy.name

		cls.leave_period = (
			frappe.db.get_value(
				"Leave Period", {"from_date": PERIOD[0], "to_date": PERIOD[1], "company": company}
			)
			or frappe.get_doc(
				{
					"doctype": "Leave Period",
					"from_date": PERIOD[0],
					"to_date": PERIOD[1],
					"company": company,
					"is_active": 1,
				}
			)
			.insert()
			.name
		)

	def assign(self, employee):
		assignment = frappe.get_doc(
			{
				"doctype": "Leave Policy Assignment",
				"employee": employee,
				"leave_policy": self.policy,
				"assignment_based_on": "Leave Period",
				"leave_period": self.leave_period,
			}
		).insert()
		assignment.submit()
		return assignment

	def test_fixed_entitlement_is_full_for_mid_year_joiner(self):
		employee = make_employee("_Reno Mid Year", "2026-07-01")
		self.assign(employee.name)
		self.assertEqual(allocations(employee.name)[MATERNITY], 90)

	def test_root_cause_unflagged_fixed_leave_is_pro_rated(self):
		"""The reported bug, reproduced: without the flag HRMS pro-rates a 90-day entitlement."""
		employee = make_employee("_Reno Root Cause", "2026-07-01")
		self.assign(employee.name)
		self.assertEqual(allocations(employee.name)[UNFLAGGED], 45)  # round(90 * 184 / 365)

	def test_other_leave_is_still_pro_rated(self):
		employee = make_employee("_Reno Mid Year 2", "2026-07-01")
		self.assign(employee.name)
		# 184 of 365 days: round(18 * 184 / 365) = 9, the unchanged HRMS behaviour
		self.assertEqual(allocations(employee.name)[ANNUAL], 9)

	def test_joiner_before_the_period_gets_everything(self):
		employee = make_employee("_Reno Veteran", "2025-03-01")
		self.assign(employee.name)
		self.assertEqual(allocations(employee.name), {ANNUAL: 18, MATERNITY: 90, UNFLAGGED: 90})

	def test_very_late_joiner_is_not_skipped(self):
		# Pro-rated, 90 * 4/365 rounds to 1 and 18 * 4/365 to 0 (HRMS skips a 0 allocation).
		employee = make_employee("_Reno Late", "2026-12-28")
		self.assign(employee.name)
		self.assertEqual(allocations(employee.name).get(MATERNITY), 90)

	def test_no_double_allocation(self):
		employee = make_employee("_Reno Twice", "2026-07-01")
		assignment = self.assign(employee.name)
		self.assertRaises(frappe.ValidationError, assignment.grant_leave_alloc_for_employee)
		self.assertEqual(
			frappe.db.count("Leave Allocation", {"employee": employee.name, "leave_type": MATERNITY}), 1
		)

	def test_flag_cannot_combine_with_earned_leave(self):
		self.assertRaises(
			frappe.ValidationError,
			make_leave_type,
			"_Reno Bad Leave",
			fixed_entitlement=1,
			is_earned_leave=1,
			earned_leave_frequency="Monthly",
		)

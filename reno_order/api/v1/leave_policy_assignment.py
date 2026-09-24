# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Part 13: fixed-entitlement leave types must not be pro-rated for mid-year joiners.

Root cause (HRMS v16.5.4, hr/doctype/leave_policy_assignment/leave_policy_assignment.py):
get_new_leaves() sends every leave type that is neither compensatory nor earned into the
"else" branch, which calls calculate_pro_rated_leaves(annual_allocation, date_of_joining,
effective_from, effective_to). So Maternity 90 for someone joining on 1 July of a calendar
leave period becomes round(90 * 184 / 365) = 45. Leave Type has no setting to switch this
off, and the only configuration workarounds are allocating those leaves outside the policy
(Leave Control Panel / manual Leave Allocation) or a separate policy assigned from the
joining date, which the overlap check forbids next to the annual policy.

Fix, without touching HRMS: a "Fixed Entitlement (No Proration)" check on Leave Type
(custom field, api/v1/setup/install.py) and a class extension (hooks.py
extend_doctype_class) that returns the full policy allocation for those types and defers
to HRMS for everything else. Extending just get_new_leaves also covers the case where the
pro-rated value rounds to 0 and HRMS would skip the allocation altogether.
"""

import frappe
from frappe import _
from frappe.model.meta import get_field_precision
from frappe.utils import cint, flt


def is_fixed_entitlement(leave_type: str) -> bool:
	return bool(cint(frappe.get_cached_value("Leave Type", leave_type, "fixed_entitlement")))


class FixedEntitlementLeavePolicyAssignment:
	"""Mixed in before HRMS's LeavePolicyAssignment (extension classes come first in the MRO)."""

	def get_new_leaves(self, annual_allocation, leave_details, date_of_joining):
		if (
			is_fixed_entitlement(leave_details.name)
			and not leave_details.is_earned_leave
			and not leave_details.is_compensatory
		):
			precision = get_field_precision(
				frappe.get_meta("Leave Allocation").get_field("new_leaves_allocated")
			)
			return flt(annual_allocation, precision)
		return super().get_new_leaves(annual_allocation, leave_details, date_of_joining)


# --- Leave Type doc_event (hooks.py) ---


def validate_leave_type(doc, method=None):
	if cint(doc.get("fixed_entitlement")) and (doc.is_earned_leave or doc.is_compensatory):
		frappe.throw(
			_("Fixed Entitlement cannot be combined with Earned Leave or Compensatory leave."),
			title=_("Invalid Leave Type"),
		)

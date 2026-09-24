# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class RenoOrderSettings(Document):
	def validate(self):
		if not 0 <= flt(self.discount_approval_threshold) <= 100:
			frappe.throw(_("Discount Approval Threshold must be between 0 and 100."))

# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class RenoLogisticsSettings(Document):
	def validate(self):
		if (
			self.enabled
			and not (self.base_url or "").startswith("https://")
			and not frappe.conf.developer_mode
		):
			frappe.throw(_("The logistics API must use https://."))
		for field in ("connect_timeout", "read_timeout", "max_attempts"):
			if (self.get(field) or 0) < 1:
				frappe.throw(_("{0} must be at least 1.").format(self.meta.get_label(field)))

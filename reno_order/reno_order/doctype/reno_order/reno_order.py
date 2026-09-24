# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

import math

import frappe
from erpnext.utilities.transaction_base import validate_uom_is_integer
from frappe import _
from frappe.model import no_value_fields, table_fields
from frappe.model.document import Document
from frappe.utils import flt, get_link_to_form, getdate
from frappe.utils.html_utils import sanitize_html

from reno_order.api.v1 import lifecycle
from reno_order.api.v1.sales_order import get_active_sales_order

# Currency and Float columns are decimal(21,9): anything from 10^12 up fails in the
# database with a 500, so reject it here with a proper validation message.
MAX_VALUE = 1e12

# allow_on_submit fields set only by the server (lifecycle transitions, background jobs).
# A client edit to any of these after submit is silently reverted. (sales_order is not
# allow_on_submit, so Frappe itself rejects a client change to it after submit.)
SERVER_OWNED_AFTER_SUBMIT = (
	"status",
	"installed_on",
	"is_overdue",
	"delivery_note",
	"downstream_status",
	"downstream_attempts",
	"downstream_error",
	"logistics_status",
	"logistics_booking_id",
	"logistics_attempts",
	"logistics_next_retry",
	"logistics_error",
)

# allow_on_submit fields a person may still edit on a submitted order, and who may edit them.
# Everything commercial (customer, rates, discount, totals) is not allow_on_submit at all,
# so Frappe itself rejects changes to it after submit.
EDITABLE_AFTER_SUBMIT = {
	"expected_installation_date": ("Sales Manager", "Production User"),
	"site_supervisor": ("Sales Manager",),
	"installation_remarks": ("Sales Manager", "Site Supervisor"),
}


class RenoOrder(Document):
	# Totals are always recomputed on the server. Whatever the desk form or an
	# API client sends for amount / total_amount / discount_amount / grand_total
	# is overwritten here, because every save path (desk, REST, frappe.client)
	# ends in doc.save() -> validate(). Cancel is the exception: see before_cancel.

	def onload(self):
		self.set_onload("allowed_transitions", lifecycle.get_allowed_transitions(self))

	def before_insert(self):
		self.validate_naming_series()

	def validate(self):
		self.set_server_owned_fields()
		self.validate_dates()
		self.validate_items()
		self.validate_discount_percentage()
		self.calculate_totals()
		self.validate_totals_in_range()
		if self.docstatus.is_draft():
			self.status = "Draft"
		self.is_overdue = lifecycle.is_overdue(self)

	def before_submit(self):
		self.validate_discount_authority()
		self.status = "Confirmed"

	def before_cancel(self):
		self.validate_unchanged_on_cancel()
		self.validate_not_installed()
		self.validate_no_active_sales_order()
		self.status = "Cancelled"
		self.is_overdue = 0

	def before_update_after_submit(self):
		self.validate_dates()
		self.validate_status_not_changed_directly()
		self.protect_server_owned_fields()
		self.validate_editable_after_submit()
		self.is_overdue = lifecycle.is_overdue(self)

	def set_server_owned_fields(self):
		"""read_only only hides a field in the form: the client can still send it, and Frappe
		re-fetches fetch_from fields only when the link itself changes. So set them here."""
		self.customer_name = frappe.db.get_value("Customer", self.customer, "customer_name")
		self.currency = frappe.get_cached_value("Company", self.company, "default_currency")
		for row in self.items:
			if row.item_code:
				row.item_name = frappe.get_cached_value("Item", row.item_code, "item_name")

		# Only the Sales Order doc_events write this link (with db.set_value, which skips validate).
		before = self.get_doc_before_save()
		self.sales_order = before.sales_order if before else None

	def validate_naming_series(self):
		"""naming_series is hidden but still accepted from the client; only allow the configured options."""
		allowed = (self.meta.get_field("naming_series").options or "").split("\n")
		if self.naming_series not in allowed:
			frappe.throw(
				_("Naming Series {0} is not allowed for Reno Order.").format(frappe.bold(self.naming_series)),
				title=_("Invalid Naming Series"),
			)

	def validate_dates(self):
		transaction_date = self.get_date("transaction_date")
		installation_date = self.get_date("expected_installation_date")
		if transaction_date and installation_date and installation_date < transaction_date:
			frappe.throw(
				_("Expected Installation Date cannot be before the Transaction Date."),
				title=_("Invalid Installation Date"),
			)

	def get_date(self, fieldname):
		"""getdate() returns None for some junk (e.g. 20260101), which would crash the comparison."""
		value = self.get(fieldname)
		if not value:
			return None
		date = getdate(value)
		if not date:
			frappe.throw(
				_("{0} is not a valid date.").format(frappe.bold(self.meta.get_label(fieldname))),
				title=_("Invalid Date"),
			)
		# Store the parsed date: a string like "31-12-2026" is understood here but rejected by MariaDB.
		self.set(fieldname, date)
		return date

	def validate_items(self):
		if not self.items:
			frappe.throw(_("Add at least one item to the Reno Order."), title=_("No Items"))

		for row in self.items:
			if row.item_code and frappe.get_cached_value("Item", row.item_code, "disabled"):
				frappe.throw(
					_("Row #{0}: Item {1} is disabled.").format(row.idx, frappe.bold(row.item_code)),
					title=_("Disabled Item"),
				)
			if row.item_code and not row.uom:
				# API clients may omit UOM; default to the item's stock UOM like the form does.
				row.uom = frappe.get_cached_value("Item", row.item_code, "stock_uom")
			if row.description:
				# Frappe's XSS filter is skipped when a document is inserted already submitted
				# (e.g. REST POST with docstatus=1), so sanitize the one free-text field here.
				row.description = sanitize_html(row.description)

			# Check before rounding: NaN fails every comparison, so `qty <= 0` would let it through.
			if not all(math.isfinite(flt(row.get(f))) for f in ("qty", "rate")):
				frappe.throw(
					_("Row #{0}: Quantity and Rate must be numbers.").format(row.idx),
					title=_("Invalid Number"),
				)
			# Round to the stored precision so the Sales Order gets exactly the same quantity.
			row.qty = flt(row.qty, row.precision("qty"))

			if row.qty <= 0:
				frappe.throw(
					_("Row #{0}: Quantity for {1} must be greater than zero.").format(
						row.idx, frappe.bold(row.item_code)
					),
					title=_("Invalid Quantity"),
				)
			if flt(row.rate) < 0:
				frappe.throw(
					_("Row #{0}: Rate for {1} cannot be negative.").format(
						row.idx, frappe.bold(row.item_code)
					),
					title=_("Invalid Rate"),
				)
			if row.qty >= MAX_VALUE or flt(row.rate) >= MAX_VALUE:
				frappe.throw(
					_("Row #{0}: Quantity or Rate is too large.").format(row.idx), title=_("Value Too Large")
				)

		# Nos and other whole-number UOMs: the Sales Order would reject a fraction, so reject it here.
		validate_uom_is_integer(self, "uom", "qty")

	def validate_discount_percentage(self):
		if not 0 <= flt(self.discount_percentage) <= 100:
			frappe.throw(_("Discount % must be between 0 and 100."), title=_("Invalid Discount"))

	def calculate_totals(self):
		total = 0.0
		for row in self.items:
			row.amount = flt(flt(row.qty) * flt(row.rate), row.precision("amount"))
			total += row.amount

		self.total_amount = flt(total, self.precision("total_amount"))
		self.discount_amount = flt(
			self.total_amount * flt(self.discount_percentage) / 100, self.precision("discount_amount")
		)
		self.grand_total = flt(self.total_amount - self.discount_amount, self.precision("grand_total"))

	def validate_totals_in_range(self):
		# Each row can be in range while qty * rate or the sum is not.
		if any(flt(row.amount) >= MAX_VALUE for row in self.items) or self.total_amount >= MAX_VALUE:
			frappe.throw(_("The order total is too large."), title=_("Value Too Large"))

	def validate_unchanged_on_cancel(self):
		"""Reject a cancel that carries edits.

		savedocs(doc, "Cancel") saves the document JSON the client sent, and Frappe skips
		both validate() and its own _validate() on cancel (document.py _save), so any
		changed value - totals, dates, even deleted item rows - would be stored as is.
		"""
		before = self.get_doc_before_save()
		if not before:
			return

		changed = _changed_fields(self, before, ignore={"status"})
		before_rows = {row.name: row for row in before.get_all_children()}
		for row in self.get_all_children():
			if row.name in before_rows:
				changed += _changed_fields(row, before_rows.pop(row.name))
			else:  # row added
				changed.append(self.meta.get_label(row.parentfield))
		# rows left over were removed by the client
		changed += [self.meta.get_label(row.parentfield) for row in before_rows.values()]

		if changed:
			frappe.throw(
				_("A Reno Order cannot be edited while cancelling it. Changed: {0}").format(
					", ".join(sorted(set(changed)))
				),
				title=_("Not Allowed"),
			)

	def validate_not_installed(self):
		if self.get_doc_before_save() and self.get_doc_before_save().status in ("Installed", "Closed"):
			frappe.throw(
				_("An installed order cannot be cancelled. Return the delivered goods instead."),
				title=_("Not Allowed"),
			)

	def protect_server_owned_fields(self):
		before = self.get_doc_before_save()
		if not before or self.flags.lifecycle_update:
			return
		for fieldname in SERVER_OWNED_AFTER_SUBMIT:
			# status gets an explicit error instead (validate_status_not_changed_directly)
			if fieldname != "status":
				self.set(fieldname, before.get(fieldname))

	def validate_editable_after_submit(self):
		"""Per-role field rules on a submitted order (Part 11: e.g. a Site Supervisor may add
		remarks but not reassign the order or move its date)."""
		before = self.get_doc_before_save()
		if not before:
			return
		roles = set(frappe.get_roles())
		if roles & {"System Manager"} or frappe.session.user == "Administrator":
			return
		not_allowed = [
			self.meta.get_label(fieldname)
			for fieldname, allowed_roles in EDITABLE_AFTER_SUBMIT.items()
			if self.get_value(fieldname) != before.get_value(fieldname) and not roles & set(allowed_roles)
		]
		if not_allowed:
			frappe.throw(
				_("You are not allowed to change {0} on a submitted order.").format(", ".join(not_allowed)),
				exc=frappe.PermissionError,
				title=_("Not Permitted"),
			)

	def validate_no_active_sales_order(self):
		"""Frappe blocks cancel only for a submitted Sales Order; a draft one would be orphaned
		(it could never be saved again, since it links to a cancelled Reno Order)."""
		if sales_order := get_active_sales_order(self.name):
			frappe.throw(
				_("Cancel or delete Sales Order {0} before cancelling this Reno Order.").format(
					get_link_to_form("Sales Order", sales_order)
				),
				frappe.LinkExistsError,
				title=_("Sales Order Exists"),
			)

	def validate_discount_authority(self):
		"""Block submission above the configured threshold unless the user holds the approver role.

		Runs in before_submit, not validate: a draft waiting for approval is a valid draft.
		"""
		settings = frappe.get_cached_doc("Reno Order Settings")
		threshold = flt(settings.discount_approval_threshold)
		if flt(self.discount_percentage) <= threshold:
			return

		if settings.discount_approver_role in frappe.get_roles():
			return

		frappe.throw(
			_(
				"Discount of {0}% exceeds the approval threshold of {1}%. Only a {2} can submit this order."
			).format(flt(self.discount_percentage), threshold, frappe.bold(settings.discount_approver_role)),
			exc=frappe.PermissionError,
			title=_("Discount Approval Required"),
		)

	def validate_status_not_changed_directly(self):
		"""Status is allow_on_submit so the server can move it, but clients must not set it by hand.

		Status transitions go through the lifecycle service (Part 2), which sets
		flags.status_change_allowed after checking the transition and role.
		"""
		before = self.get_doc_before_save()
		if before and before.status != self.status and not self.flags.status_change_allowed:
			frappe.throw(
				_("Status cannot be changed directly. Use the order actions instead."),
				exc=frappe.PermissionError,
				title=_("Not Allowed"),
			)


def _changed_fields(doc, before, ignore=()) -> list[str]:
	"""Labels of data fields whose value differs from the saved copy (both cast to the field type)."""
	changed = []
	for df in doc.meta.fields:
		if df.fieldtype in no_value_fields or df.fieldtype in table_fields or df.fieldname in ignore:
			continue
		if not doc.get(df.fieldname) and not before.get(df.fieldname):
			# Both empty. Don't cast: get_value() turns an empty Datetime into "now",
			# which would never compare equal.
			continue
		new, old = doc.get_value(df.fieldname), before.get_value(df.fieldname)
		if new != old:
			changed.append(df.label or df.fieldname)
	return changed


def on_doctype_update():
	"""Covering index for the Monthly Value report (Part 10): range on transaction_date, and
	docstatus / status / grand_total read from the index itself. See docs/PERFORMANCE.md."""
	frappe.db.add_index(
		"Reno Order", ["transaction_date", "docstatus", "status", "grand_total"], "monthly_value_idx"
	)

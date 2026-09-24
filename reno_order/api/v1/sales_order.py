# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Reno Order -> Sales Order.

Duplicate prevention is layered:
1. make_sales_order locks the Reno Order row (SELECT ... FOR UPDATE), so two
   concurrent clicks are serialised and the second one sees the first Sales Order.
2. validate_reno_order_link (a Sales Order doc_event, see hooks.py) runs on every
   Sales Order save, so a duplicate made through any other path (manual entry,
   Duplicate menu, data import) is rejected.

Reno Order.sales_order is kept in sync by the Sales Order doc_events alone
(link on on_update, unlink on cancel/delete), so it is right however the Sales
Order was made: make_sales_order, an amendment, or by hand.

Only make_sales_order is whitelisted; the doc_event handlers are plain functions
and cannot be called over HTTP.
"""

import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc
from frappe.utils import get_link_to_form


@frappe.whitelist(methods=["POST"])
def make_sales_order(reno_order: str) -> str:
	"""Create a draft Sales Order from a submitted Reno Order and return its name.

	POST /api/method/reno_order.api.v1.sales_order.make_sales_order
	{"reno_order": "RO-00001"}
	"""
	ro = frappe.get_doc("Reno Order", reno_order, for_update=True)
	ro.check_permission("read")

	if not ro.docstatus.is_submitted():
		frappe.throw(_("Submit the Reno Order before creating a Sales Order."))

	if existing := get_active_sales_order(ro.name):
		frappe.throw(
			_("Sales Order {0} already exists for this Reno Order.").format(
				get_link_to_form("Sales Order", existing)
			),
			exc=frappe.DuplicateEntryError,
			title=_("Sales Order Exists"),
		)

	so = _map_sales_order(ro.name)
	so.insert()  # on_update -> link_reno_order sets Reno Order.sales_order
	return so.name


def _map_sales_order(reno_order: str):
	def set_missing_values(source, target):
		target.delivery_date = source.expected_installation_date
		# Keep the Reno Order's commercial terms: no pricing rules re-pricing the items.
		target.ignore_pricing_rule = 1
		if source.discount_percentage:
			target.apply_discount_on = "Net Total"
			target.additional_discount_percentage = source.discount_percentage
		if source.sales_person:
			target.append("sales_team", {"sales_person": source.sales_person, "allocated_percentage": 100})
		target.run_method("set_missing_values")
		target.run_method("calculate_taxes_and_totals")

	def update_item(source, target, source_parent):
		target.delivery_date = source_parent.expected_installation_date
		# ERPNext only fills item fields that are None. With price_list_rate unset it would
		# fetch the price list rate and use it for a zero-rate (free) line, so set it to the
		# Reno Order rate: the Reno Order is the price source.
		target.price_list_rate = source.rate

	return get_mapped_doc(
		"Reno Order",
		reno_order,
		{
			"Reno Order": {
				"doctype": "Sales Order",
				"validation": {"docstatus": ["=", 1]},
				"field_map": {"name": "reno_order"},
				"field_no_map": [
					"naming_series",
					"status",
					"discount_amount",
					"total_amount",
					"grand_total",
					"discount_percentage",
					"order_type",
				],
			},
			"Reno Order Item": {
				"doctype": "Sales Order Item",
				"field_map": {"name": "reno_order_item", "warehouse": "warehouse"},
				"field_no_map": ["amount"],
				"postprocess": update_item,
			},
		},
		postprocess=set_missing_values,
	)


def get_active_sales_order(reno_order: str, exclude: str | None = None) -> str | None:
	filters = {"reno_order": reno_order, "docstatus": ("<", 2)}
	if exclude:
		filters["name"] = ("!=", exclude)
	return frappe.db.get_value("Sales Order", filters, "name")


# --- Sales Order doc_events (registered in hooks.py) ---


def validate_reno_order_link(doc, method=None):
	if not doc.get("reno_order"):
		return

	ro_docstatus = frappe.db.get_value("Reno Order", doc.reno_order, "docstatus")
	if ro_docstatus != 1:
		frappe.throw(
			_("Reno Order {0} must be submitted and not cancelled.").format(
				get_link_to_form("Reno Order", doc.reno_order)
			)
		)

	if existing := get_active_sales_order(doc.reno_order, exclude=doc.name):
		frappe.throw(
			_("Reno Order {0} is already linked to Sales Order {1}.").format(
				get_link_to_form("Reno Order", doc.reno_order), get_link_to_form("Sales Order", existing)
			),
			exc=frappe.DuplicateEntryError,
			title=_("Duplicate Sales Order"),
		)


def link_reno_order(doc, method=None):
	"""On every Sales Order save (and submit, which also runs on_update), point the Reno Order at it."""
	if doc.get("reno_order") and frappe.db.get_value("Reno Order", doc.reno_order, "sales_order") != doc.name:
		# update_modified: an open Reno Order form then gets "modified after you opened it"
		# instead of a confusing "cannot change Sales Order after submission" on its next save.
		frappe.db.set_value("Reno Order", doc.reno_order, "sales_order", doc.name)


def unlink_reno_order(doc, method=None):
	"""On Sales Order cancel/delete, free the Reno Order so a replacement can be created."""
	if doc.get("reno_order") and frappe.db.get_value("Reno Order", doc.reno_order, "sales_order") == doc.name:
		frappe.db.set_value("Reno Order", doc.reno_order, "sales_order", None)

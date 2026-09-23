# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Part 4: Reno Order -> Work Orders for internally manufactured components.

Standard ERPNext manufacturing does the rest, uncustomised:
	BOM (materials + operations on workstations) -> Work Order -> Job Cards (one per
	operation, on Work Order submit) -> Stock Entry "Material Transfer for Manufacture"
	(raw materials to WIP) -> Stock Entry "Manufacture" (consumes WIP, receives the
	finished cabinet into the finished-goods warehouse, valued from the materials and
	operating cost).

The connection to the order is the `reno_order` field on Work Order (plus `project` and
`sales_order`, so ERPNext's own reports group it with the rest of the job).
"""

import frappe
from erpnext.stock.get_item_details import get_conversion_factor
from frappe import _
from frappe.utils import flt, nowdate


@frappe.whitelist(methods=["POST"])
def make_work_orders(reno_order: str) -> list[str]:
	"""Draft Work Orders for every order line whose item has a default BOM.

	POST {"reno_order": "RO-00001"} -> ["MFG-WO-2026-00001", ...]
	Idempotent: lines that already have an active Work Order for this order are skipped.
	"""
	ro = frappe.get_doc("Reno Order", reno_order, for_update=True)  # serialise double clicks
	ro.check_permission("read")
	frappe.has_permission("Work Order", "create", throw=True)
	if not ro.docstatus.is_submitted() or ro.status not in ("Confirmed", "In Production"):
		frappe.throw(_("Work Orders can be made for Confirmed or In Production orders only."))

	created = []
	for row in ro.items:
		bom = get_default_bom(row.item_code)
		if not bom or get_active_work_order(ro.name, row.item_code):
			continue

		wo = frappe.new_doc("Work Order")
		wo.update(
			{
				"production_item": row.item_code,
				"bom_no": bom,
				"qty": flt(row.qty)
				* flt(get_conversion_factor(row.item_code, row.uom).get("conversion_factor") or 1),
				"company": ro.company,
				# Blank falls back to the Company's default WIP / finished-goods warehouses.
				"fg_warehouse": row.warehouse,
				"planned_start_date": nowdate(),
				"expected_delivery_date": ro.expected_installation_date,
				"project": ro.project,
				"reno_order": ro.name,
			}
		)
		if ro.sales_order and frappe.db.get_value("Sales Order", ro.sales_order, "docstatus") == 1:
			wo.sales_order = ro.sales_order
		# What the form does when a BOM is picked: materials and operations (-> Job Cards).
		wo.get_items_and_operations_from_bom()
		wo.insert()
		created.append(wo.name)

	if not created:
		frappe.msgprint(_("Nothing to manufacture: no line needs a new Work Order."), alert=True)
	return created


def get_default_bom(item_code: str) -> str | None:
	return frappe.db.get_value(
		"BOM", {"item": item_code, "is_active": 1, "is_default": 1, "docstatus": 1}, "name"
	)


def get_active_work_order(reno_order: str, item_code: str) -> str | None:
	return frappe.db.get_value(
		"Work Order",
		{"reno_order": reno_order, "production_item": item_code, "docstatus": ("<", 2)},
		"name",
	)

# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Part 5: Reno Order -> Material Request for bought-in items that are out of stock.

From there the standard ERPNext procurement chain runs, uncustomised, and the
`reno_order` field rides along on every document:
	Material Request -> Request for Quotation -> Supplier Quotation -> Purchase Order
	-> Purchase Receipt (stock in: Stock Ledger + GL "Stock In Hand" Dr /
	"Stock Received But Not Billed" Cr) -> Purchase Invoice ("Stock Received But Not
	Billed" Dr / Creditors Cr; no second stock movement).
"""

import frappe
from erpnext.stock.get_item_details import get_conversion_factor
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate

from reno_order.api.v1.manufacturing import get_default_bom


@frappe.whitelist(methods=["POST"])
def make_material_request(reno_order: str) -> str | None:
	"""Draft a Purchase Material Request for the shortfall of each bought-in stock item.

	POST {"reno_order": "RO-00001"} -> "MAT-MR-2026-00001" (or null: nothing is short)
	Idempotent: quantities already requested for this order are deducted.
	"""
	ro = frappe.get_doc("Reno Order", reno_order, for_update=True)
	ro.check_permission("read")
	frappe.has_permission("Material Request", "create", throw=True)
	if not ro.docstatus.is_submitted() or ro.status in ("Installed", "Closed", "Cancelled"):
		frappe.throw(_("Material can be requested for open, submitted orders only."))

	schedule_date = max(getdate(nowdate()), getdate(add_days(ro.expected_installation_date, -7)))
	rows = []
	for row in ro.items:
		shortage = get_shortage(ro, row)
		if shortage > 0:
			rows.append(
				{
					"item_code": row.item_code,
					"qty": shortage,
					"uom": frappe.get_cached_value("Item", row.item_code, "stock_uom"),
					"warehouse": row.warehouse or default_warehouse(ro.company),
					"schedule_date": schedule_date,
					"project": ro.project,
				}
			)
	if not rows:
		frappe.msgprint(
			_("Nothing to request: every bought-in item is in stock or already requested."), alert=True
		)
		return None

	mr = frappe.get_doc(
		{
			"doctype": "Material Request",
			"material_request_type": "Purchase",
			"company": ro.company,
			"transaction_date": nowdate(),
			"schedule_date": schedule_date,
			"reno_order": ro.name,
			"items": rows,
		}
	).insert()
	return mr.name


def get_shortage(ro, row) -> float:
	"""Stock-UOM quantity still to buy for one order line (0 when nothing is needed)."""
	item = frappe.get_cached_value("Item", row.item_code, ["is_stock_item", "is_purchase_item"], as_dict=True)
	if not item or not item.is_stock_item or not item.is_purchase_item or get_default_bom(row.item_code):
		return 0  # services, and components we make ourselves (Part 4)

	required = flt(row.qty) * flt(get_conversion_factor(row.item_code, row.uom).get("conversion_factor") or 1)
	warehouse = row.warehouse or default_warehouse(ro.company)
	projected = flt(
		frappe.db.get_value("Bin", {"item_code": row.item_code, "warehouse": warehouse}, "projected_qty")
	)

	# projected_qty = actual + ordered + requested + planned - reserved. Once the Sales Order
	# is submitted this order's own need is already in `reserved`, so the shortage is simply
	# how far projected is below zero; before that, the need has to be added explicitly.
	so_submitted = ro.sales_order and frappe.db.get_value("Sales Order", ro.sales_order, "docstatus") == 1
	shortage = -projected if so_submitted else required - projected
	# Requests already raised for this order are part of projected_qty (as `requested`) only
	# after submit; deduct open drafts as well so a second click does not double the request.
	shortage -= requested_in_drafts(ro.name, row.item_code)
	return max(0.0, min(shortage, required))


def requested_in_drafts(reno_order: str, item_code: str) -> float:
	return flt(
		frappe.db.sql(
			"""
			select sum(mri.stock_qty) from `tabMaterial Request Item` mri
			join `tabMaterial Request` mr on mr.name = mri.parent
			where mr.reno_order = %s and mr.docstatus = 0 and mri.item_code = %s
			""",
			(reno_order, item_code),
		)[0][0]
	)


def default_warehouse(company: str) -> str | None:
	return frappe.db.get_single_value("Stock Settings", "default_warehouse") or frappe.db.get_value(
		"Warehouse", {"company": company, "is_group": 0}, "name"
	)

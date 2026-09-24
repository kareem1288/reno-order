# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Masters for the manufacturing (Part 4) and buying (Part 5) scenarios. Idempotent.

	bench --site <site> execute reno_order.api.v1.setup.sample_data.setup

Kitchen Base Cabinet 600mm, made in-house:
	materials   Plywood 18mm x2, Laminate Sheet x1, PVA Adhesive x0.5 L, Soft-close Hinge x4
	operations  Cutting (Cutting Station) -> Assembly (Assembly Bench) -> Finishing (Finishing Booth)
Soft-close hinges are bought from a hardware supplier.
"""

import frappe

CABINET = "KC-BASE-600"
HINGE = "HW-HINGE-SC"
SUPPLIER = "Hettich Hardware Supplies"

RAW_MATERIALS = [
	# item_code, item_name, uom, qty per cabinet, valuation rate
	("RM-PLY-18", "Plywood 18mm (8x4 ft)", "Nos", 2, 2800),
	("RM-LAM-SHEET", "Laminate Sheet", "Nos", 1, 1200),
	("RM-ADH-PVA", "PVA Adhesive", "Litre", 0.5, 400),
	(HINGE, "Soft-close Cabinet Hinge", "Nos", 4, 180),
]
OPERATIONS = [
	# operation, workstation, minutes, hourly rate
	("Cutting", "Cutting Station", 45, 600),
	("Assembly", "Assembly Bench", 60, 500),
	("Finishing", "Finishing Booth", 30, 700),
]


def setup(company: str | None = None) -> str:
	"""Create everything and return the cabinet's default BOM."""
	company = company or frappe.defaults.get_global_default("company")
	set_manufacturing_warehouses(company)

	make_item(CABINET, "Kitchen Base Cabinet 600mm", "Nos", is_purchase_item=0)
	for code, name, uom, _qty, rate in RAW_MATERIALS:
		make_item(code, name, uom, valuation_rate=rate)
	for operation, workstation, _minutes, hour_rate in OPERATIONS:
		make_workstation(workstation, hour_rate)
		make_operation(operation, workstation)
	make_supplier(SUPPLIER)
	return make_bom(company)


def set_manufacturing_warehouses(company: str):
	"""v16 keeps the default WIP / finished-goods warehouses on the Company."""
	abbr = frappe.get_cached_value("Company", company, "abbr")
	for field, warehouse in (
		("default_wip_warehouse", f"Work In Progress - {abbr}"),
		("default_fg_warehouse", f"Finished Goods - {abbr}"),
	):
		if not frappe.get_cached_value("Company", company, field) and frappe.db.exists(
			"Warehouse", warehouse
		):
			frappe.db.set_value("Company", company, field, warehouse)


def make_item(code, name, uom, is_purchase_item=1, valuation_rate=0):
	if not frappe.db.exists("UOM", uom):
		frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert(ignore_permissions=True)
	if frappe.db.exists("Item", code):
		return code
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": name,
			"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
			"stock_uom": uom,
			"is_stock_item": 1,
			"is_purchase_item": is_purchase_item,
			"is_sales_item": 1,
			"valuation_rate": valuation_rate,
		}
	).insert(ignore_permissions=True)
	return code


def make_workstation(name, hour_rate):
	if not frappe.db.exists("Workstation", name):
		frappe.get_doc(
			{
				"doctype": "Workstation",
				"workstation_name": name,
				"production_capacity": 1,
				"hour_rate_labour": hour_rate,
			}
		).insert(ignore_permissions=True)


def make_operation(name, workstation):
	if not frappe.db.exists("Operation", name):
		frappe.get_doc({"doctype": "Operation", "name": name, "workstation": workstation}).insert(
			ignore_permissions=True
		)


def make_supplier(name):
	if not frappe.db.exists("Supplier", name):
		frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": name,
				"supplier_type": "Company",
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name"),
			}
		).insert(ignore_permissions=True)


def make_bom(company) -> str:
	if bom := frappe.db.get_value("BOM", {"item": CABINET, "is_default": 1, "docstatus": 1}, "name"):
		return bom
	bom = frappe.get_doc(
		{
			"doctype": "BOM",
			"item": CABINET,
			"company": company,
			"quantity": 1,
			"is_active": 1,
			"is_default": 1,
			"with_operations": 1,
			"rm_cost_as_per": "Valuation Rate",
			"items": [
				{"item_code": code, "qty": qty, "uom": uom, "rate": rate}
				for code, _name, uom, qty, rate in RAW_MATERIALS
			],
			"operations": [
				{"operation": op, "workstation": ws, "time_in_mins": minutes, "hour_rate": rate}
				for op, ws, minutes, rate in OPERATIONS
			],
		}
	).insert(ignore_permissions=True)
	bom.submit()
	return bom.name

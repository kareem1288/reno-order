# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Part 10: Monthly Reno Order value grouped by status, last 12 months.

The query is shaped for the covering index `monthly_value_idx`
(transaction_date, docstatus, status, grand_total), added in RenoOrder's on_doctype_update:
a range scan on transaction_date that never reads the table rows. See
docs/PERFORMANCE.md for the EXPLAIN before/after on 100k rows.

Row-level permissions (Part 11) still apply: build_match_conditions adds the same
permission_query_conditions the list view uses, so a manager sees only their team's value.
"""

import frappe
from frappe import _
from frappe.desk.reportview import build_match_conditions
from frappe.utils import add_months, get_first_day, get_last_day, getdate

MONTHS = 12

QUERY = """
	select
		date_format(transaction_date, '%%Y-%%m') as month,
		status,
		count(*) as orders,
		sum(grand_total) as value
	from `tabReno Order`
	where transaction_date between %(from_date)s and %(to_date)s
		and docstatus < 2
		{match_conditions}
	group by month, status
	order by month, status
"""


def execute(filters=None):
	filters = frappe._dict(filters or {})
	to_date = get_last_day(getdate(filters.get("to_date")) if filters.get("to_date") else getdate())
	from_date = get_first_day(add_months(to_date, -(MONTHS - 1)))

	rows = get_rows(from_date, to_date)
	return get_columns(), rows, None, get_chart(rows)


def get_rows(from_date, to_date, user=None):
	match = build_match_conditions("Reno Order", user=user)
	return frappe.db.sql(
		QUERY.format(match_conditions=f"and ({match})" if match else ""),
		{"from_date": from_date, "to_date": to_date},
		as_dict=True,
	)


def get_columns():
	return [
		{"fieldname": "month", "label": _("Month"), "fieldtype": "Data", "width": 110},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 170},
		{"fieldname": "orders", "label": _("Orders"), "fieldtype": "Int", "width": 90},
		{"fieldname": "value", "label": _("Value"), "fieldtype": "Currency", "width": 160},
	]


def get_chart(rows):
	months = sorted({r.month for r in rows})
	statuses = sorted({r.status for r in rows})
	value = {(r.month, r.status): r.value for r in rows}
	return {
		"data": {
			"labels": months,
			"datasets": [
				{"name": status, "values": [value.get((m, status), 0) for m in months]} for status in statuses
			],
		},
		"type": "bar",
		"barOptions": {"stacked": True},
	}

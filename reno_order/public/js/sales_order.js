// Copyright (c) 2026, Shaik Khaja Kareem and contributors
// For license information, please see license.txt

// Reno Order.sales_order links back to this Sales Order, so the desk Cancel dialog
// would list the Reno Order under "Cancel All" and fail (the Sales Order still links
// to it). Leave it out: the Sales Order's on_cancel doc_event clears that link.
// Runs in refresh because ERPNext's sales_order.js resets the list in onload.

frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		const ignored = frm.ignore_doctypes_on_cancel_all || [];
		if (!ignored.includes("Reno Order")) {
			frm.ignore_doctypes_on_cancel_all = [...ignored, "Reno Order"];
		}
	},
});

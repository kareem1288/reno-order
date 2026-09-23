// Copyright (c) 2026, Shaik Khaja Kareem and contributors
// For license information, please see license.txt

// Totals here only give instant feedback while typing.
// The server recomputes them in RenoOrder.validate(); these values are never trusted.
// Likewise the status buttons are only shown when the server says the move is allowed
// (__onload.allowed_transitions); set_status checks everything again.

const STATUS_ACTIONS = {
	"In Production": { label: __("Start Production") },
	"Ready for Installation": { label: __("Ready for Installation") },
	Installed: { label: __("Mark as Installed"), primary: true, confirm: true },
	Closed: { label: __("Close Order") },
};

frappe.ui.form.on("Reno Order", {
	setup(frm) {
		frm.set_query("site_supervisor", () => ({
			query: "reno_order.api.v1.lifecycle.site_supervisor_query",
		}));
		frm.set_query("warehouse", "items", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("customer_address", () => ({
			query: "frappe.contacts.doctype.address.address.address_query",
			filters: { link_doctype: "Customer", link_name: frm.doc.customer },
		}));
		frm.set_query("contact_person", () => ({
			query: "frappe.contacts.doctype.contact.contact.contact_query",
			filters: { link_doctype: "Customer", link_name: frm.doc.customer },
		}));
	},

	customer(frm) {
		// Fetch the customer's primary address and contact, like a Sales Order does.
		frm.set_value({ customer_address: null, contact_person: null });
		if (!frm.doc.customer) return;
		frappe.db.get_value("Customer", frm.doc.customer, ["customer_primary_address", "customer_primary_contact"])
			.then(({ message }) => {
				if (message?.customer_primary_address) frm.set_value("customer_address", message.customer_primary_address);
				if (message?.customer_primary_contact) frm.set_value("contact_person", message.customer_primary_contact);
			});
	},

	expected_installation_date(frm) {
		// Friendly early warning; the server rejects it on save anyway.
		const { transaction_date, expected_installation_date } = frm.doc;
		if (transaction_date && expected_installation_date && expected_installation_date < transaction_date) {
			frappe.show_alert({ message: __("Installation date is before the order date."), indicator: "orange" });
		}
	},

	refresh(frm) {
		add_status_actions(frm);
		show_downstream_state(frm);
		add_create_actions(frm);

		if (frm.doc.docstatus === 1 && !frm.doc.sales_order && frm.doc.status !== "Cancelled") {
			frm.add_custom_button(
				__("Sales Order"),
				() => {
					frappe.call({
						method: "reno_order.api.v1.sales_order.make_sales_order",
						args: { reno_order: frm.doc.name },
						freeze: true,
						freeze_message: __("Creating Sales Order..."),
						callback(r) {
							if (r.message) {
								// The server linked the Sales Order; drop the cached copy so coming back shows it.
								frappe.model.clear_doc(frm.doctype, frm.docname);
								frappe.set_route("Form", "Sales Order", r.message);
							}
						},
					});
				},
				__("Create")
			);
		}
	},

	discount_percentage(frm) {
		calculate_totals(frm);
	},
});

frappe.ui.form.on("Reno Order Item", {
	item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.item_code) return;
		frappe.db.get_value("Item", row.item_code, ["item_name", "stock_uom", "description"]).then(({ message }) => {
			if (!message) return;
			frappe.model.set_value(cdt, cdn, "item_name", message.item_name);
			if (!row.uom) frappe.model.set_value(cdt, cdn, "uom", message.stock_uom);
			if (!row.description) frappe.model.set_value(cdt, cdn, "description", message.description);
		});
	},
	qty(frm, cdt, cdn) {
		calculate_row(frm, cdt, cdn);
	},
	rate(frm, cdt, cdn) {
		calculate_row(frm, cdt, cdn);
	},
	items_remove(frm) {
		calculate_totals(frm);
	},
});

function add_create_actions(frm) {
	if (frm.doc.docstatus !== 1 || !["Confirmed", "In Production"].includes(frm.doc.status)) return;
	const make = (method, label, doctype) =>
		frm.add_custom_button(
			label,
			() =>
				frappe.call({
					method,
					args: { reno_order: frm.doc.name },
					freeze: true,
					callback({ message }) {
						const names = [].concat(message || []);
						if (names.length === 1) frappe.set_route("Form", doctype, names[0]);
						else if (names.length) frappe.set_route("List", doctype, { reno_order: frm.doc.name });
					},
				}),
			__("Create")
		);
	make("reno_order.api.v1.manufacturing.make_work_orders", __("Work Orders"), "Work Order");
	make("reno_order.api.v1.buying.make_material_request", __("Material Request"), "Material Request");
}

function add_status_actions(frm) {
	const allowed = frm.doc.__onload?.allowed_transitions || [];
	for (const status of allowed) {
		const action = STATUS_ACTIONS[status] || { label: status };
		const run = () =>
			frappe.call({
				method: "reno_order.api.v1.lifecycle.set_status",
				args: { reno_order: frm.doc.name, status },
				freeze: true,
				freeze_message: __("Updating..."),
				callback: () => frm.reload_doc(),
			});
		const onclick = action.confirm
			? () => frappe.confirm(__("Mark {0} as {1}?", [frm.doc.name, __(status)]), run)
			: run;
		if (action.primary) {
			frm.page.set_primary_action(action.label, onclick);
		} else {
			frm.add_custom_button(action.label, onclick, __("Status"));
		}
	}
}

function show_downstream_state(frm) {
	if (frm.doc.is_overdue) {
		frm.dashboard.set_headline(__("Installation is overdue (expected {0}).", [frappe.datetime.str_to_user(frm.doc.expected_installation_date)]), "red");
	}
	if (frm.doc.downstream_status === "Queued") {
		frm.dashboard.set_headline(__("Delivery Note is being prepared in the background."), "blue");
	}
	if (frm.doc.downstream_status === "Failed") {
		frm.dashboard.set_headline(__("Delivery Note failed: {0}", [frm.doc.downstream_error || ""]), "red");
		if (frappe.user.has_role("Sales Manager")) {
			frm.add_custom_button(__("Retry Delivery Note"), () =>
				frappe.call({
					method: "reno_order.api.v1.delivery_note.retry_delivery_note",
					args: { reno_order: frm.doc.name },
					callback: () => frm.reload_doc(),
				})
			);
		}
	}
}

function calculate_row(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, "amount", flt(row.qty) * flt(row.rate));
	calculate_totals(frm);
}

function calculate_totals(frm) {
	const total = (frm.doc.items || []).reduce((sum, row) => sum + flt(row.amount), 0);
	const discount = (total * flt(frm.doc.discount_percentage)) / 100;
	frm.set_value("total_amount", total);
	frm.set_value("discount_amount", discount);
	frm.set_value("grand_total", total - discount);
}

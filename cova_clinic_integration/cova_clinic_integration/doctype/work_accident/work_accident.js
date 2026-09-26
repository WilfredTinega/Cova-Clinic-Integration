// Copyright (c) 2026, Upande LTD and contributors
// For license information, please see license.txt

frappe.ui.form.on("Work Accident", {
	setup(frm) {
		frm.set_query("employee", () => ({ filters: { status: "Active" } }));
		frm.set_query("first_aider", () => ({ filters: { status: "Active" } }));
	},

	refresh(frm) {
		if (!frm.is_new() && !frm.doc.clinic_ticket) {
			frm.add_custom_button(__("Send to Clinic"), () =>
				frm.call("send_to_clinic_from_form").then(() => frm.reload_doc())
			);
		}
	},
});

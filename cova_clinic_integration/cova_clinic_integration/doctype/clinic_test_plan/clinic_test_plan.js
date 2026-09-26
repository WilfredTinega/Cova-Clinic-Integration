// Copyright (c) 2026, Upande LTD and contributors
// For license information, please see license.txt

const GROUPS =
	"cova_clinic_integration.cova_clinic_integration.doctype.clinic_test_schedule.clinic_test_schedule.test_group_filters";

frappe.ui.form.on("Clinic Test Plan", {
	onload(frm) {
		frappe.call({ method: GROUPS }).then((r) => {
			frm.set_df_property("test_group", "options", r.message || []);
		});
	},

	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		const scheduled = (frm.doc.rounds || []).some((r) => r.status === "Scheduled");
		if (!scheduled) {
			frm.add_custom_button(__("Generate Rounds"), () =>
				frm.call({ doc: frm.doc, method: "generate_rounds" }).then(() => frm.reload_doc())
			);
		}
		const next = (frm.doc.rounds || []).find((r) => r.status === "Planned");
		if (next || !(frm.doc.rounds || []).length) {
			frm.add_custom_button(__("Schedule Next Round"), () => {
				frm.call({
					doc: frm.doc,
					method: "schedule_next_round",
					freeze: true,
					freeze_message: __("Scheduling and sending to Cova…"),
				}).then((r) => {
					frm.reload_doc();
					const m = r.message || {};
					let msg = __("Round {0}: {1} employees scheduled on {2}.", [
						m.round,
						m.scheduled,
						m.schedule,
					]);
					if ((m.on_leave || []).length) {
						msg +=
							"<br>" +
							__("{0} on leave will carry over to the next round: {1}", [
								m.on_leave.length,
								m.on_leave
									.map((e) => frappe.utils.escape_html(e.employee_name))
									.join(", "),
							]);
					}
					frappe.msgprint({ title: __("Test Plan"), message: msg, indicator: "green" });
				});
			}).addClass("btn-primary");
		}
	},
});

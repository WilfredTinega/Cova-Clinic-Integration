// Copyright (c) 2026, Upande LTD and contributors
// For license information, please see license.txt

frappe.ui.form.on("Clinic Test Schedule", {
	setup(frm) {
		frm.set_query("test_package", () => ({ filters: { disabled: 0 } }));
		frm.set_query("employee", "employees", () => ({
			filters: { status: "Active", company: frm.doc.company },
		}));
	},

	onload(frm) {
		frappe
			.call({
				method: "cova_clinic_integration.cova_clinic_integration.doctype.clinic_test_schedule.clinic_test_schedule.test_group_filters",
			})
			.then((r) => {
				frm.set_df_property("test_group", "options", r.message || []);
			});
	},

	test_group(frm) {
		if (!frm.doc.test_group) {
			return;
		}
		frappe
			.call({
				method: "cova_clinic_integration.cova_clinic_integration.doctype.clinic_test_schedule.clinic_test_schedule.test_group_filters",
				args: { group: frm.doc.test_group },
			})
			.then((r) => {
				const g = r.message || {};
				frm.set_value("test_package", g.test_package);
				frm.set_value("employees_per_designation", g.employees_per_designation || 0);
				frm.set_value(
					"departments",
					(g.departments || []).map((d) => ({ department: d }))
				);
				frm.set_value(
					"designations",
					(g.designations || []).map((d) => ({ designation: d }))
				);
				if (!frm.doc.title && frm.doc.scheduled_from) {
					frm.set_value(
						"title",
						frm.doc.scheduled_from.slice(0, 4) + " " + frm.doc.test_group
					);
				}
			});
	},

	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		frm.add_custom_button(__("Get Employees"), () => {
			frm.call({
				doc: frm.doc,
				method: "get_employees",
				freeze: true,
				freeze_message: __("Finding employees…"),
			}).then((r) => {
				frm.reload_doc();
				const m = r.message || {};
				frappe.show_alert({
					message: __("{0} employees added ({1} on the schedule)", [
						m.added || 0,
						m.total || 0,
					]),
					indicator: m.added ? "green" : "orange",
				});
			});
		});

		const pending = (frm.doc.employees || []).filter((r) => !r.test_request).length;
		if (pending) {
			frm.add_custom_button(__("Create Test Requests"), () => {
				frappe.confirm(
					__("Raise {0} Clinic Test Requests for {1} to {2}?", [
						pending,
						frappe.datetime.str_to_user(frm.doc.scheduled_from),
						frappe.datetime.str_to_user(frm.doc.scheduled_to),
					]),
					() =>
						frm
							.call({
								doc: frm.doc,
								method: "create_test_requests",
								freeze: true,
								freeze_message: __("Scheduling…"),
							})
							.then((r) => {
								frm.reload_doc();
								const m = r.message || {};
								let msg = __("{0} test requests created.", [m.created || 0]);
								if (frm.doc.send_to_cova) {
									msg += m.send_skipped
										? "<br>" +
										  __(
												"Nothing was sent: Cova is not configured in Cova Clinic Settings."
										  )
										: "<br>" +
										  __("{0} sent to Cova, {1} refused.", [
												m.sent || 0,
												m.failed || 0,
										  ]);
								}
								(m.failures || []).forEach((f) => {
									msg +=
										"<br>" +
										frappe.utils.escape_html(f.employee + ": " + f.reason);
								});
								frappe.msgprint({
									title: __("Schedule"),
									message: msg,
									indicator: m.failed ? "orange" : "green",
								});
							})
				);
			}).addClass("btn-primary");
		}
	},

	scheduled_from(frm) {
		if (frm.doc.scheduled_from && !frm.doc.scheduled_to) {
			frm.set_value("scheduled_to", frappe.datetime.add_days(frm.doc.scheduled_from, 14));
		}
	},
});

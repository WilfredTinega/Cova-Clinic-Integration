// Copyright (c) 2026, Upande LTD and contributors
// For license information, please see license.txt

frappe.ui.form.on("Clinic Ticket", {
    setup(frm) {
        frm.set_query("employee", () => ({ filters: { status: "Active" } }));
    },

    refresh(frm) {
        if (frm.is_new() || frm.doc.status !== "Issued") { return; }

        frm.add_custom_button(__("Cancel Ticket"), () => {
            frm.set_value("status", "Cancelled").then(() => frm.save());
        });

        // Gate Pass comes from upande_ta, which not every site runs; the
        // `gate_pass` field is only added where it does (setup.install_gate_pass_link).
        // One ticket, one gate pass: the ticket links its pass the moment the
        // pass is saved, and the server refuses a second live one either way.
        if (frm.fields_dict.gate_pass && !frm.doc.gate_pass && frappe.model.can_create("Gate Pass")) {
            frm.add_custom_button(__("Raise Gate Pass"), () => {
                frappe.db.get_value("Gate Pass",
                    { clinic_ticket: frm.doc.name, docstatus: ["<", 2], workflow_state: ["!=", "Rejected"] }, "name")
                    .then((r) => {
                        const existing = r.message && r.message.name;
                        if (existing) {
                            frappe.set_route("Form", "Gate Pass", existing);
                            return;
                        }
                        frappe.new_doc("Gate Pass", {
                            employee: frm.doc.employee,
                            date: frm.doc.ticket_date,
                            pass_type: "Medical",
                            reason: frm.doc.reason,
                            clinic_ticket: frm.doc.name
                        });
                    });
            });
        }
    },

    ticket_date(frm) {
        if (frm.doc.ticket_date && (!frm.doc.valid_until || frm.doc.valid_until < frm.doc.ticket_date)) {
            frm.set_value("valid_until", frm.doc.ticket_date);
        }
    }
});

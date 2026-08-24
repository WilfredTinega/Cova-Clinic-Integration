// Copyright (c) 2026, Upande Limited and contributors
// For license information, please see license.txt
// Employee form — COVA register / request-test / deactivate buttons.
// Ported from the live "Register to Cova" Client Script.

frappe.ui.form.on('Employee', {
    refresh: function(frm) {
        if (frm.is_new()) {
            return;
        }

        // Which Clinic buttons make sense depends on whether this employee is
        // already on Cova. The Cova Members row is the signal to use — not
        // Employee.cova_member_id, which is only filled in when Cova's reply
        // carries a covaMemberId, so a mocked or partial response leaves it
        // empty even though the member exists.
        frappe.db.get_value('Cova Members', { employee: frm.doc.name }, ['name', 'status'])
            .then(function(r) {
                const member = (r && r.message && r.message.name) ? r.message : null;
                const registered = !!(member && member.status === 'Active');
                cova_clinic_buttons(frm, member, registered);
            });
    }
});

function cova_clinic_buttons(frm, member, registered) {
    if (registered) {
        frm.dashboard.add_indicator(__('Registered on Cova'), 'green');
    } else if (member) {
        frm.dashboard.add_indicator(__('Deactivated on Cova'), 'orange');
    }

    // ─── REGISTER TO COVA ───────────────────────────────
    // Hidden once the member is on Cova — registering twice just returns the
    // existing member and confuses the audit log.
    if (!registered) {
        frm.add_custom_button(__('Register to Cova'), function() {
            frappe.confirm(
                __('Register {0} to Cova?', [frm.doc.employee_name]),
                function() {
                    frappe.dom.freeze(__('Registering...'));

                    fetch('/api/method/cova_clinic_api', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-Frappe-CSRF-Token': frappe.csrf_token
                        },
                        body: JSON.stringify({
                            action: 'register_member',
                            member_type: 'Active',
                            employee: frm.doc.name
                        })
                    })
                    .then(function(res) { return res.text(); })
                    .then(function(text) {
                        frappe.dom.unfreeze();
                        let data;
                        try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
                        const msg = data.message || {};
                        if (msg && !msg.error) {
                            frappe.show_alert({ message: __('Registered to Cova successfully'), indicator: 'green' });
                            frm.reload_doc();
                        } else {
                            frappe.msgprint({
                                title: __('Registration Failed'),
                                message: (msg.error || JSON.stringify(data)),
                                indicator: 'red'
                            });
                        }
                        console.log('Cova register response:', data);
                    })
                    .catch(function(err) {
                        frappe.dom.unfreeze();
                        frappe.msgprint(__('Request failed: ') + err);
                    });
                }
            );
        }, __('Clinic'));
    }

    // ─── REQUEST MEDICAL TEST ───────────────────────────
    frm.add_custom_button(__('Request Medical Test'), function() {
        frappe.prompt(
            [
                {
                    fieldname: 'test_package',
                    label: __('Test Package'),
                    fieldtype: 'Select',
                    options: ['Pre Employment Wellness', 'Cholinesterase', 'Food Handler', 'Annual Medical', 'Exit Medical'].join('\n'),
                    reqd: 1
                },
                {
                    fieldname: 'scheduled_from',
                    label: __('Scheduled From'),
                    fieldtype: 'Date',
                    reqd: 1,
                    default: frappe.datetime.get_today()
                },
                {
                    fieldname: 'scheduled_to',
                    label: __('Scheduled To'),
                    fieldtype: 'Date',
                    reqd: 1,
                    default: frappe.datetime.add_days(frappe.datetime.get_today(), 15)
                },
                {
                    fieldname: 'notes',
                    label: __('Notes'),
                    fieldtype: 'Small Text'
                }
            ],
            function(values) {
                frappe.dom.freeze(__('Creating request...'));

                frappe.call({
                    method: 'frappe.client.insert',
                    args: {
                        doc: {
                            doctype: 'Clinic Test Request',
                            member_type: 'Active',
                            employee: frm.doc.name,
                            status: 'Pending',
                            test_package: values.test_package,
                            scheduled_from: values.scheduled_from,
                            scheduled_to: values.scheduled_to,
                            notes: values.notes || ''
                        }
                    },
                    callback: function(r) {
                        if (!r.exc && r.message) {
                            const request_name = r.message.name;
                            // Now send it to Cova
                            fetch('/api/method/cova_clinic_api', {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'X-Frappe-CSRF-Token': frappe.csrf_token
                                },
                                body: JSON.stringify({
                                    action: 'submit_test_request',
                                    request_name: request_name
                                })
                            })
                            .then(function(res) { return res.text(); })
                            .then(function(text) {
                                frappe.dom.unfreeze();
                                let data;
                                try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
                                const msg = data.message || {};
                                if (msg && !msg.error) {
                                    frappe.show_alert({
                                        message: __('{0} request created and sent to Cova ({1})', [values.test_package, request_name]),
                                        indicator: 'green'
                                    });
                                } else {
                                    frappe.msgprint({
                                        title: __('Created but Cova send failed'),
                                        message: __('Request {0} created. Cova error: {1}', [request_name, (msg.error || JSON.stringify(data))]),
                                        indicator: 'orange'
                                    });
                                }
                                console.log('Cova test request response:', data);
                            })
                            .catch(function(err) {
                                frappe.dom.unfreeze();
                                frappe.msgprint(__('Request created but Cova send failed: ') + err);
                            });
                        } else {
                            frappe.dom.unfreeze();
                            frappe.msgprint(__('Failed to create test request.'));
                        }
                    }
                });
            },
            __('Request Medical Test'),
            __('Create & Send')
        );
    }, __('Clinic'));

    // ─── DEACTIVATE FROM COVA ───────────────────────────
    // The mirror of the above: nothing to deactivate until they are on Cova.
    if (registered) {
        frm.add_custom_button(__('Deactivate from Cova'), function() {
            frappe.prompt(
                [
                    {
                        fieldname: 'exit_date',
                        label: __('Exit Date'),
                        fieldtype: 'Date',
                        reqd: 1,
                        default: frm.doc.relieving_date || frappe.datetime.get_today()
                    },
                    {
                        fieldname: 'requires_exit_medical',
                        label: __('Requires Exit Medical'),
                        fieldtype: 'Check',
                        default: 0
                    }
                ],
                function(values) {
                    frappe.dom.freeze(__('Deactivating...'));

                    fetch('/api/method/cova_clinic_api', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-Frappe-CSRF-Token': frappe.csrf_token
                        },
                        body: JSON.stringify({
                            action: 'deactivate_member',
                            employee: frm.doc.name,
                            exit_date: values.exit_date,
                            requires_exit_medical: values.requires_exit_medical ? true : false
                        })
                    })
                    .then(function(res) { return res.text(); })
                    .then(function(text) {
                        frappe.dom.unfreeze();
                        let data;
                        try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
                        const msg = data.message || {};
                        if (msg && !msg.error) {
                            frappe.show_alert({ message: __('Deactivated from Cova successfully'), indicator: 'green' });
                            frm.reload_doc();
                        } else {
                            frappe.msgprint({
                                title: __('Deactivation Failed'),
                                message: (msg.error || JSON.stringify(data)),
                                indicator: 'red'
                            });
                        }
                        console.log('Cova deactivate response:', data);
                    })
                    .catch(function(err) {
                        frappe.dom.unfreeze();
                        frappe.msgprint(__('Request failed: ') + err);
                    });
                },
                __('Deactivate from Cova'),
                __('Deactivate')
            );
        }, __('Clinic'));
    }
}
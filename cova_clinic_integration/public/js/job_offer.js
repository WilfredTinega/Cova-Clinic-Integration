// Copyright (c) 2026, Upande Limited and contributors
// For license information, please see license.txt
// Job Offer form — COVA pre-employment 'Register & Request Wellness' button.
// Ported from the live "Cova Medical Cover" Client Script.

// National ID and Phone Number are required before a candidate can be sent to
// COVA, so they are mandatory on offers for the company configured in Cova
// Clinic Settings. The server enforces the same rule in job_offer.validate();
// this only mirrors it in the form so the asterisks show up.
function toggle_cova_mandatory(frm) {
    const clinic_company = frappe.boot.cova_clinic_company;
    const applies = Boolean(clinic_company) && frm.doc.company === clinic_company;
    ['custom_national_id', 'custom_phone_number'].forEach(function(fieldname) {
        if (frm.fields_dict[fieldname]) {
            frm.toggle_reqd(fieldname, applies);
        }
    });
}

frappe.ui.form.on('Job Offer', {
    company: function(frm) {
        toggle_cova_mandatory(frm);
    },

    onload: function(frm) {
        toggle_cova_mandatory(frm);
    },

    refresh: function(frm) {
        toggle_cova_mandatory(frm);

        if (frm.is_new()) {
            return;
        }

        // Already tested - candidate is locked, no button
        if (frm.doc.custom_cova_tested) {
            frm.dashboard.add_comment(__('This candidate has completed their pre-employment wellness test and is locked.'), 'blue', true);
            return;
        }

        // ─── REGISTER & REQUEST WELLNESS ────────────────────
        frm.add_custom_button(__('Register & Request Wellness'), function() {
            if (!frm.doc.custom_national_id) {
                frappe.msgprint(__('Please enter the National ID before registering.'));
                return;
            }

            frappe.confirm(
                __('Register {0} to Cova pre-employment scheme and request a wellness test?', [frm.doc.applicant_name]),
                function() {
                    frappe.dom.freeze(__('Registering candidate...'));

                    fetch('/api/method/cova_clinic_api', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-Frappe-CSRF-Token': frappe.csrf_token
                        },
                        body: JSON.stringify({
                            action: 'register_preemployment_candidate',
                            job_offer: frm.doc.name
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
                                message: __('Registered & wellness test requested ({0})', [msg.test_request || '']),
                                indicator: 'green'
                            });
                            frm.reload_doc();
                        } else {
                            frappe.msgprint({
                                title: __('Registration Failed'),
                                message: (msg.error || JSON.stringify(data)),
                                indicator: 'red'
                            });
                        }
                        console.log('Cova pre-employment response:', data);
                    })
                    .catch(function(err) {
                        frappe.dom.unfreeze();
                        frappe.msgprint(__('Request failed: ') + err);
                    });
                }
            );
        }, __('Clinic'));
    }
});

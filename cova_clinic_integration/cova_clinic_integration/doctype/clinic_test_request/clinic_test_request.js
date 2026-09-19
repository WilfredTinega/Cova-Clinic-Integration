// Copyright (c) 2026, Upande Limited and contributors
// For license information, please see license.txt
//
// Clinic Test Request form — send (or re-send) the request to COVA.
//
// A request reaches the clinic only when `submit_test_request` is POSTed for it.
// Nothing about typing one into the desk does that, so saving now asks outright
// whether to send it, and the record carries the answer: `received_by_cova` is
// ticked from COVA's own reply. Answer No and the request simply sits there with
// the Clinic → Send to Clinic button waiting, which is also the button for a
// send COVA refused.

// The same code is installed as a Client Script on sites that cannot wait for a
// deploy. Both copies set this flag, so whichever loads first wins and the
// buttons are never registered twice.
frappe.provide('frappe.cova');
if (!frappe.cova.clinic_test_request_form) {
frappe.cova.clinic_test_request_form = true;

frappe.ui.form.on('Clinic Test Request', {
    refresh: function(frm) {
        if (frm.is_new()) {
            return;
        }
        if (frm.doc.status === 'Cancelled') {
            return;
        }

        const delivered = cova_is_delivered(frm);

        frm.add_custom_button(delivered ? __('Re-send to Clinic') : __('Send to Clinic'), function() {
            cova_send_request(frm, delivered, false);
        }, __('Clinic'));

        frm.dashboard.add_indicator(
            delivered ? __('Received by Cova') : __('Not sent to Cova yet'),
            delivered ? 'green' : 'orange'
        );
    },

    after_save: function(frm) {
        // Asked on every save until it is in, not only on the first one: a
        // request that was refused (bad identifier, member not enrolled) is
        // normally fixed by editing it, and that edit is exactly the moment to
        // offer the send again.
        if (frm.doc.status === 'Cancelled' || cova_is_delivered(frm)) {
            return;
        }

        frappe.confirm(
            __('Send this test request to Cova now?') +
                '<br><br><b>' + frappe.utils.escape_html(frm.doc.test_package || '') + '</b> — ' +
                frappe.utils.escape_html(frm.doc.employee || frm.doc.nationa_id || frm.doc.name),
            function() {
                cova_send_request(frm, false, true);
            },
            function() {
                frappe.show_alert({
                    message: __('Not sent. Use Clinic → Send to Clinic when you are ready.'),
                    indicator: 'orange'
                });
            }
        );
    }
});

// Proof the request reached the clinic. `received_by_cova` is the direct answer;
// a linked result is the older one — it cannot exist unless COVA had the
// request — and is what keeps rows raised before that field existed from being
// reported as never sent.
function cova_is_delivered(frm) {
    return !!(frm.doc.received_by_cova || frm.doc.linked_test_result);
}

// Everything that would make the send pointless, checked before COVA is called.
// Returns a list of warnings to show in the confirmation, or null when the
// request cannot go at all (the reason is shown from here).
function cova_precheck(frm) {
    const warnings = [];

    if (frm.doc.member_type === 'Active') {
        if (!frm.doc.employee) {
            frappe.msgprint({
                title: __('Cannot Send'),
                message: __('An Active request needs an Employee — Cova identifies the member by payroll number.'),
                indicator: 'red'
            });
            return null;
        }
        if (!frm.doc.payroll_number) {
            // Not a blocker: the endpoint falls back to the Employee ID, which is
            // what the API route has always sent for employees with no
            // employee_number. Worth saying out loud all the same.
            warnings.push(__('Payroll Number is blank — the Employee ID ({0}) will be sent as the member identifier.',
                [frm.doc.employee]));
        }
    } else if (!frm.doc.nationa_id) {
        frappe.msgprint({
            title: __('Cannot Send'),
            message: __('A Pre Employment request needs a National ID — Cova identifies the candidate by it.'),
            indicator: 'red'
        });
        return null;
    }

    return warnings;
}

function cova_send_request(frm, delivered, confirmed) {
    const warnings = cova_precheck(frm);
    if (warnings === null) {
        return;
    }

    // The save prompt has already asked; asking again for the same send is just
    // a second dialog in the way.
    if (confirmed) {
        cova_post_request(frm);
        return;
    }

    if (delivered) {
        warnings.unshift(__('Cova has already received this request. Sending again raises a second test.'));
    }

    let message = __('Send {0} for {1} to Cova?', [
        frappe.utils.escape_html(frm.doc.test_package || ''),
        frappe.utils.escape_html(frm.doc.employee || frm.doc.nationa_id || frm.doc.name)
    ]);
    if (warnings.length) {
        message += '<br><br><span class="text-warning">' + warnings.join('<br>') + '</span>';
    }

    frappe.confirm(message, function() {
        cova_post_request(frm);
    });
}

function cova_post_request(frm) {
    frappe.dom.freeze(__('Sending to Cova...'));

    fetch('/api/method/cova_clinic_api', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-Frappe-CSRF-Token': frappe.csrf_token
        },
        body: JSON.stringify({
            action: 'submit_test_request',
            request_name: frm.doc.name
        })
    })
    .then(function(res) { return res.text(); })
    .then(function(text) {
        frappe.dom.unfreeze();
        let data;
        try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
        const outcome = cova_read_outcome(data);
        cova_log_attempt(frm.doctype, frm.doc.name, outcome);

        if (outcome.state === 'failed') {
            frappe.msgprint({
                title: __('Cova Refused the Request'),
                message: __('Nothing was sent. Cova said: {0}', [frappe.utils.escape_html(outcome.reason)]) +
                    '<br><br>' + __('Fix the cause and use <b>Clinic → Send to Clinic</b>, or re-send it in bulk from the list view.'),
                indicator: 'red'
            });
        } else {
            frappe.show_alert({ message: __('Sent to Cova'), indicator: 'green' });
        }
        // Brings back `received_by_cova` and `cova_member_id`, both written
        // server-side from Cova's reply.
        frm.reload_doc();
        console.log('Cova test request response:', data);
    })
    .catch(function(err) {
        frappe.dom.unfreeze();
        cova_log_attempt(frm.doctype, frm.doc.name, { state: 'failed', reason: String(err) });
        frappe.msgprint({
            title: __('Request Failed'),
            message: __('The request never reached the server: {0}', [String(err)]),
            indicator: 'red'
        });
    });
}

// What actually happened to one request. cova_post no longer raises when COVA
// rejects a row — it hands back COVA's own message — so a failure has a reason
// worth showing rather than being reduced to "something went wrong".
function cova_read_outcome(data) {
    const msg = (data && data.message) || {};

    if (data && data._server_messages) {
        let reason = data._server_messages;
        try {
            reason = JSON.parse(data._server_messages)
                .map(function(m) { try { return JSON.parse(m).message; } catch (e) { return m; } })
                .join(' ');
        } catch (e) { /* leave it as the raw string */ }
        return { state: 'failed', reason: frappe.utils.strip_html(String(reason)) };
    }
    if (data && data.exc_type) {
        return { state: 'failed', reason: data.exc_type };
    }
    if (msg && msg.error) {
        return { state: 'failed', reason: String(msg.error) };
    }
    if (msg && msg.status === 'duplicate') {
        return { state: 'duplicate', reason: msg.message || __('Already raised on Cova') };
    }
    return { state: 'done', reason: '' };
}

// The timeline keeps what the checkbox cannot: the refusals. `received_by_cova`
// answers "is it in?", these comments answer "what happened when we tried?".
function cova_log_attempt(doctype, name, outcome) {
    const content = outcome.state === 'failed'
        ? __('Cova send FAILED: {0}', [frappe.utils.escape_html(outcome.reason)])
        : (outcome.state === 'duplicate'
            ? __('Cova reported this request as already raised: {0}', [frappe.utils.escape_html(outcome.reason)])
            : __('Sent to Cova.'));

    frappe.call({
        method: 'frappe.desk.form.utils.add_comment',
        args: {
            reference_doctype: doctype,
            reference_name: name,
            content: content,
            comment_email: frappe.session.user,
            comment_by: frappe.session.user_fullname
        }
    });
}

} // frappe.cova.clinic_test_request_form

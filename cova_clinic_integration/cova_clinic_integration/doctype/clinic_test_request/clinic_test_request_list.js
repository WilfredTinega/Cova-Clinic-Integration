// Copyright (c) 2026, Upande Limited and contributors
// For license information, please see license.txt
//
// Clinic Test Request list — re-send refused requests to COVA in bulk.
//
// A request that COVA refused (bad identifier, member not enrolled, endpoint
// down) stays Pending with no result against it, and until now the only way to
// push it again was to open every record. Tick the rows and send them.
//
// Wrapped in an IIFE and written over a copy of whatever listview_settings
// already exist for the doctype, so nothing another app registered is dropped.

(function() {
    // The same code is installed as a Client Script on sites that cannot wait
    // for a deploy. Both copies set this flag, so whichever loads first wins
    // and the list button is never registered twice.
    frappe.provide('frappe.cova');
    if (frappe.cova.clinic_test_request_list) { return; }
    frappe.cova.clinic_test_request_list = true;

    const DOCTYPE = 'Clinic Test Request';
    const prior = frappe.listview_settings[DOCTYPE] || {};
    const prior_onload = prior.onload;

    // Pulled into the list data so the selection can be vetted client-side —
    // get_checked_items() only carries the fields the list actually fetched.
    const COVA_FIELDS = [
        'status',
        'received_by_cova',
        'member_type',
        'employee',
        'payroll_number',
        'nationa_id',
        'test_package',
        'scheduled_from',
        'scheduled_to',
        'linked_test_result'
    ];

    const add_fields = (prior.add_fields || []).slice();
    COVA_FIELDS.forEach(function(f) {
        if (add_fields.indexOf(f) === -1) { add_fields.push(f); }
    });

    frappe.listview_settings[DOCTYPE] = Object.assign({}, prior, {
        add_fields: add_fields,

        onload: function(listview) {
            if (prior_onload) { prior_onload.call(this, listview); }

            function start() {
                const selected = listview.get_checked_items();

                if (!selected.length) {
                    frappe.msgprint({
                        title: __('Nothing Selected'),
                        message: __('Tick the requests that never reached Cova, then press <b>Re-send to Clinic</b> again.'),
                        indicator: 'orange'
                    });
                    return;
                }
                review(selected);
            }

            // Offered in both places, matching the Employee and Job Offer lists:
            // a standing button, and the Actions menu Frappe reveals once rows
            // are ticked.
            listview.page.add_inner_button(__('Re-send to Clinic'), start, __('Clinic'));
            listview.page.add_actions_menu_item(__('Re-send to Clinic'), start, true);

            // Why a row can or cannot be pushed again. A request Cova has already
            // answered is held back by default — re-sending it raises a second
            // test on the member's account, which someone has to pay for.
            function classify(row) {
                if (row.status === 'Cancelled') {
                    return { ok: false, reason: __('Cancelled') };
                }
                // Cova said it has this one. Re-sending raises a second test on
                // the member's account, which someone has to pay for — the form
                // button is the deliberate way to do that.
                if (row.received_by_cova) {
                    return { ok: false, reason: __('Received by Cova') };
                }
                if (row.linked_test_result) {
                    return { ok: false, reason: __('Result already received ({0})', [row.linked_test_result]) };
                }
                if (row.member_type === 'Active') {
                    if (!row.employee) {
                        return { ok: false, reason: __('No Employee') };
                    }
                    return {
                        ok: true,
                        reason: row.payroll_number ? __('Ready') : __('Ready — no payroll no., Employee ID will be sent')
                    };
                }
                if (!row.nationa_id) {
                    return { ok: false, reason: __('No National ID') };
                }
                return { ok: true, reason: __('Ready') };
            }

            function review(selected) {
                const rows = selected.map(function(row) {
                    return { row: row, verdict: classify(row) };
                });
                const eligible = rows.filter(function(x) { return x.verdict.ok; });

                if (!eligible.length) {
                    frappe.msgprint({
                        title: __('Nothing to Re-send'),
                        message: render_table(rows),
                        indicator: 'orange'
                    });
                    return;
                }

                const d = new frappe.ui.Dialog({
                    title: __('Re-send to Clinic'),
                    size: 'large',
                    fields: [{ fieldname: 'summary_html', fieldtype: 'HTML' }],
                    primary_action_label: __('Send ({0})', [eligible.length]),
                    primary_action: function() {
                        d.hide();
                        run_bulk(eligible.map(function(x) { return x.row; }));
                    }
                });

                d.fields_dict.summary_html.$wrapper.html(render_table(rows));
                d.show();
            }

            function render_table(rows) {
                let html = '<div style="max-height:380px;overflow-y:auto;border:1px solid var(--border-color);border-radius:6px;">';
                html += '<table class="table table-bordered" style="margin:0;font-size:13px;">';
                html += '<thead style="position:sticky;top:0;background:var(--fg-color);z-index:1;"><tr>';
                html += '<th>' + __('Request') + '</th><th>' + __('Member') + '</th><th>' + __('Package') + '</th>';
                html += '<th>' + __('Status') + '</th><th>' + __('At Cova') + '</th><th>' + __('Verdict') + '</th>';
                html += '</tr></thead><tbody>';

                rows.forEach(function(x) {
                    const cls = x.verdict.ok ? 'text-success' : 'text-muted';
                    const member = x.row.member_type === 'Active'
                        ? (x.row.employee || '')
                        : (x.row.nationa_id || '');
                    html += '<tr>';
                    html += '<td>' + frappe.utils.escape_html(x.row.name) + '</td>';
                    html += '<td>' + (member
                        ? frappe.utils.escape_html(member)
                        : '<span class="text-danger">' + __('missing') + '</span>') + '</td>';
                    html += '<td>' + frappe.utils.escape_html(x.row.test_package || '') + '</td>';
                    html += '<td>' + frappe.utils.escape_html(x.row.status || '') + '</td>';
                    html += '<td>' + (x.row.received_by_cova ? __('Received') : '—') + '</td>';
                    html += '<td class="' + cls + '">' + x.verdict.reason + '</td>';
                    html += '</tr>';
                });

                html += '</tbody></table></div>';
                const skipped = rows.filter(function(x) { return !x.verdict.ok; }).length;
                if (skipped) {
                    html += '<div class="text-muted" style="margin-top:6px;">' +
                        __('{0} of {1} selected will be skipped.', [skipped, rows.length]) + '</div>';
                }
                return html;
            }

            function read_outcome(data) {
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

            // The attempt is recorded on the document's timeline — the doctype
            // has no "delivered" field, and without a trace a request that was
            // refused looks exactly like one nobody ever sent.
            function log_attempt(name, outcome) {
                const content = outcome.state === 'failed'
                    ? __('Cova send FAILED: {0}', [frappe.utils.escape_html(outcome.reason)])
                    : (outcome.state === 'duplicate'
                        ? __('Cova reported this request as already raised: {0}', [frappe.utils.escape_html(outcome.reason)])
                        : __('Sent to Cova.'));

                return frappe.call({
                    method: 'frappe.desk.form.utils.add_comment',
                    args: {
                        reference_doctype: DOCTYPE,
                        reference_name: name,
                        content: content,
                        comment_email: frappe.session.user,
                        comment_by: frappe.session.user_fullname
                    }
                });
            }

            // Sent one at a time on purpose: these go straight out to Cova, and
            // firing a whole ticked page at once turns one bad selection into a
            // burst the clinic's endpoint answers with rate-limit errors that
            // read like real refusals.
            function run_bulk(rows) {
                const total = rows.length;
                const failures = [];
                let done = 0, duplicates = 0;

                function finish() {
                    frappe.hide_progress();
                    listview.clear_checked_items && listview.clear_checked_items();
                    listview.refresh();

                    let message = __('Sent: {0}', [done]);
                    if (duplicates) {
                        message += '<br>' + __('Already raised on Cova: {0}', [duplicates]);
                    }
                    message += '<br>' + __('Failed: {0}', [failures.length]);

                    if (failures.length) {
                        message += '<div style="max-height:220px;overflow-y:auto;margin-top:10px;' +
                            'border:1px solid var(--border-color);border-radius:6px;">' +
                            '<table class="table table-bordered" style="margin:0;font-size:12px;">' +
                            '<thead><tr><th>' + __('Request') + '</th><th>' + __('Reason') + '</th></tr></thead><tbody>';
                        failures.forEach(function(f) {
                            message += '<tr><td>' + frappe.utils.escape_html(f.name) + '</td><td>' +
                                frappe.utils.escape_html(f.reason) + '</td></tr>';
                        });
                        message += '</tbody></table></div>';
                    }

                    frappe.msgprint({
                        title: __('Re-send Complete'),
                        message: message,
                        indicator: failures.length ? 'orange' : 'green'
                    });
                }

                function send_next(i) {
                    if (i >= total) {
                        finish();
                        return;
                    }
                    const row = rows[i];
                    frappe.show_progress(__('Sending to Cova'), i, total, __('{0} ({1} of {2})', [row.name, i + 1, total]));

                    fetch('/api/method/cova_clinic_api', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-Frappe-CSRF-Token': frappe.csrf_token
                        },
                        body: JSON.stringify({ action: 'submit_test_request', request_name: row.name })
                    })
                    .then(function(res) { return res.text(); })
                    .then(function(text) {
                        let data;
                        try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
                        const outcome = read_outcome(data);
                        if (outcome.state === 'done') { done++; }
                        else if (outcome.state === 'duplicate') { duplicates++; }
                        else { failures.push({ name: row.name, reason: outcome.reason }); }
                        log_attempt(row.name, outcome);
                    })
                    .catch(function(err) {
                        failures.push({ name: row.name, reason: String(err) });
                        log_attempt(row.name, { state: 'failed', reason: String(err) });
                    })
                    .finally(function() {
                        send_next(i + 1);
                    });
                }

                send_next(0);
            }
        }
    });
})();

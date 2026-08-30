// Copyright (c) 2026, Upande Limited and contributors
// For license information, please see license.txt
// Job Offer list — bulk COVA pre-employment register + wellness request.
//
// HRMS ships its own frappe.listview_settings["Job Offer"] (status indicators,
// add_fields) and this file is concatenated after it into the same __list_js
// blob, so a plain assignment would silently drop HRMS's settings. Extend the
// existing object instead and chain its onload.

(function() {
    const DOCTYPE = 'Job Offer';
    const prior = frappe.listview_settings[DOCTYPE] || {};
    const prior_onload = prior.onload;

    // Pulled into the list data so the selection can be vetted client-side.
    const COVA_FIELDS = [
        'company',
        'designation',
        'applicant_name',
        'national_id',
        'phone_number',
        'cova_registered',
        'cova_tested'
    ];

    const add_fields = (prior.add_fields || []).slice();
    COVA_FIELDS.forEach(function(f) {
        if (add_fields.indexOf(f) === -1) { add_fields.push(f); }
    });

    frappe.listview_settings[DOCTYPE] = Object.assign({}, prior, {
        add_fields: add_fields,

        onload: function(listview) {
            if (prior_onload) { prior_onload.call(this, listview); }

            // The company the integration is configured for, published by the
            // extend_bootinfo hook. Null until one is picked in the settings.
            const LOCKED_COMPANY = frappe.boot.cova_clinic_company;

            function start() {
                if (!LOCKED_COMPANY) {
                    frappe.msgprint({
                        title: __('Not Configured'),
                        message: __('Pick a Company in Cova Clinic Settings first.'),
                        indicator: 'orange'
                    });
                    return;
                }

                const selected = listview.get_checked_items();

                if (!selected.length) {
                    frappe.msgprint(__('Select at least one Job Offer.'));
                    return;
                }

                // Existing pre-employment members are skipped so we never register twice.
                frappe.call({
                    method: 'frappe.client.get_list',
                    args: {
                        doctype: 'Cova Members',
                        filters: [['member_type', '=', 'Pre Employment']],
                        fields: ['national_id'],
                        limit_page_length: 0
                    },
                    callback: function(r) {
                        const members = (r.message || [])
                            .map(function(m) { return m.national_id; })
                            .filter(Boolean);
                        review(selected, members);
                    }
                });
            }

            // Offered in two places: a standing button (visible before anything is
            // ticked, matching the Employee list) and the Actions menu that Frappe
            // reveals once rows are checked.
            listview.page.add_inner_button(__('Register & Request Wellness'), start, __('Clinic'));
            listview.page.add_actions_menu_item(__('Register & Request Wellness'), start, true);

            function classify(row, cova_national_ids) {
                if (row.company !== LOCKED_COMPANY) {
                    return { ok: false, reason: __('Not {0}', [LOCKED_COMPANY]) };
                }
                if (row.docstatus === 2) {
                    return { ok: false, reason: __('Cancelled') };
                }
                if (row.cova_tested) {
                    return { ok: false, reason: __('Already tested') };
                }
                if (!row.national_id) {
                    return { ok: false, reason: __('No National ID') };
                }
                if (!row.phone_number) {
                    return { ok: false, reason: __('No Phone Number') };
                }
                if (cova_national_ids.indexOf(row.national_id) !== -1) {
                    return { ok: false, reason: __('Already a Cova member') };
                }
                return { ok: true, reason: row.cova_registered ? __('Registered, retry') : __('Ready') };
            }

            function review(selected, cova_national_ids) {
                const rows = selected.map(function(row) {
                    return { row: row, verdict: classify(row, cova_national_ids) };
                });
                const eligible = rows.filter(function(x) { return x.verdict.ok; });

                if (!eligible.length) {
                    frappe.msgprint({
                        title: __('Nothing to Register'),
                        message: render_table(rows),
                        indicator: 'orange'
                    });
                    return;
                }

                const d = new frappe.ui.Dialog({
                    title: __('Pre-Employment Wellness'),
                    size: 'large',
                    fields: [
                        { fieldname: 'summary_html', fieldtype: 'HTML' }
                    ],
                    primary_action_label: __('Register & Request ({0})', [eligible.length]),
                    primary_action: function() {
                        d.hide();
                        run_bulk(eligible.map(function(x) { return x.row.name; }));
                    }
                });

                d.fields_dict.summary_html.$wrapper.html(render_table(rows));
                d.show();
            }

            function render_table(rows) {
                let html = '<div style="max-height:380px;overflow-y:auto;border:1px solid var(--border-color);border-radius:6px;">';
                html += '<table class="table table-bordered" style="margin:0;font-size:13px;">';
                html += '<thead style="position:sticky;top:0;background:var(--fg-color);z-index:1;"><tr>';
                html += '<th>' + __('Name') + '</th><th>' + __('National ID') + '</th><th>' + __('Designation') + '</th><th>' + __('Status') + '</th>';
                html += '</tr></thead><tbody>';

                rows.forEach(function(x) {
                    const cls = x.verdict.ok ? 'text-success' : 'text-muted';
                    html += '<tr>';
                    html += '<td>' + frappe.utils.escape_html(x.row.applicant_name || x.row.name) + '</td>';
                    html += '<td>' + (x.row.national_id
                        ? frappe.utils.escape_html(x.row.national_id)
                        : '<span class="text-danger">' + __('missing') + '</span>') + '</td>';
                    html += '<td>' + frappe.utils.escape_html(x.row.designation || '') + '</td>';
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

            // What actually happened to one candidate. The endpoint no longer
            // raises when COVA rejects a row — it returns COVA's own message —
            // so a failure has a reason worth showing instead of being reduced
            // to a number. A 200 with status "duplicate" means the candidate was
            // already enrolled: not an error, and not a new registration.
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
                    return { state: 'failed', reason: msg.error };
                }
                // The registration and the test submission are reported separately.
                const registration = msg.registration || {};
                const submission = msg.test_submission || {};

                if (registration.status === 'duplicate') {
                    return { state: 'duplicate', reason: registration.message || __('Already enrolled on Cova') };
                }

                // COVA reports a partly-completed registration as an HTTP error
                // that still carries a covaMemberId — the member does exist. The
                // server says so with `registered`, and calling that a failure
                // sends someone off to re-enrol a candidate who is already on the
                // scheme and whose test has already been accepted.
                if (msg.registered === false || (msg.registered === undefined && registration.error)) {
                    return { state: 'failed', reason: registration.error || __('Registration refused') };
                }
                if (submission.error) {
                    return {
                        state: 'failed',
                        reason: __('Registered, but the test request was refused: {0}', [submission.error])
                    };
                }
                if (msg.warning) {
                    return { state: 'warning', reason: msg.warning };
                }
                return { state: 'done' };
            }

            function run_bulk(offers) {
                const total = offers.length;
                let done = 0, duplicates = 0, processed = 0;
                const failures = [];
                // Registered, but something after the member did not complete —
                // counted as done, listed so it is not lost.
                const warnings = [];
                frappe.dom.freeze(__('Processing {0} candidate(s)...', [total]));

                offers.forEach(function(offer_name) {
                    fetch('/api/method/cova_clinic_api', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-Frappe-CSRF-Token': frappe.csrf_token },
                        body: JSON.stringify({ action: 'register_preemployment_candidate', job_offer: offer_name })
                    })
                    .then(function(res) { return res.text(); })
                    .then(function(text) {
                        let data;
                        try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
                        const outcome = read_outcome(data);
                        if (outcome.state === 'done') { done++; }
                        else if (outcome.state === 'duplicate') { duplicates++; }
                        else if (outcome.state === 'warning') {
                            done++;
                            warnings.push({ name: offer_name, reason: outcome.reason });
                        }
                        else { failures.push({ name: offer_name, reason: outcome.reason }); }
                    })
                    .catch(function(err) {
                        failures.push({ name: offer_name, reason: String(err) });
                    })
                    .finally(function() {
                        processed++;
                        if (processed === total) {
                            frappe.dom.unfreeze();
                            let message = __('Registered & requested: {0}', [done]);
                            if (duplicates) {
                                message += '<br>' + __('Already on Cova (skipped): {0}', [duplicates]);
                            }
                            if (warnings.length) {
                                message += '<br>' + __('Registered with a warning: {0}', [warnings.length]);
                            }
                            message += '<br>' + __('Failed: {0}', [failures.length]);
                            if (warnings.length) {
                                message += '<div style="max-height:200px;overflow-y:auto;margin-top:10px;' +
                                    'border:1px solid var(--border-color);border-radius:6px;">' +
                                    '<table class="table table-bordered" style="margin:0;font-size:13px;">' +
                                    '<thead style="position:sticky;top:0;background:var(--fg-color);z-index:1;">' +
                                    '<tr><th>' + __('Job Offer') + '</th><th>' + __('Registered, but') +
                                    '</th></tr></thead><tbody>';
                                warnings.forEach(function(w) {
                                    message += '<tr><td>' + frappe.utils.escape_html(w.name) + '</td><td>' +
                                        frappe.utils.escape_html(String(w.reason)) + '</td></tr>';
                                });
                                message += '</tbody></table></div>';
                            }
                            if (failures.length) {
                                message += '<div style="max-height:320px;overflow-y:auto;margin-top:10px;' +
                                    'border:1px solid var(--border-color);border-radius:6px;">' +
                                    '<table class="table table-bordered" style="margin:0;font-size:13px;">' +
                                    '<thead style="position:sticky;top:0;background:var(--fg-color);z-index:1;">' +
                                    '<tr><th>' + __('Job Offer') + '</th><th>' + __('Why it failed') +
                                    '</th></tr></thead><tbody>';
                                failures.forEach(function(f) {
                                    message += '<tr><td>' + frappe.utils.escape_html(f.name) + '</td><td>' +
                                        frappe.utils.escape_html(String(f.reason || __('Unknown error'))) +
                                        '</td></tr>';
                                });
                                message += '</tbody></table></div>';
                            }
                            frappe.msgprint({
                                title: __('Pre-Employment Complete'),
                                message: message,
                                indicator: failures.length ? 'orange' : 'green'
                            });
                            listview.clear_checked_items();
                            listview.refresh();
                        }
                    });
                });
            }
        }
    });
})();

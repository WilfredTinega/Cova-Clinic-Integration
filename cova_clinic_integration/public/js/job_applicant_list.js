// Copyright (c) 2026, Upande Limited and contributors
// For license information, please see license.txt
// Job Applicant list — bulk COVA pre-employment register + wellness request.
// Ported from the live "Cova medical test" Client Script.

frappe.listview_settings['Job Applicant'] = {
    onload: function(listview) {

        const LOCKED_COMPANY = 'Karen Roses';

        listview.page.add_inner_button(__('Register & Request Wellness'), function() {
            let master_rows = [];
            let all_rows = [];

            const d = new frappe.ui.Dialog({
                title: __('Pre-Employment Wellness'),
                size: 'extra-large',
                fields: [
                    { fieldtype: 'Section Break', label: __('Filters') },
                    { fieldname: 'company', label: __('Company'), fieldtype: 'Data', default: LOCKED_COMPANY, read_only: 1 },
                    { fieldtype: 'Column Break' },
                    { fieldname: 'status', label: __('Status'), fieldtype: 'Select', options: [''] },
                    { fieldtype: 'Column Break' },
                    { fieldname: 'job_title', label: __('Job Opening'), fieldtype: 'Select', options: [''] },
                    { fieldtype: 'Section Break', label: __('Candidates') },
                    { fieldname: 'applicant_html', fieldtype: 'HTML' }
                ],
                primary_action_label: __('Register & Request Selected'),
                primary_action: function() {
                    const checked = [];
                    d.$wrapper.find('.cova-app-check:checked').each(function() {
                        checked.push($(this).data('app'));
                    });
                    if (!checked.length) {
                        frappe.msgprint(__('Select at least one candidate.'));
                        return;
                    }
                    d.hide();
                    run_bulk(checked);
                }
            });

            function load_filter_options() {
                // Load Job Applicants
                const app_promise = new Promise(function(resolve) {
                    frappe.call({
                        method: 'frappe.client.get_list',
                        args: {
                            doctype: 'Job Applicant',
                            filters: [
                                ['custom_company', '=', LOCKED_COMPANY],
                                ['custom_cova_tested', '=', 0]
                            ],
                            fields: ['name', 'applicant_name', 'custom_national_id', 'status', 'job_title', 'custom_cova_registered'],
                            order_by: 'creation desc',
                            limit_page_length: 0
                        },
                        callback: function(r) { resolve(r.message || []); }
                    });
                });

                // Load existing Cova Members (Pre Employment) to exclude
                const cova_promise = new Promise(function(resolve) {
                    frappe.call({
                        method: 'frappe.client.get_list',
                        args: {
                            doctype: 'Cova Members',
                            filters: [['member_type', '=', 'Pre Employment']],
                            fields: ['national_id'],
                            limit_page_length: 0
                        },
                        callback: function(r) {
                            const members = r.message || [];
                            resolve(members.map(function(m) { return m.national_id; }).filter(Boolean));
                        }
                    });
                });

                Promise.all([app_promise, cova_promise]).then(function(results) {
                    var all_applicants = results[0];
                    var cova_national_ids = results[1];

                    // Filter out applicants already in Cova Members
                    if (cova_national_ids.length) {
                        master_rows = all_applicants.filter(function(a) {
                            return !a.custom_national_id || cova_national_ids.indexOf(a.custom_national_id) === -1;
                        });
                    } else {
                        master_rows = all_applicants;
                    }

                    const statuses = [...new Set(master_rows.map(x => x.status).filter(Boolean))].sort();
                    const titles = [...new Set(master_rows.map(x => x.job_title).filter(Boolean))].sort();

                    d.set_df_property('status', 'options', [''].concat(statuses).join('\n'));
                    d.set_df_property('job_title', 'options', [''].concat(titles).join('\n'));

                    load_applicants();
                });
            }

            function load_applicants() {
                const v = d.get_values(true);
                all_rows = master_rows.filter(function(a) {
                    if (v.status && a.status !== v.status) { return false; }
                    if (v.job_title && a.job_title !== v.job_title) { return false; }
                    return true;
                });
                render_table();
            }

            function render_table() {
                let html = '<div style="max-height:380px;overflow-y:auto;border:1px solid var(--border-color);border-radius:6px;">';
                html += '<table class="table table-bordered" style="margin:0;font-size:13px;">';
                html += '<thead style="position:sticky;top:0;background:var(--fg-color);z-index:1;"><tr>';
                html += '<th style="width:40px;"><input type="checkbox" class="cova-select-all"></th>';
                html += '<th>' + __('Name') + '</th><th>' + __('National ID') + '</th><th>' + __('Job Opening') + '</th><th>' + __('Registered') + '</th>';
                html += '</tr></thead><tbody>';

                if (!all_rows.length) {
                    html += '<tr><td colspan="5" class="text-center text-muted">' + __('No candidates found') + '</td></tr>';
                } else {
                    all_rows.forEach(function(a) {
                        const noId = !a.custom_national_id;
                        const reg = a.custom_cova_registered ? '<span class="text-success">&#10003;</span>' : '';
                        html += '<tr>';
                        html += '<td><input type="checkbox" class="cova-app-check" data-app="' + a.name + '"' + (noId ? ' disabled title="No National ID"' : '') + '></td>';
                        html += '<td>' + frappe.utils.escape_html(a.applicant_name || '') + '</td>';
                        html += '<td>' + frappe.utils.escape_html(a.custom_national_id || '<span class="text-danger">missing</span>') + '</td>';
                        html += '<td>' + frappe.utils.escape_html(a.job_title || '') + '</td>';
                        html += '<td class="text-center">' + reg + '</td>';
                        html += '</tr>';
                    });
                }
                html += '</tbody></table></div>';
                html += '<div class="text-muted" style="margin-top:6px;">' + all_rows.length + ' ' + __('candidate(s)') + '</div>';

                d.fields_dict.applicant_html.$wrapper.html(html);

                d.$wrapper.find('.cova-select-all').on('change', function() {
                    d.$wrapper.find('.cova-app-check:not(:disabled)').prop('checked', $(this).prop('checked'));
                });
            }

            ['status', 'job_title'].forEach(function(fn) {
                d.fields_dict[fn].df.onchange = function() { load_applicants(); };
            });

            d.show();
            load_filter_options();

            function run_bulk(applicants) {
                const total = applicants.length;
                let done = 0, failed = 0, processed = 0;
                frappe.dom.freeze(__('Processing {0} candidate(s)...', [total]));

                applicants.forEach(function(app_name) {
                    fetch('/api/method/cova_clinic_api', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-Frappe-CSRF-Token': frappe.csrf_token },
                        body: JSON.stringify({ action: 'register_preemployment_candidate', applicant: app_name })
                    })
                    .then(function(res) { return res.text(); })
                    .then(function(text) {
                        let data;
                        try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
                        const msg = data.message || {};
                        if (msg && !msg.error) { done++; } else { failed++; }
                    })
                    .catch(function() { failed++; })
                    .finally(function() {
                        processed++;
                        if (processed === total) {
                            frappe.dom.unfreeze();
                            frappe.msgprint({
                                title: __('Pre-Employment Complete'),
                                message: __('Registered & requested: {0}<br>Failed: {1}', [done, failed]),
                                indicator: failed ? 'orange' : 'green'
                            });
                            listview.refresh();
                        }
                    });
                });
            }
        }, __('Clinic'));
    }
};
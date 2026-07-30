// Copyright (c) 2026, Upande Limited and contributors
// For license information, please see license.txt
// Employee list — bulk COVA register / request-test / deactivate.
// Ported from the live "Cova" Client Script.

frappe.listview_settings['Employee'] = {
    onload: function(listview) {

        const LOCKED_COMPANY = 'Karen Roses';
        const TEST_PACKAGES = ['Pre Employment Wellness', 'Cholinesterase', 'Food Handler', 'Annual Medical', 'Exit Medical'];

        function open_employee_picker(opts) {
            let all_rows = [];

            const d = new frappe.ui.Dialog({
                title: opts.title,
                size: 'extra-large',
                fields: [
                    { fieldtype: 'Section Break', label: __('Filters') },
                    { fieldname: 'company', label: __('Company'), fieldtype: 'Data', default: LOCKED_COMPANY, read_only: 1 },
                    { fieldtype: 'Column Break' },
                    { fieldname: 'farm', label: __('Farm'), fieldtype: 'Select', options: [''] },
                    { fieldtype: 'Column Break' },
                    { fieldname: 'designation', label: __('Designation'), fieldtype: 'Select', options: [''] },
                    { fieldtype: 'Column Break' },
                    { fieldname: 'department', label: __('Department'), fieldtype: 'Select', options: [''] }
                ].concat(opts.extra_fields || []).concat([
                    { fieldtype: 'Section Break', label: __('Employees') },
                    { fieldname: 'employee_html', fieldtype: 'HTML' }
                ]),
                primary_action_label: opts.action_label,
                primary_action: function() {
                    const checked = [];
                    d.$wrapper.find('.cova-emp-check:checked').each(function() {
                        checked.push($(this).data('emp'));
                    });
                    if (!checked.length) {
                        frappe.msgprint(__('Select at least one employee.'));
                        return;
                    }
                    const values = d.get_values();
                    d.hide();
                    opts.on_submit(checked, values);
                }
            });

            let master_rows = [];
            let cova_member_employees = [];

            function load_filter_options() {
                const status_filter = opts.status_filter || ['=', 'Active'];

                // Load employees
                const emp_promise = new Promise(function(resolve) {
                    frappe.call({
                        method: 'frappe.client.get_list',
                        args: {
                            doctype: 'Employee',
                            filters: [
                                ['company', '=', LOCKED_COMPANY],
                                ['status', status_filter[0], status_filter[1]]
                            ],
                            fields: ['name', 'employee_name', 'employee_number', 'designation', 'department', 'custom_farm'],
                            order_by: 'employee_name asc',
                            limit_page_length: 0
                        },
                        callback: function(r) { resolve(r.message || []); }
                    });
                });

                // Load existing Cova Members (if exclude_cova_members is set)
                const cova_promise = new Promise(function(resolve) {
                    if (!opts.exclude_cova_members) { resolve([]); return; }
                    frappe.call({
                        method: 'frappe.client.get_list',
                        args: {
                            doctype: 'Cova Members',
                            filters: [['member_type', '=', 'Active']],
                            fields: ['employee'],
                            limit_page_length: 0
                        },
                        callback: function(r) {
                            const members = r.message || [];
                            resolve(members.map(function(m) { return m.employee; }).filter(Boolean));
                        }
                    });
                });

                Promise.all([emp_promise, cova_promise]).then(function(results) {
                    var all_employees = results[0];
                    cova_member_employees = results[1];

                    // Filter out employees already in Cova Members
                    if (cova_member_employees.length) {
                        master_rows = all_employees.filter(function(e) {
                            return cova_member_employees.indexOf(e.name) === -1;
                        });
                    } else {
                        master_rows = all_employees;
                    }

                    const farms = [...new Set(master_rows.map(x => x.custom_farm).filter(Boolean))].sort();
                    const desigs = [...new Set(master_rows.map(x => x.designation).filter(Boolean))].sort();
                    const depts = [...new Set(master_rows.map(x => x.department).filter(Boolean))].sort();

                    d.set_df_property('farm', 'options', [''].concat(farms).join('\n'));
                    d.set_df_property('designation', 'options', [''].concat(desigs).join('\n'));
                    d.set_df_property('department', 'options', [''].concat(depts).join('\n'));

                    load_employees();
                });
            }

            function load_employees() {
                const v = d.get_values(true);
                all_rows = master_rows.filter(function(e) {
                    if (v.farm && e.custom_farm !== v.farm) { return false; }
                    if (v.designation && e.designation !== v.designation) { return false; }
                    if (v.department && e.department !== v.department) { return false; }
                    return true;
                });
                render_table();
            }

            function render_table() {
                let html = '<div style="max-height:380px;overflow-y:auto;border:1px solid var(--border-color);border-radius:6px;">';
                html += '<table class="table table-bordered" style="margin:0;font-size:13px;">';
                html += '<thead style="position:sticky;top:0;background:var(--fg-color);z-index:1;">';
                html += '<tr>';
                html += '<th style="width:40px;"><input type="checkbox" class="cova-select-all"></th>';
                html += '<th>' + __('PIN') + '</th><th>' + __('Name') + '</th><th>' + __('Designation') + '</th><th>' + __('Farm') + '</th>';
                html += '</tr></thead><tbody>';

                if (!all_rows.length) {
                    html += '<tr><td colspan="5" class="text-center text-muted">' + __('No employees found') + '</td></tr>';
                } else {
                    all_rows.forEach(function(e) {
                        html += '<tr>';
                        html += '<td><input type="checkbox" class="cova-emp-check" data-emp="' + e.name + '"></td>';
                        html += '<td>' + (e.employee_number || '') + '</td>';
                        html += '<td>' + frappe.utils.escape_html(e.employee_name || '') + '</td>';
                        html += '<td>' + frappe.utils.escape_html(e.designation || '') + '</td>';
                        html += '<td>' + frappe.utils.escape_html(e.custom_farm || '') + '</td>';
                        html += '</tr>';
                    });
                }
                html += '</tbody></table></div>';
                html += '<div class="text-muted" style="margin-top:6px;"><span class="cova-count">' + all_rows.length + '</span> ' + __('employee(s)') + '</div>';

                d.fields_dict.employee_html.$wrapper.html(html);

                d.$wrapper.find('.cova-select-all').on('change', function() {
                    d.$wrapper.find('.cova-emp-check').prop('checked', $(this).prop('checked'));
                });
            }

            ['farm', 'designation', 'department'].forEach(function(fn) {
                d.fields_dict[fn].df.onchange = function() { load_employees(); };
            });

            d.show();
            load_filter_options();
        }

        function run_bulk(employees, build_body, complete_title) {
            const total = employees.length;
            let done = 0, failed = 0, processed = 0;
            frappe.dom.freeze(__('Processing {0} employee(s)...', [total]));

            employees.forEach(function(emp_name) {
                fetch('/api/method/cova_clinic_api', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Frappe-CSRF-Token': frappe.csrf_token },
                    body: JSON.stringify(build_body(emp_name))
                })
                .then(function(res) { return res.text(); })
                .then(function(text) {
                    let data;
                    try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
                    const msg = data.message || {};
                    const inner = msg.deactivation || msg;
                    if (inner && !inner.error) { done++; } else { failed++; }
                })
                .catch(function() { failed++; })
                .finally(function() {
                    processed++;
                    if (processed === total) {
                        frappe.dom.unfreeze();
                        frappe.msgprint({
                            title: complete_title,
                            message: __('Success: {0}<br>Failed: {1}', [done, failed]),
                            indicator: failed ? 'orange' : 'green'
                        });
                        listview.refresh();
                    }
                });
            });
        }

        // ─── REGISTER ───────────────────────────────────────
        listview.page.add_inner_button(__('Register to Cova'), function() {
            open_employee_picker({
                title: __('Register Employees to Cova'),
                action_label: __('Register Selected'),
                exclude_cova_members: true,
                on_submit: function(employees) {
                    run_bulk(employees, function(emp_name) {
                        return { action: 'register_member', member_type: 'Active', employee: emp_name };
                    }, __('Cova Registration Complete'));
                }
            });
        }, __('Clinic'));

        // ─── REQUEST MEDICAL TEST ───────────────────────────
        listview.page.add_inner_button(__('Request Medical Test'), function() {
            open_employee_picker({
                title: __('Request Medical Test'),
                action_label: __('Create & Send'),
                extra_fields: [
                    { fieldtype: 'Section Break', label: __('Test Details') },
                    { fieldname: 'test_package', label: __('Test Package'), fieldtype: 'Select', options: TEST_PACKAGES.join('\n'), reqd: 1 },
                    { fieldtype: 'Column Break' },
                    { fieldname: 'scheduled_from', label: __('Scheduled From'), fieldtype: 'Date', reqd: 1, default: frappe.datetime.get_today() },
                    { fieldtype: 'Column Break' },
                    { fieldname: 'scheduled_to', label: __('Scheduled To'), fieldtype: 'Date', reqd: 1, default: frappe.datetime.add_days(frappe.datetime.get_today(), 15) },
                    { fieldtype: 'Column Break' },
                    { fieldname: 'notes', label: __('Notes'), fieldtype: 'Small Text' }
                ],
                on_submit: function(employees, values) {
                    if (!values.test_package) { frappe.msgprint(__('Select a test package.')); return; }
                    const total = employees.length;
                    let done = 0, failed = 0, processed = 0;
                    frappe.dom.freeze(__('Creating {0} test requests...', [total]));

                    employees.forEach(function(emp_name) {
                        frappe.call({
                            method: 'frappe.client.insert',
                            args: {
                                doc: {
                                    doctype: 'Clinic Test Request',
                                    member_type: 'Active',
                                    employee: emp_name,
                                    status: 'Pending',
                                    test_package: values.test_package,
                                    scheduled_from: values.scheduled_from,
                                    scheduled_to: values.scheduled_to,
                                    notes: values.notes || ''
                                }
                            },
                            callback: function(r) {
                                if (!r.exc && r.message) {
                                    fetch('/api/method/cova_clinic_api', {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json', 'X-Frappe-CSRF-Token': frappe.csrf_token },
                                        body: JSON.stringify({ action: 'submit_test_request', request_name: r.message.name })
                                    })
                                    .then(function(res) { return res.text(); })
                                    .then(function(text) {
                                        let data;
                                        try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
                                        const msg = data.message || {};
                                        if (msg && !msg.error) { done++; } else { failed++; }
                                    })
                                    .catch(function() { failed++; })
                                    .finally(function() { processed++; if (processed === total) { finish(); } });
                                } else {
                                    failed++; processed++; if (processed === total) { finish(); }
                                }
                            }
                        });
                    });

                    function finish() {
                        frappe.dom.unfreeze();
                        frappe.msgprint({
                            title: __('Test Requests Complete'),
                            message: __('Sent: {0}<br>Failed: {1}', [done, failed]),
                            indicator: failed ? 'orange' : 'green'
                        });
                        listview.refresh();
                    }
                }
            });
        }, __('Clinic'));

        // ─── DEACTIVATE ─────────────────────────────────────
        listview.page.add_inner_button(__('Deactivate from Cova'), function() {
            open_employee_picker({
                title: __('Deactivate Employees from Cova'),
                action_label: __('Deactivate Selected'),
                status_filter: ['!=', 'Active'],
                extra_fields: [
                    { fieldtype: 'Section Break', label: __('Exit Details') },
                    { fieldname: 'exit_date', label: __('Exit Date'), fieldtype: 'Date', reqd: 1, default: frappe.datetime.get_today() },
                    { fieldtype: 'Column Break' },
                    { fieldname: 'requires_exit_medical', label: __('Requires Exit Medical'), fieldtype: 'Check', default: 0 }
                ],
                on_submit: function(employees, values) {
                    run_bulk(employees, function(emp_name) {
                        return {
                            action: 'deactivate_member',
                            employee: emp_name,
                            exit_date: values.exit_date,
                            requires_exit_medical: values.requires_exit_medical ? true : false
                        };
                    }, __('Cova Deactivation Complete'));
                }
            });
        }, __('Clinic'));
    }
};
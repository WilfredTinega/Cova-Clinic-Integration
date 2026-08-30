// Copyright (c) 2026, Upande Limited and contributors
// For license information, please see license.txt
// Employee list — bulk COVA register / request-test / deactivate.
// Ported from the live "Cova" Client Script.
//
// ERPNext ships its own frappe.listview_settings["Employee"] (status
// indicators, the default Active filter, add_fields) and this file is
// concatenated after it into the same __list_js blob, so a plain assignment
// would silently drop all of it. Extend the existing object and chain onload.

(function() {
const DOCTYPE = 'Employee';
const prior = frappe.listview_settings[DOCTYPE] || {};
const prior_onload = prior.onload;

frappe.listview_settings[DOCTYPE] = Object.assign({}, prior, {
    onload: function(listview) {
        if (prior_onload) { prior_onload.call(this, listview); }

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
                        // attr(), never data(): jQuery's .data() coerces a
                        // numeric-looking value, so a payroll-style Employee id
                        // such as "101253" comes back as the number 101253. Sent
                        // as JSON that becomes an unquoted integer, and the
                        // server's `name = 101253` against a varchar column makes
                        // MySQL cast every row — which errors on the ids that are
                        // not numeric ("Truncated incorrect DECIMAL value").
                        checked.push(String($(this).attr('data-emp')));
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

        // Reading one row's outcome out of the endpoint's reply.
        //
        // Three things can come back and they used to be flattened into a bare
        // "failed" with no reason: COVA rejecting the row (the server now hands
        // over COVA's own message rather than raising), the server throwing
        // (data.exc), and COVA answering 200 with status "duplicate" — an
        // already-enrolled member, which is not a failure and not a new
        // registration either.
        function read_outcome(data) {
            const msg = (data && data.message) || {};
            const inner = msg.deactivation || msg;

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
                // exc_type alone is a class name — "OperationalError" tells the
                // person reading the report nothing they can act on. Prefer the
                // exception's last line, which carries the actual message.
                let detail = '';
                try {
                    const exc = Array.isArray(data.exc) ? data.exc.join('\n') : (data.exc || '');
                    const lines = String(JSON.parse(exc || '[]').join('\n') || exc)
                        .split('\n').filter(function(l) { return l.trim(); });
                    detail = lines.length ? lines[lines.length - 1].trim() : '';
                } catch (e) {
                    const lines = String(data.exc || '').split('\n')
                        .filter(function(l) { return l.trim(); });
                    detail = lines.length ? lines[lines.length - 1].trim() : '';
                }
                return { state: 'failed', reason: detail || data.exc_type };
            }
            if (inner && inner.error) {
                return { state: 'failed', reason: inner.error };
            }
            if (inner && inner.status === 'duplicate') {
                return { state: 'duplicate', reason: inner.message || __('Already enrolled on Cova') };
            }
            return { state: 'done' };
        }

        // Counts plus a table of what went wrong, so a 200-row sweep does not
        // end in "Failed: 37" with nowhere to go next.
        function report_bulk(title, done, duplicates, failures) {
            let message = __('Succeeded: {0}', [done]);
            if (duplicates) {
                message += '<br>' + __('Already on Cova (skipped): {0}', [duplicates]);
            }
            message += '<br>' + __('Failed: {0}', [failures.length]);

            if (failures.length) {
                message += '<div style="max-height:320px;overflow-y:auto;margin-top:10px;' +
                    'border:1px solid var(--border-color);border-radius:6px;">' +
                    '<table class="table table-bordered" style="margin:0;font-size:13px;">' +
                    '<thead style="position:sticky;top:0;background:var(--fg-color);z-index:1;"><tr><th>' +
                    __('Record') + '</th><th>' + __('Why it failed') + '</th></tr></thead><tbody>';
                failures.forEach(function(f) {
                    message += '<tr><td>' + frappe.utils.escape_html(f.name) + '</td><td>' +
                        frappe.utils.escape_html(String(f.reason || __('Unknown error'))) + '</td></tr>';
                });
                message += '</tbody></table></div>';
            }

            frappe.msgprint({
                title: title,
                message: message,
                indicator: failures.length ? 'orange' : 'green'
            });
        }

        // A sweep of 200 employees used to open 200 sockets at once, and the
        // browser drops the overflow as "TypeError: Failed to fetch" — rows that
        // never reached the server at all, reported as if COVA had refused them.
        // Each row is one outbound call to COVA, so a small pool is also kinder
        // to the far end. Runs in order, a few at a time, with live progress.
        const BULK_CONCURRENCY = 4;

        function run_pool(items, worker, complete_title) {
            const total = items.length;
            let done = 0, duplicates = 0, processed = 0, cursor = 0;
            const failures = [];

            function progress() {
                frappe.dom.freeze(__('Processing {0} of {1}...', [processed, total]));
            }
            progress();

            function record(item, outcome) {
                if (outcome.state === 'done') { done++; }
                else if (outcome.state === 'duplicate') { duplicates++; }
                else { failures.push({ name: item, reason: outcome.reason }); }
            }

            function finish_one(item, outcome) {
                record(item, outcome);
                processed++;
                progress();
                if (processed === total) {
                    frappe.dom.unfreeze();
                    report_bulk(complete_title, done, duplicates, failures);
                    listview.refresh();
                    return;
                }
                pump();
            }

            function pump() {
                if (cursor >= total) { return; }
                const item = items[cursor++];
                worker(item)
                    .then(function(outcome) { finish_one(item, outcome); })
                    .catch(function(err) {
                        finish_one(item, { state: 'failed', reason: String(err && err.message || err) });
                    });
            }

            if (!total) {
                frappe.dom.unfreeze();
                return;
            }
            for (let i = 0; i < Math.min(BULK_CONCURRENCY, total); i++) { pump(); }
        }

        function post_action(body) {
            return fetch('/api/method/cova_clinic_api', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Frappe-CSRF-Token': frappe.csrf_token },
                body: JSON.stringify(body)
            })
            .then(function(res) { return res.text(); })
            .then(function(text) {
                let data;
                try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
                return read_outcome(data);
            });
        }

        function run_bulk(employees, build_body, complete_title) {
            run_pool(employees, function(emp_name) {
                return post_action(build_body(emp_name));
            }, complete_title);
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

                    run_pool(employees, function(emp_name) {
                        return new Promise(function(resolve, reject) {
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
                                    if (r.exc || !r.message) {
                                        // The Clinic Test Request itself would not save — report
                                        // that, since nothing reached COVA at all.
                                        resolve({
                                            state: 'failed',
                                            reason: (r.exc_type || __('Could not create the Clinic Test Request'))
                                        });
                                        return;
                                    }
                                    post_action({
                                        action: 'submit_test_request',
                                        request_name: r.message.name
                                    }).then(resolve, reject);
                                },
                                error: function(r) {
                                    resolve({
                                        state: 'failed',
                                        reason: (r && r.exc_type) || __('Could not create the Clinic Test Request')
                                    });
                                }
                            });
                        });
                    }, __('Test Requests Complete'));
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
});
})();
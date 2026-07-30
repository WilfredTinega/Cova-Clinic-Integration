# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

# insert_after is used only for positioning; it is NOT a DocField attribute.
_JA_DEPENDS = 'eval:doc.custom_company==="Karen Roses"'

CLINIC_FIELDS = {
	"Job Applicant": [
		# The Company that gates the COVA pre-employment block below.
		{
			"fieldname": "custom_company",
			"label": "Company",
			"fieldtype": "Link",
			"options": "Company",
			"insert_after": "details_section",
			"reqd": 1,
			"in_list_view": 1,
			"in_standard_filter": 1,
		},
		# COVA pre-employment biodata + tracking block.
		{
			"fieldname": "custom_column_break_1ke4q",
			"fieldtype": "Column Break",
			"insert_after": "status",
		},
		{
			"fieldname": "custom_national_id",
			"label": "National ID",
			"fieldtype": "Data",
			"insert_after": "custom_column_break_1ke4q",
			"depends_on": _JA_DEPENDS,
		},
		{
			"fieldname": "custom_date_of_birth",
			"label": "Date of Birth",
			"fieldtype": "Date",
			"insert_after": "custom_national_id",
			"depends_on": _JA_DEPENDS,
		},
		{
			"fieldname": "custom_gender",
			"label": "Gender",
			"fieldtype": "Select",
			"options": "\nMale\nFemale",
			"insert_after": "custom_date_of_birth",
			"depends_on": _JA_DEPENDS,
		},
		{
			"fieldname": "custom_cova_registered",
			"label": "Cova Registered?",
			"fieldtype": "Check",
			"insert_after": "custom_gender",
			"depends_on": _JA_DEPENDS,
		},
		{
			"fieldname": "custom_cova_tested",
			"label": "Cova Tested?",
			"fieldtype": "Check",
			"insert_after": "custom_cova_registered",
			"depends_on": _JA_DEPENDS,
		},
		{
			"fieldname": "custom_linked_test_result",
			"label": "Linked Test Result",
			"fieldtype": "Link",
			"options": "Clinic Test Result",
			"insert_after": "custom_cova_tested",
			"depends_on": _JA_DEPENDS,
		},
	],
	"Employee": [
		{
			"fieldname": "cova_member_id",
			"label": "Cova Member ID",
			"fieldtype": "Data",
			"insert_after": "national_id",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "cova_deactivated",
			"label": "Cova Deactivated",
			"fieldtype": "Check",
			"insert_after": "cova_member_id",
			"default": "0",
			"no_copy": 1,
		},
	],
}


def _save_standard_doctype(dt):
	prev_in_import = frappe.flags.in_import
	frappe.flags.in_import = True
	try:
		dt.flags.ignore_permissions = True
		dt.save()
	finally:
		frappe.flags.in_import = prev_in_import


def _position(dt, row, insert_after):
	"""Move an appended field row to sit right after `insert_after`, if that
	anchor is one of the doctype's own DocFields (custom-field anchors on the
	parent are not in dt.fields, so the row simply stays at the end)."""
	if not insert_after:
		return
	names = [f.fieldname for f in dt.fields]
	if insert_after in names:
		dt.fields.remove(row)
		dt.fields.insert(names.index(insert_after) + 1, row)


def install_clinic_fields():
	"""Create the integration's fields as normal DocFields (idempotent).

	If a field already exists as a Custom Field record, it is converted: the
	Custom Field record is deleted and the same fieldname is re-added as a
	normal DocField. The physical column is left in place across the swap, so
	existing data is preserved."""
	if not frappe.conf.get("developer_mode"):
		frappe.log_error(
			title="COVA install_clinic_fields skipped",
			message="developer_mode is off; cannot add normal fields to standard doctypes.",
		)
		return

	for doctype, specs in CLINIC_FIELDS.items():
		fieldnames = [s["fieldname"] for s in specs]

		# Convert any existing Custom Field versions to normal fields: drop the
		# Custom Field record first (the DB column stays, keeping the data).
		for cf in frappe.get_all(
			"Custom Field",
			filters={"dt": doctype, "fieldname": ["in", fieldnames]},
			pluck="name",
		):
			frappe.delete_doc("Custom Field", cf, ignore_permissions=True, force=True)
		frappe.clear_cache(doctype=doctype)

		# Whatever is already a normal DocField is left untouched.
		normal_fields = {f.fieldname for f in frappe.get_doc("DocType", doctype).fields}
		to_add = [s for s in specs if s["fieldname"] not in normal_fields]
		if not to_add:
			continue

		dt = frappe.get_doc("DocType", doctype)
		for spec in to_add:
			field = {k: v for k, v in spec.items() if k != "insert_after"}
			row = dt.append("fields", field)
			_position(dt, row, spec.get("insert_after"))

		for i, f in enumerate(dt.fields):
			f.idx = i + 1

		_save_standard_doctype(dt)
		frappe.clear_cache(doctype=doctype)

	frappe.db.commit()


def uninstall_clinic_fields():
	"""Remove the integration's fields (and their columns) on uninstall."""
	if not frappe.conf.get("developer_mode"):
		return

	for doctype, specs in CLINIC_FIELDS.items():
		fieldnames = {s["fieldname"] for s in specs}
		dt = frappe.get_doc("DocType", doctype)
		kept = [f for f in dt.fields if f.fieldname not in fieldnames]
		if len(kept) != len(dt.fields):
			dt.set("fields", kept)
			for i, f in enumerate(dt.fields):
				f.idx = i + 1
			_save_standard_doctype(dt)
			frappe.clear_cache(doctype=doctype)

		# Drop the physical columns for a clean uninstall.
		table_cols = set(frappe.db.get_table_columns(doctype))
		for fn in fieldnames:
			if fn in table_cols:
				try:
					frappe.db.sql_ddl(
						"ALTER TABLE `tab{0}` DROP COLUMN `{1}`".format(doctype, fn)
					)
				except Exception:
					frappe.log_error(
						title="COVA uninstall drop column",
						message=frappe.get_traceback(),
					)

	frappe.db.commit()

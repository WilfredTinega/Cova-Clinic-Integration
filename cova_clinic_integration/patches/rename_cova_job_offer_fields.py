# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

"""Drop the `custom_` prefix from the COVA fields on Job Offer.

The app installs its Job Offer block as normal DocFields on the standard
doctype, not as Custom Fields — so the `custom_` prefix was misleading: it named
them after a mechanism the app does not use. They are now plain fieldnames
(`national_id`, `phone_number`, `cova_registered`, …), matching how the Employee
side (`cova_member_id`, `cova_deactivated`) was always named.

Existing sites carry data in the old columns, so this patch copies it across
rather than letting `install_clinic_fields()` create empty fields beside it:

    1. strip the old DocField / Custom Field rows (Frappe leaves the columns
       behind, which is exactly what we want here);
    2. let install_clinic_fields() create the new fields and their columns;
    3. copy every non-empty old value into the new column, without overwriting
       anything already recorded there;
    4. drop the old columns.

Idempotent: with nothing named `custom_*` left on Job Offer it returns at once.
"""

import frappe

from cova_clinic_integration.setup import (
	_save_standard_doctype,
	install_clinic_fields,
)

# old fieldname -> new fieldname
RENAMED_FIELDS = {
	"custom_cova_section": "cova_section",
	"custom_national_id": "national_id",
	"custom_phone_number": "phone_number",
	"custom_column_break_1ke4q": "cova_column_break_1",
	"custom_date_of_birth": "date_of_birth",
	"custom_gender": "gender",
	"custom_column_break_cova2": "cova_column_break_2",
	"custom_cova_registered": "cova_registered",
	"custom_column_break_cova3": "cova_column_break_3",
	"custom_cova_tested": "cova_tested",
	"custom_linked_test_result": "linked_test_result",
}

# Layout-only fields: no column, nothing to carry over.
NO_DATA = {
	"custom_cova_section",
	"custom_column_break_1ke4q",
	"custom_column_break_cova2",
	"custom_column_break_cova3",
}

DOCTYPE = "Job Offer"


def execute():
	if not frappe.db.table_exists(DOCTYPE):
		return

	old_columns = {c for c in frappe.db.get_table_columns(DOCTYPE) if c in RENAMED_FIELDS}
	old_fields = set(
		frappe.db.get_all(
			"DocField",
			filters={"parent": DOCTYPE, "fieldname": ["in", list(RENAMED_FIELDS)]},
			pluck="fieldname",
		)
	)
	old_custom_fields = set(
		frappe.get_all(
			"Custom Field",
			filters={"dt": DOCTYPE, "fieldname": ["in", list(RENAMED_FIELDS)]},
			pluck="fieldname",
		)
	)
	if not (old_columns or old_fields or old_custom_fields):
		return

	_strip_old_fields(old_fields, old_custom_fields)

	# Recreate the block under the new names. Runs here rather than waiting for
	# the after_migrate hook, because the data copy below needs the columns.
	install_clinic_fields()

	new_columns = set(frappe.db.get_table_columns(DOCTYPE))
	copied = _copy_values(old_columns, new_columns)
	_drop_old_columns(old_columns, new_columns)

	frappe.clear_cache(doctype=DOCTYPE)
	frappe.db.commit()  # nosemgrep - schema changes must land before the next patch

	if copied:
		frappe.logger().info("cova_clinic_integration renamed Job Offer fields: %s" % copied)


def _strip_old_fields(old_fields, old_custom_fields):
	"""Remove the old rows. Frappe does not drop the columns for a removed
	DocField, so the data survives for the copy below."""
	if old_fields:
		dt = frappe.get_doc("DocType", DOCTYPE)
		kept = [f for f in dt.fields if f.fieldname not in old_fields]
		if len(kept) != len(dt.fields):
			dt.set("fields", kept)
			for i, f in enumerate(dt.fields):
				f.idx = i + 1
			_save_standard_doctype(dt)

	for fieldname in old_custom_fields:
		name = frappe.db.get_value("Custom Field", {"dt": DOCTYPE, "fieldname": fieldname})
		if name:
			frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)

	frappe.clear_cache(doctype=DOCTYPE)


def _is_empty(column, fieldtype):
	"""SQL for "this column holds nothing", typed per fieldtype.

	A blanket `col = '' or col = 0` cannot be used: comparing a Date or an Int
	column against '' makes MariaDB cast, and in strict mode that is a hard
	error ("Truncated incorrect DECIMAL value").
	"""
	if fieldtype in ("Check", "Int", "Float", "Currency", "Percent"):
		return f"(`{column}` is null or `{column}` = 0)"
	if fieldtype in ("Date", "Datetime", "Time"):
		return f"`{column}` is null"
	return f"(`{column}` is null or `{column}` = '')"


def _copy_values(old_columns, new_columns):
	"""Carry every non-empty old value into its new column, never overwriting a
	value already sitting in the new one."""
	from cova_clinic_integration.setup import CLINIC_FIELDS

	fieldtypes = {s["fieldname"]: s["fieldtype"] for s in CLINIC_FIELDS[DOCTYPE]}

	copied = []
	for old, new in RENAMED_FIELDS.items():
		if old in NO_DATA or old not in old_columns or new not in new_columns:
			continue
		fieldtype = fieldtypes.get(new, "Data")
		frappe.db.sql(
			"""
			update `tab{doctype}`
			set `{new}` = `{old}`
			where {new_empty} and not {old_empty}
			""".format(
				doctype=DOCTYPE,
				new=new,
				old=old,
				new_empty=_is_empty(new, fieldtype),
				old_empty=_is_empty(old, fieldtype),
			)
		)
		copied.append(f"{old} -> {new}")
	return copied


def _drop_old_columns(old_columns, new_columns):
	for old, new in RENAMED_FIELDS.items():
		if old not in old_columns:
			continue
		# Never drop the old column unless its replacement is really there.
		if old not in NO_DATA and new not in new_columns:
			frappe.log_error(
				title="COVA rename kept old Job Offer column",
				message=f"{new} was not created, so `{old}` is left in place with its data.",
			)
			continue
		try:
			frappe.db.sql_ddl(f"ALTER TABLE `tab{DOCTYPE}` DROP COLUMN `{old}`")
		except Exception:
			frappe.log_error(
				title="COVA drop old Job Offer column",
				message=frappe.get_traceback(),
			)

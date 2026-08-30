# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

"""Move the COVA pre-employment block from Job Applicant to Job Offer.

Earlier versions of this app installed the biodata + tracking fields on Job
Applicant. They now live on Job Offer (which already carries a standard
`company` field, so the old `custom_company` copy is dropped rather than
moved). This patch copies whatever data exists onto the matching Job Offers,
then removes the fields and their columns from Job Applicant."""

import frappe

from cova_clinic_integration.setup import (
	_save_standard_doctype,
	install_clinic_fields,
)

# Legacy Job Applicant column -> the Job Offer fieldname it lands in. The
# source names keep their `custom_` prefix (that is how the old version created
# them); the destinations are the plain fieldnames the app installs today.
MOVED_FIELDS = {
	"custom_national_id": "national_id",
	"custom_date_of_birth": "date_of_birth",
	"custom_gender": "gender",
	"custom_cova_registered": "cova_registered",
	"custom_cova_tested": "cova_tested",
	"custom_linked_test_result": "linked_test_result",
}

# Removed from Job Applicant without a destination: `custom_company` is
# redundant on Job Offer, and the column break was pure layout.
DROPPED_FIELDS = ["custom_company", "custom_column_break_1ke4q"]


def execute():
	if not frappe.db.table_exists("Job Applicant"):
		return

	ja_columns = set(frappe.db.get_table_columns("Job Applicant"))
	if not any(fn in ja_columns for fn in list(MOVED_FIELDS) + DROPPED_FIELDS):
		return

	# The Job Offer fields are created by an after_migrate hook that has not run
	# yet at patch time, so create them now.
	install_clinic_fields()

	jo_columns = set(frappe.db.get_table_columns("Job Offer"))
	movable = {
		source: target
		for source, target in MOVED_FIELDS.items()
		if source in ja_columns and target in jo_columns
	}

	if len(movable) < len([fn for fn in MOVED_FIELDS if fn in ja_columns]):
		# Job Offer did not get every field (developer_mode off, most likely).
		# Leave Job Applicant alone rather than dropping data with nowhere to go.
		frappe.log_error(
			title="COVA move to Job Offer skipped",
			message="Job Offer is missing columns %s; Job Applicant fields left in place."
			% sorted(set(MOVED_FIELDS.values()) - set(movable.values())),
		)
		return

	_copy_to_job_offers(movable)
	_strip_job_applicant(ja_columns)
	frappe.db.commit()


def _copy_to_job_offers(field_map):
	"""Fill each Job Offer's empty COVA fields from its linked Job Applicant.

	`field_map` is {Job Applicant column: Job Offer fieldname}.
	"""
	cols = ", ".join("ja.`%s`" % fn for fn in field_map)
	rows = frappe.db.sql(
		"""
		select jo.name as offer, {cols}
		from `tabJob Offer` jo
		inner join `tabJob Applicant` ja on ja.name = jo.job_applicant
		where jo.docstatus < 2
		""".format(cols=cols),
		as_dict=True,
	)

	targets = list(field_map.values())
	for row in rows:
		values = {
			target: row.get(source)
			for source, target in field_map.items()
			if row.get(source) not in (None, "", 0)
		}
		if not values:
			continue
		current = frappe.db.get_value("Job Offer", row.offer, targets, as_dict=True) or {}
		# Never overwrite something already recorded against the offer.
		values = {fn: v for fn, v in values.items() if current.get(fn) in (None, "", 0)}
		if values:
			frappe.db.set_value("Job Offer", row.offer, values, update_modified=False)


def _strip_job_applicant(ja_columns):
	stale = set(MOVED_FIELDS) | set(DROPPED_FIELDS)

	dt = frappe.get_doc("DocType", "Job Applicant")
	kept = [f for f in dt.fields if f.fieldname not in stale]
	if len(kept) != len(dt.fields):
		dt.set("fields", kept)
		for i, f in enumerate(dt.fields):
			f.idx = i + 1
		_save_standard_doctype(dt)

	for cf in frappe.get_all(
		"Custom Field",
		filters={"dt": "Job Applicant", "fieldname": ["in", list(stale)]},
		pluck="name",
	):
		frappe.delete_doc("Custom Field", cf, ignore_permissions=True, force=True)

	for fn in stale:
		if fn not in ja_columns:
			continue
		try:
			frappe.db.sql_ddl("ALTER TABLE `tabJob Applicant` DROP COLUMN `%s`" % fn)
		except Exception:
			frappe.log_error(
				title="COVA drop Job Applicant column",
				message=frappe.get_traceback(),
			)

	frappe.clear_cache(doctype="Job Applicant")

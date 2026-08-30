# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt
"""Fold Clinic Checkin's second employee field into the first.

Clinic Checkin used to name its person in one of two columns: ``employee`` for
sick-off records and ``b_employee`` for door punches. Which column was filled
was what told the two payloads apart — so a row written into the wrong one
silently changed meaning. On the live site an import put 216 sick-off records
into ``b_employee``, and because the controller only raises a Sick Leave
Application when ``employee`` is set, not one of them ever produced any leave.

Both payloads now use ``employee``, and what distinguishes them is the payload
itself: a punch carries ``time`` (and usually ``log_type``), a sick-off carries
``start_date`` and ``end_date``.

The copy is a direct UPDATE rather than a document save. Saving would fire the
controller and raise a Leave Application for every backfilled sick-off at once —
hundreds of them, backdated. Whether those should exist is a decision for
whoever owns the data, not a side effect of a schema change.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Clinic Checkin"):
		return
	if not frappe.db.has_column("Clinic Checkin", "b_employee"):
		return  # already merged

	moved = frappe.db.sql(
		"""UPDATE `tabClinic Checkin`
		   SET employee = b_employee
		   WHERE (employee IS NULL OR employee = '')
		     AND b_employee IS NOT NULL AND b_employee != ''"""
	)

	# Rows holding a different employee in each column would lose one of them, so
	# they are left alone and reported rather than guessed at.
	conflicts = frappe.db.sql(
		"""SELECT name, employee, b_employee FROM `tabClinic Checkin`
		   WHERE employee IS NOT NULL AND employee != ''
		     AND b_employee IS NOT NULL AND b_employee != ''
		     AND employee != b_employee""",
		as_dict=True,
	)
	if conflicts:
		frappe.log_error(
			title="COVA checkin merge: conflicting employees",
			message=frappe.as_json(conflicts),
		)

	frappe.db.commit()

	# The column goes only once nothing is left in it that is not already copied.
	stranded = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabClinic Checkin`
		   WHERE b_employee IS NOT NULL AND b_employee != ''
		     AND (employee IS NULL OR employee = '' OR employee != b_employee)"""
	)[0][0]
	if stranded:
		frappe.log_error(
			title="COVA checkin merge: column kept",
			message=f"{stranded} row(s) still hold an unmerged b_employee; column not dropped.",
		)
		return

	frappe.db.sql("ALTER TABLE `tabClinic Checkin` DROP COLUMN `b_employee`")
	frappe.db.commit()
	frappe.clear_cache(doctype="Clinic Checkin")

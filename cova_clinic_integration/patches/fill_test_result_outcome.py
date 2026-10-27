"""Show each existing Test Result row with its clinical outcome.

The table used to show only a risk band derived from the outcome, which read as
a second, different verdict. Rows now carry the outcome itself; fill it from the
parent result for every row received before the column existed.
"""

import frappe

OUTCOMES = ("FitForWork", "FitWithRestrictions", "InconclusiveRetestRequired", "UnfitForWork")


def execute():
	frappe.db.sql(
		"""UPDATE `tabTest Result` tr
		JOIN `tabClinic Test Result` ctr ON ctr.name = tr.parent
		SET tr.outcome = ctr.clinical_outcome
		WHERE tr.parenttype = 'Clinic Test Result'
		AND (tr.outcome IS NULL OR tr.outcome = '')
		AND ctr.clinical_outcome IN %(outcomes)s""",
		{"outcomes": OUTCOMES},
	)

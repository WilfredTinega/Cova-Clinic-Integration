# First aiders are marked on the Employee itself (is_first_aider); the short-lived
# First Aider doctype is removed. Work Accident.first_aider now links to Employee,
# so any value pointing at a First Aider record (named by employee) stays valid.

import frappe


def execute():
	# The Employee fields are installed after migrate; this needs them now.
	from cova_clinic_integration.setup import install_clinic_fields

	install_clinic_fields()
	if frappe.db.exists("Onboarding Step", "Keep the First Aider List"):
		frappe.delete_doc("Onboarding Step", "Keep the First Aider List", ignore_permissions=True, force=True)
	if frappe.db.exists("DocType", "First Aider"):
		for row in frappe.get_all("First Aider", fields=["employee", "certified_until"]):
			frappe.db.set_value(
				"Employee",
				row.employee,
				{
					"is_first_aider": 1,
					"first_aid_certified_until": row.certified_until,
				},
				update_modified=False,
			)
		frappe.delete_doc("DocType", "First Aider", ignore_permissions=True, force=True)

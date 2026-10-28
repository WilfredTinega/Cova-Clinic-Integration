"""One row per clinic exchange in Clinic API Log.

Every call the clinic makes (``getClinicData``, ``cova_clinic_api``) and every
sick leave the integration files lands here as Success, Warning or Error, with
the request, the reply and the document it touched — so HR can see what the
clinic sent and what became of it without reading the Error Log.
"""

import json

import frappe

SUCCESS, WARNING, ERROR = "Success", "Warning", "Error"


def _as_json(value):
	if value is None or isinstance(value, str):
		return value
	return json.dumps(value, indent=2, default=str)


def log_clinic(
	action,
	status,
	message=None,
	request=None,
	response=None,
	employee=None,
	reference_doctype=None,
	reference_name=None,
):
	"""Write one Clinic API Log row. Never raises: logging must not fail a call."""
	try:
		if employee and not frappe.db.exists("Employee", employee):
			employee = None
		frappe.get_doc(
			{
				"doctype": "Clinic API Log",
				"action": str(action or "unknown")[:140],
				"status": status,
				"message": message,
				"request": _as_json(request),
				"response": _as_json(response),
				"employee": employee,
				"reference_doctype": reference_doctype if reference_name else None,
				"reference_name": reference_name,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Clinic API Log write failed", message=frappe.get_traceback())

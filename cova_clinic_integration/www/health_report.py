# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt
"""Controller for the /health-report portal page.

The page is the website counterpart of the "Health Report" Custom HTML Block
that lived on the desk. It is restricted to HR roles — see
``cova_clinic_integration.api.has_health_report_access`` for the exact rule —
and its data comes from POST /api/method/clinic_disease_report, which enforces
the same check so the page cannot be bypassed by calling the endpoint directly.
"""

import frappe
from frappe import _

from cova_clinic_integration.api import allowed_dashboard_sections, has_health_report_access

no_cache = 1


def get_context(context):
	if not has_health_report_access():
		frappe.throw(
			_("You are not permitted to view the Health Report."),
			frappe.PermissionError,
		)

	context.no_cache = 1
	context.show_sidebar = False
	# The report is a wide grid (one column per month plus two charts side by
	# side); the default portal container crops it, so take the full width and
	# let the page's own max-width keep it readable.
	context.full_width = 1
	context.title = _("Clinic Analytics")

	# The desk lives at /app before v17 and /desk from v17 on, so the way back
	# is derived rather than written out.
	context.desk_url = _desk_url()
	context.allowed_sections = allowed_dashboard_sections()

	# Who is looking at this, shown at the foot of the rail.
	user = frappe.session.user
	full_name = frappe.utils.get_fullname(user) or user
	context.viewer = {
		"id": user,
		"full_name": full_name,
		"initials": _initials(full_name),
	}
	return context


def _desk_url():
	"""Root of the desk for this Frappe version, as a site-relative path."""
	from urllib.parse import urlsplit

	# get_url_to_form() with no name returns the doctype's list route; its first
	# segment is the desk prefix, whichever version this is running on.
	path = urlsplit(frappe.utils.get_url_to_list("Clinic Visit Cost")).path
	return "/" + path.strip("/").split("/")[0]


def _initials(name):
	parts = [p for p in (name or "").split() if p]
	if not parts:
		return "?"
	if len(parts) == 1:
		return parts[0][:2].upper()
	return (parts[0][0] + parts[-1][0]).upper()

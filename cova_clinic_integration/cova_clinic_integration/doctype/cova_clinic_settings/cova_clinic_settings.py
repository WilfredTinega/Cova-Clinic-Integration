# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# What the statutory run used to carry in code. Seeded into Test Scheduling on
# migrate while that table is empty, and only for designations the site has.
DEFAULT_TEST_GROUPS = {
	"Cholinesterase": {
		"test_package": "Annual Medical",
		"designations": [
			"Sprayer",
			"Spray Pump Operator",
			"Spray Applicator",
			"Spray Supervisor",
			"Crop Protection Section Head",
			"Scouter",
		],
	},
	"Food Handler": {
		"test_package": "Annual Medical",
		"designations": [
			"Chef",
			"Cook / Cleaner",
			"Directors Cook / Cleaner",
			"Hospitality",
			"Cleaner/Feeder",
			"Feeder",
			"Milker",
			"Dairy Assistant",
			"Cold Room Attendant",
		],
	},
}


class CovaClinicSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from cova_clinic_integration.cova_clinic_integration.doctype.clinic_dashboard_viewer.clinic_dashboard_viewer import (
			ClinicDashboardViewer,
		)
		from cova_clinic_integration.cova_clinic_integration.doctype.clinic_test_group.clinic_test_group import (
			ClinicTestGroup,
		)

		access_token: DF.Text | None
		api_key: DF.Data | None
		base_url: DF.Data | None
		company: DF.Link | None
		dashboard_viewers: DF.Table[ClinicDashboardViewer]
		deactivation_endpoint: DF.Data | None
		pre_employement_endpoint: DF.Data | None
		register_endpoint: DF.Data | None
		test_groups: DF.Table[ClinicTestGroup]
		test_request_endpoint: DF.Data | None
	# end: auto-generated types

	def validate(self):
		# A group is scheduled as one batch of one package; two packages under
		# one name would silently schedule only the first.
		packages = {}
		for row in self.test_groups:
			row.group_name = (row.group_name or "").strip()
			first = packages.setdefault(row.group_name, row.test_package)
			if first != row.test_package:
				frappe.throw(
					_("Test Scheduling row {0}: group {1} already uses package {2}.").format(
						row.idx, frappe.bold(row.group_name), frappe.bold(first)
					)
				)


def get_clinic_company() -> str | None:
	"""The Company the COVA integration is configured for, or None when one has
	not been picked yet. Read with get_single_value so callers do not need read
	permission on the settings."""
	return frappe.db.get_single_value("Cova Clinic Settings", "company") or None


# The sections of the Clinic Analytics page, in rail order. Each is a tick box
# (``view_<key>``) on a Dashboard Viewers row.
DASHBOARD_SECTIONS = (
	"overview",
	"health",
	"biometric",
	"sickoff",
	"visits",
	"requests",
	"results",
	"tickets",
	"schedules",
	"accidents",
)


def get_dashboard_viewers() -> dict[str, set[str]]:
	"""Users listed under Dashboard Access, each with the sections ticked for
	them. Empty means nobody has been listed."""
	rows = frappe.get_all(
		"Clinic Dashboard Viewer",
		filters={"parenttype": "Cova Clinic Settings", "parentfield": "dashboard_viewers"},
		fields=["user"] + ["view_" + s for s in DASHBOARD_SECTIONS],
	)
	viewers = {}
	for r in rows:
		viewers.setdefault(r.user, set()).update(s for s in DASHBOARD_SECTIONS if r.get("view_" + s))
	return viewers


def get_test_groups() -> dict:
	"""Enabled Test Scheduling rows folded into groups:
	``{name: {test_package, departments, designations, employees_per_designation}}``.
	An empty departments (or designations) list matches every one."""
	groups = {}
	rows = frappe.get_all(
		"Clinic Test Group",
		filters={"parenttype": "Cova Clinic Settings", "parentfield": "test_groups", "enabled": 1},
		fields=["group_name", "test_package", "department", "designation", "employees_per_designation"],
		order_by="idx asc",
	)
	for r in rows:
		g = groups.setdefault(
			r.group_name,
			{
				"test_package": r.test_package,
				"departments": [],
				"designations": [],
				"employees_per_designation": 0,
			},
		)
		if r.department and r.department not in g["departments"]:
			g["departments"].append(r.department)
		if r.designation and r.designation not in g["designations"]:
			g["designations"].append(r.designation)
		g["employees_per_designation"] = max(
			g["employees_per_designation"], int(r.employees_per_designation or 0)
		)
	return groups

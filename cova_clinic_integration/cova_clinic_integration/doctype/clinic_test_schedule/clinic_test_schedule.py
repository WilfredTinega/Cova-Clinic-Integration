# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import random

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from cova_clinic_integration.cova_clinic_integration.doctype.cova_clinic_settings.cova_clinic_settings import (
	get_clinic_company,
	get_test_groups,
)


def group_employees(
	company,
	departments=None,
	designations=None,
	package=None,
	already_from=None,
	already_to=None,
	leave_window=None,
	on_leave=False,
):
	"""Active employees of ``company`` in the given departments / designations
	(an empty list matches any).

	* ``package`` with ``already_from`` / ``already_to``: leave out anyone with a
	  live test request for that package scheduled in that range.
	* ``leave_window`` (from, to): leave out anyone on approved leave for any
	  part of it — or, with ``on_leave=True``, return only them.
	"""
	clauses = ["e.status = 'Active'", "e.company = %(company)s"]
	params = {"company": company}
	if departments:
		clauses.append("e.department IN %(departments)s")
		params["departments"] = tuple(departments)
	if designations:
		clauses.append("e.designation IN %(designations)s")
		params["designations"] = tuple(designations)
	if package:
		clauses.append(
			"NOT EXISTS (SELECT 1 FROM `tabClinic Test Request` tr "
			"WHERE tr.employee = e.name AND tr.test_package = %(package)s "
			"AND tr.scheduled_from BETWEEN %(already_from)s AND %(already_to)s "
			"AND COALESCE(tr.status, '') != 'Cancelled')"
		)
		params.update({"package": package, "already_from": already_from, "already_to": already_to})
	if leave_window:
		leave = (
			"EXISTS (SELECT 1 FROM `tabLeave Application` la WHERE la.employee = e.name "
			"AND la.docstatus = 1 AND la.status = 'Approved' "
			"AND la.from_date <= %(window_to)s AND la.to_date >= %(window_from)s)"
		)
		clauses.append(leave if on_leave else "NOT " + leave)
		params.update({"window_from": leave_window[0], "window_to": leave_window[1]})
	return frappe.db.sql(
		"SELECT e.name, e.employee_name, e.department, e.designation "
		"FROM `tabEmployee` e WHERE " + " AND ".join(clauses) + " ORDER BY e.designation, e.employee_name",
		params,
		as_dict=True,
	)


class ClinicTestSchedule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from cova_clinic_integration.cova_clinic_integration.doctype.clinic_schedule_department.clinic_schedule_department import (
			ClinicScheduleDepartment,
		)
		from cova_clinic_integration.cova_clinic_integration.doctype.clinic_schedule_designation.clinic_schedule_designation import (
			ClinicScheduleDesignation,
		)
		from cova_clinic_integration.cova_clinic_integration.doctype.clinic_schedule_employee.clinic_schedule_employee import (
			ClinicScheduleEmployee,
		)

		company: DF.Link
		departments: DF.TableMultiSelect[ClinicScheduleDepartment]
		designations: DF.TableMultiSelect[ClinicScheduleDesignation]
		employees: DF.Table[ClinicScheduleEmployee]
		employees_per_designation: DF.Int
		notes: DF.SmallText | None
		requests_created: DF.Int
		scheduled_from: DF.Date
		scheduled_to: DF.Date
		send_to_cova: DF.Check
		skip_already_scheduled: DF.Check
		skip_employees_on_leave: DF.Check
		status: DF.Literal["Draft", "Scheduled", "Partly Scheduled"]
		test_group: DF.Autocomplete | None
		test_package: DF.Link
		title: DF.Data
		total_employees: DF.Int
		year: DF.Int
	# end: auto-generated types

	def validate(self):
		if not self.company:
			self.company = get_clinic_company()
		if (
			self.scheduled_from
			and self.scheduled_to
			and getdate(self.scheduled_to) < getdate(self.scheduled_from)
		):
			frappe.throw(_("Scheduled To cannot be before Scheduled From."))
		self.year = getdate(self.scheduled_from).year if self.scheduled_from else None

		seen = set()
		for row in self.employees:
			if row.employee in seen:
				frappe.throw(_("Row {0}: {1} is already on this schedule.").format(row.idx, row.employee))
			seen.add(row.employee)
			row.scheduled_from = row.scheduled_from or self.scheduled_from
			row.scheduled_to = row.scheduled_to or self.scheduled_to
			if getdate(row.scheduled_to) < getdate(row.scheduled_from):
				frappe.throw(_("Row {0}: Scheduled To cannot be before Scheduled From.").format(row.idx))

		self.set_totals()

	def set_totals(self):
		self.total_employees = len(self.employees)
		self.requests_created = sum(1 for r in self.employees if r.test_request)
		if not self.requests_created:
			self.status = "Draft"
		elif self.requests_created < self.total_employees:
			self.status = "Partly Scheduled"
		else:
			self.status = "Scheduled"

	@frappe.whitelist()
	def get_employees(self):
		"""Add every active employee the filters pick to the Employees table.

		Departments and designations each narrow the list only when filled in.
		With ``employees_per_designation`` set, that many are drawn at random from
		each designation — the annual rota spreads a large section over several
		schedules rather than sending everyone at once."""
		if not self.flags.ignore_permissions:
			self.check_permission("write")
		candidates = self.matching_employees()
		already = {r.employee for r in self.employees}
		added = 0
		for emp in candidates:
			if emp.name in already:
				continue
			self.append(
				"employees",
				{
					"employee": emp.name,
					"employee_name": emp.employee_name,
					"department": emp.department,
					"designation": emp.designation,
					"scheduled_from": self.scheduled_from,
					"scheduled_to": self.scheduled_to,
				},
			)
			already.add(emp.name)
			added += 1
		self.save()
		return {
			"added": added,
			"total": len(self.employees),
			"on_leave": [{"employee": e.name, "employee_name": e.employee_name} for e in self.on_leave],
		}

	def matching_employees(self):
		"""Active employees the filters pick, less anyone already scheduled for
		this package this year and anyone on approved leave during the window.
		Those on leave are kept in ``self.on_leave`` — they have no request, so
		the next schedule picks them up."""
		if not self.company:
			frappe.throw(_("Set the Company first."))
		scheduled_from = getdate(self.scheduled_from)
		common = {
			"company": self.company,
			"departments": [r.department for r in self.departments if r.department],
			"designations": [r.designation for r in self.designations if r.designation],
			"package": self.test_package if self.skip_already_scheduled else None,
			"already_from": f"{scheduled_from.year}-01-01",
			"already_to": f"{scheduled_from.year}-12-31",
		}
		window = (self.scheduled_from, self.scheduled_to) if self.skip_employees_on_leave else None
		employees = group_employees(**common, leave_window=window)
		self.on_leave = group_employees(**common, leave_window=window, on_leave=True) if window else []

		limit = int(self.employees_per_designation or 0)
		if not limit:
			return employees

		by_designation = {}
		for emp in employees:
			by_designation.setdefault(emp.designation or "", []).append(emp)
		picked = []
		for group in by_designation.values():
			picked.extend(random.sample(group, min(limit, len(group))))
		return picked

	@frappe.whitelist()
	def create_test_requests(self):
		"""Raise a Clinic Test Request for every row that has none yet, and send
		each to Cova when ``send_to_cova`` is ticked and Cova is configured.
		Rows that already carry a request are left alone, so this is safe to run
		again after adding employees."""
		if not self.flags.ignore_permissions:
			self.check_permission("write")
		if self.is_new() or self.has_value_changed("employees"):
			self.save()

		# Imported here: api.py is a heavy module and only this path needs it.
		from cova_clinic_integration.api import send_test_request

		settings = frappe.get_cached_doc("Cova Clinic Settings")
		can_send = bool(self.send_to_cova and settings.base_url and settings.test_request_endpoint)

		created = sent = 0
		failures = []
		for row in self.employees:
			if row.test_request:
				continue
			request = frappe.get_doc(
				{
					"doctype": "Clinic Test Request",
					"member_type": "Active",
					"employee": row.employee,
					"department": row.department,
					"designation": row.designation,
					"clinic_test_schedule": self.name,
					"test_group": self.test_group,
					"status": "Pending",
					"test_package": self.test_package,
					"scheduled_from": row.scheduled_from or self.scheduled_from,
					"scheduled_to": row.scheduled_to or self.scheduled_to,
					"notes": self.notes or _("Annual medical schedule {0}").format(self.name),
				}
			).insert(ignore_permissions=bool(self.flags.ignore_permissions))
			row.test_request = request.name
			created += 1

			if can_send:
				result = send_test_request(request, action="clinic_test_schedule")
				if result.get("error"):
					failures.append(
						{"employee": row.employee, "request": request.name, "reason": result["error"]}
					)
				else:
					sent += 1
			row.received_by_cova = frappe.db.get_value(
				"Clinic Test Request", request.name, "received_by_cova"
			)

		self.save()
		return {
			"created": created,
			"sent": sent,
			"failed": len(failures),
			"failures": failures[:20],
			"send_skipped": bool(self.send_to_cova and not can_send),
		}


@frappe.whitelist()
def test_group_filters(group: str | None = None):
	"""The groups set up in Cova Clinic Settings → Test Scheduling, or one of
	them, for the form to fill its package and filters from."""
	groups = get_test_groups()
	if group is None:
		return sorted(groups)
	if group not in groups:
		frappe.throw(_("Test group {0} is not set up in Cova Clinic Settings.").format(group))
	return groups[group]

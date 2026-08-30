# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

"""Frappe version-tolerant test base classes.

This app supports frappe >=15,<20 (see pyproject [tool.bench.frappe-dependencies]),
and the scaffolded test base class moved between those versions:

  * v15  - only ``frappe.tests.utils.FrappeTestCase`` exists.
  * v16+ - ``frappe.tests.IntegrationTestCase`` is the replacement, and
           ``frappe.tests.utils`` was removed outright in v17, so the scaffolded
           ``from frappe.tests.utils import FrappeTestCase`` raises
           ModuleNotFoundError and the whole app's tests silently collect as zero.

Test modules import ``IntegrationTestCase`` from here so the same file runs on
every supported version.
"""

try:  # frappe v16+
	from frappe.tests import IntegrationTestCase
except ImportError:  # frappe v15
	from frappe.tests.utils import FrappeTestCase as IntegrationTestCase

__all__ = ["IntegrationTestCase"]


# ─── shared fixtures ───────────────────────────────────────────────────────
# Kept here rather than in a test_*.py so the test runner does not collect it.

import contextlib

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

HR_ROLE = "HR Manager"


def ensure_hr_role(user: str | None = None) -> None:
	"""The report endpoints gate on ``assert_health_report_access()``, which wants
	an HR-family role. Grant it to the session user for the duration of the run."""
	user = user or frappe.session.user
	if HR_ROLE in frappe.get_roles(user):
		return
	if not frappe.db.exists("Role", HR_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": HR_ROLE}).insert(ignore_permissions=True)
	doc = frappe.get_doc("User", user)
	doc.append("roles", {"role": HR_ROLE})
	doc.save(ignore_permissions=True)
	frappe.clear_cache(user=user)


def any_company() -> str:
	name = frappe.db.get_value("Company", {}, "name")
	if not name:
		frappe.throw("No Company on this site — cannot build an Employee fixture.")
	return name


def make_employee(payroll_number: str, employee_name: str = "Cova Test") -> str:
	"""Return an Employee carrying ``employee_number = payroll_number``, creating
	one if this payroll number is not on the site yet."""
	existing = frappe.db.get_value("Employee", {"employee_number": payroll_number}, "name")
	if existing:
		return existing

	first, _, last = employee_name.partition(" ")
	doc = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": first or "Cova",
			"last_name": last or "Test",
			"employee_number": payroll_number,
			"gender": "Male",
			"date_of_birth": "1990-01-01",
			"date_of_joining": "2020-01-01",
			"status": "Active",
			"company": any_company(),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


class _RequestStub(Request):
	"""A real werkzeug request, with the JSON body handed over directly.

	``frappe.request`` is a LocalProxy over ``frappe.local.request``, so swapping
	that attribute is enough to drive the endpoints straight from a test. It has
	to be a *real* request rather than a hand-rolled object: anything the
	endpoint touches downstream may reach for ``host``/``headers``/``cookies``
	(frappe.utils.get_url() does, and so any HRMS validation message carrying a
	document link does), and a stub missing one of those replaces the real error
	with an AttributeError.
	"""

	_stub_json = None

	def get_json(self, *a, **kw):
		return self._stub_json


def _make_request(json_body=None, method="POST", args=None, content_type="application/json"):
	builder = EnvironBuilder(
		method=method,
		path="/api/method/cova_test",
		query_string={k: str(v) for k, v in (args or {}).items()},
		headers={"Host": frappe.local.site or "localhost"},
		content_type=content_type,
	)
	request = _RequestStub(builder.get_environ())
	request._stub_json = json_body
	return request


@contextlib.contextmanager
def stub_request(json_body=None, method="POST", args=None, content_type="application/json", form=None):
	"""Run an endpoint as though it had been called over HTTP."""
	saved_request = getattr(frappe.local, "request", None)
	saved_form = getattr(frappe.local, "form_dict", None)
	saved_response = getattr(frappe.local, "response", None)
	frappe.local.request = _make_request(json_body, method, args, content_type)
	if form is not None:
		frappe.local.form_dict = frappe._dict(form)
	frappe.local.response = frappe._dict()
	try:
		yield frappe.local.response
	finally:
		frappe.local.request = saved_request
		if saved_form is not None:
			frappe.local.form_dict = saved_form
		if saved_response is not None:
			frappe.local.response = saved_response


SICK_LEAVE_TYPE = "Sick Leave (Full Pay)"


def ensure_sick_leave_prerequisites(employee: str, on_date: str) -> bool:
	"""Put everything a Sick Leave Application needs on the site for ``employee``.

	Clinic Checkin auto-creates an approved Sick Leave, but HRMS will only let it
	through with a Leave Type, a holiday list the employee resolves to, and a
	submitted Leave Allocation covering the date. The live site has all three;
	a bare dev/CI site has none, and the controller then (correctly) swallows the
	failure — which leaves the happy path untested.

	Returns True if the prerequisites are in place, False if this site would not
	let them be built. Everything created here is inside the test transaction.
	"""
	try:
		if not frappe.db.exists("Leave Type", SICK_LEAVE_TYPE):
			frappe.get_doc(
				{
					"doctype": "Leave Type",
					"leave_type_name": SICK_LEAVE_TYPE,
					"max_leaves_allowed": 30,
				}
			).insert(ignore_permissions=True)

		company = frappe.db.get_value("Employee", employee, "company")
		year = str(frappe.utils.getdate(on_date).year)
		holiday_list = f"Cova Test {year}"
		if not frappe.db.exists("Holiday List", holiday_list):
			doc = frappe.get_doc(
				{
					"doctype": "Holiday List",
					"holiday_list_name": holiday_list,
					"from_date": f"{year}-01-01",
					"to_date": f"{year}-12-31",
					"weekly_off": "Sunday",
				}
			)
			doc.get_weekly_off_dates()
			doc.insert(ignore_permissions=True)

		# Newer HRMS resolves the holiday list through Holiday List Assignment
		# rather than the fields on Company / Employee.
		if frappe.db.exists("DocType", "Holiday List Assignment"):
			if not frappe.db.exists(
				"Holiday List Assignment", {"assigned_to": company, "docstatus": 1}
			):
				assignment = frappe.get_doc(
					{
						"doctype": "Holiday List Assignment",
						"applicable_for": "Company",
						"assigned_to": company,
						"holiday_list": holiday_list,
						"from_date": f"{year}-01-01",
					}
				)
				assignment.insert(ignore_permissions=True)
				assignment.submit()
		else:
			frappe.db.set_value("Company", company, "default_holiday_list", holiday_list)
			frappe.db.set_value("Employee", employee, "holiday_list", holiday_list)

		if not frappe.db.exists(
			"Leave Allocation",
			{"employee": employee, "leave_type": SICK_LEAVE_TYPE, "docstatus": 1},
		):
			allocation = frappe.get_doc(
				{
					"doctype": "Leave Allocation",
					"employee": employee,
					"leave_type": SICK_LEAVE_TYPE,
					"from_date": f"{year}-01-01",
					"to_date": f"{year}-12-31",
					"new_leaves_allocated": 30,
				}
			)
			allocation.insert(ignore_permissions=True)
			allocation.submit()
		return True
	except Exception:
		frappe.log_error(title="COVA sick leave prerequisites", message=frappe.get_traceback())
		return False

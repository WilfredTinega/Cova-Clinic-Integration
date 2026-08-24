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


class _RequestStub:
	"""Minimal stand-in for the werkzeug request the endpoints read.

	``frappe.request`` is a LocalProxy over ``frappe.local.request``, so swapping
	that attribute is enough to drive the endpoints straight from a test.
	"""

	def __init__(self, json_body=None, method="POST", args=None, content_type="application/json"):
		self._json = json_body
		self.method = method
		self.args = frappe._dict(args or {})
		self.content_type = content_type

	def get_json(self, *a, **kw):
		return self._json


@contextlib.contextmanager
def stub_request(json_body=None, method="POST", args=None, content_type="application/json", form=None):
	"""Run an endpoint as though it had been called over HTTP."""
	saved_request = getattr(frappe.local, "request", None)
	saved_form = getattr(frappe.local, "form_dict", None)
	saved_response = getattr(frappe.local, "response", None)
	frappe.local.request = _RequestStub(json_body, method, args, content_type)
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

# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.cova_clinic_integration.doctype.clinic_checkin.clinic_checkin import (
	resolve_leave_approver,
)
from cova_clinic_integration.testing import (
	IntegrationTestCase,
	ensure_sick_leave_prerequisites,
	make_employee,
)

SICK_LEAVE_TYPE = "Sick Leave (Full Pay)"


# The automatic link crawl reaches Company and trips the Fiscal Year overlap on
# a site that already has one. These fixtures build the Employee they need
# themselves (see cova_clinic_integration.testing.make_employee).
IGNORE_TEST_RECORD_DEPENDENCIES = ["Employee", "Company", "Leave Application"]

def _last_sick_leave_error() -> str:
	"""Whatever create_sick_leave_application last logged, for a failing assert."""
	rows = frappe.get_all(
		"Error Log",
		filters={"method": ["like", "%Sick Leave - FAILED%"]},
		fields=["error"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return "(nothing logged — the leave was skipped before it was attempted)"
	return (rows[0]["error"] or "")[-800:]


class TestClinicCheckin(IntegrationTestCase):
	"""Clinic Checkin carries two unrelated payloads on one doctype:

	* biometric punches (``log_type`` / ``time``);
	* sick-off records (``start_date`` + ``end_date``), and only those raise a
	  Leave Application.

	Both name their person in ``employee``; the payload is what tells them apart.

	Duplicates are allowed on purpose — the live "duplicate prevention" hook was
	deliberately not ported.
	"""

	def tearDown(self):
		frappe.db.rollback()

	def _checkin(self, **kw):
		doc = {"doctype": "Clinic Checkin"}
		doc.update(kw)
		return frappe.get_doc(doc).insert(ignore_permissions=True)

	def test_undated_checkin_raises_no_leave(self):
		doc = self._checkin(employee=make_employee("CV-TEST-9010"), reason="walk-in")
		self.assertIsNone(doc.leave_application)

	def test_biometric_punch_raises_no_leave(self):
		employee = make_employee("CV-TEST-9011")
		doc = self._checkin(employee=employee, log_type="IN", time="2026-06-01 08:15:00")
		self.assertIsNone(doc.leave_application)

	def test_dated_checkin_does_not_raise(self):
		# The controller wraps leave creation in try/except and only fires when
		# both dates are present. Where the prerequisites are absent (as on a bare
		# dev site) the check-in must still insert cleanly.
		employee = make_employee("CV-TEST-9012")
		doc = self._checkin(
			employee=employee,
			start_date="2026-06-02",
			end_date="2026-06-03",
			reason="fever",
		)
		self.assertTrue(frappe.db.exists("Clinic Checkin", doc.name))

	def test_dated_checkin_creates_an_approved_sick_leave(self):
		# The happy path: a Leave Type alone is not enough — HRMS also wants a
		# holiday list and a submitted allocation, which is why this builds the
		# lot rather than gating on the Leave Type existing.
		employee = make_employee("CV-TEST-9015")
		if not ensure_sick_leave_prerequisites(employee, "2026-06-02"):
			self.skipTest("this site will not allow the sick-leave prerequisites to be built")

		doc = self._checkin(
			employee=employee,
			start_date="2026-06-02",
			end_date="2026-06-03",
			reason="fever",
		)
		# The controller swallows a failure and logs it, so an assertion that just
		# says "None" tells you nothing on a machine you cannot open. Quote the
		# reason it recorded.
		self.assertIsNotNone(
			doc.leave_application,
			"no Leave Application was created. Controller reported:\n" + _last_sick_leave_error(),
		)
		leave = frappe.get_doc("Leave Application", doc.leave_application)
		self.assertEqual(leave.leave_type, SICK_LEAVE_TYPE)
		self.assertEqual(leave.status, "Approved")
		self.assertEqual(leave.docstatus, 1)
		self.assertEqual(str(leave.from_date), "2026-06-02")
		self.assertEqual(str(leave.to_date), "2026-06-03")

	def test_leave_approver_is_filled_in_when_hrms_demands_one(self):
		# HR Settings ships with "Leave Approver Mandatory In Leave Application"
		# on, and an employee record with no approver on it then took the whole
		# leave down (HRMS: "Leave Approver is mandatory"). The check-in has no
		# human in front of it to pick one, so it falls back to the user the
		# check-in arrived as.
		frappe.db.set_single_value("HR Settings", "leave_approver_mandatory_in_leave_application", 1)
		employee = frappe.get_doc("Employee", make_employee("CV-TEST-9016"))
		self.assertFalse(employee.leave_approver, "fixture is only meaningful without an approver")
		self.assertEqual(resolve_leave_approver(employee), frappe.session.user)

	def test_leave_approver_is_left_alone_when_hrms_does_not_demand_one(self):
		frappe.db.set_single_value("HR Settings", "leave_approver_mandatory_in_leave_application", 0)
		employee = frappe.get_doc("Employee", make_employee("CV-TEST-9017"))
		self.assertIsNone(resolve_leave_approver(employee))

	def test_duplicates_are_allowed(self):
		employee = make_employee("CV-TEST-9013")
		payload = {
			"employee": employee,
			"start_date": "2026-06-04",
			"end_date": "2026-06-04",
			"reason": "same again",
		}
		first = self._checkin(**payload)
		second = self._checkin(**payload)
		self.assertNotEqual(first.name, second.name)

	def test_reinserting_the_same_row_does_not_duplicate_the_leave(self):
		# create_sick_leave_application runs on after_insert AND on_update, so a
		# save must not produce a second Leave Application.
		employee = make_employee("CV-TEST-9014")
		doc = self._checkin(
			employee=employee, start_date="2026-06-05", end_date="2026-06-05", reason="recheck"
		)
		before = doc.leave_application
		doc.reason = "recheck (edited)"
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.leave_application, before)

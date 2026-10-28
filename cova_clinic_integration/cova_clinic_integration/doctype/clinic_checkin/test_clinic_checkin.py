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
IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Employee",
	"Company",
	"Leave Application",
	"Department",
	"Designation",
	"Clinic Test Schedule",
	"Clinic Ticket",
]


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

	def _mark(self, employee, date, status):
		attendance = frappe.get_doc(
			{
				"doctype": "Attendance",
				"employee": employee,
				"attendance_date": date,
				"status": status,
				"company": frappe.db.get_value("Employee", employee, "company"),
			}
		)
		attendance.flags.ignore_permissions = True
		attendance.insert()
		attendance.submit()
		return attendance.name

	def _status_on(self, employee, date):
		return frappe.db.get_value(
			"Attendance", {"employee": employee, "attendance_date": date, "docstatus": 1}, "status"
		)

	def test_backdated_sick_off_overrides_present_and_absent(self):
		# A backdated sick off lands on days that are already marked: Present
		# where the employee clocked in before going to the clinic (HRMS rejects
		# leave over those, which used to lose the whole window) and Absent for
		# the rest. The sick off wins: both are cancelled and every day goes on
		# leave.
		employee = make_employee("CV-TEST-9018")
		if not ensure_sick_leave_prerequisites(employee, "2026-06-08"):
			self.skipTest("this site will not allow the sick-leave prerequisites to be built")

		present = self._mark(employee, "2026-06-08", "Present")
		absent = self._mark(employee, "2026-06-09", "Absent")

		doc = self._checkin(
			employee=employee, start_date="2026-06-08", end_date="2026-06-10", reason="backdated"
		)
		self.assertIsNotNone(
			doc.leave_application,
			"no Leave Application was created. Controller reported:\n" + _last_sick_leave_error(),
		)
		leave = frappe.get_doc("Leave Application", doc.leave_application)
		self.assertEqual(str(leave.from_date), "2026-06-08")
		self.assertEqual(str(leave.to_date), "2026-06-10")
		self.assertEqual(frappe.db.get_value("Attendance", present, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Attendance", absent, "docstatus"), 2)
		for date in ("2026-06-08", "2026-06-09", "2026-06-10"):
			self.assertEqual(self._status_on(employee, date), "On Leave", date)

	def test_dates_added_on_save_override_attendance(self):
		# The window can arrive on a later save rather than the insert.
		employee = make_employee("CV-TEST-9019")
		if not ensure_sick_leave_prerequisites(employee, "2026-06-15"):
			self.skipTest("this site will not allow the sick-leave prerequisites to be built")

		present = self._mark(employee, "2026-06-15", "Present")
		doc = self._checkin(employee=employee, reason="walk-in")
		self.assertIsNone(doc.leave_application)

		doc.start_date = "2026-06-15"
		doc.end_date = "2026-06-16"
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertIsNotNone(
			doc.leave_application,
			"no Leave Application was created. Controller reported:\n" + _last_sick_leave_error(),
		)
		self.assertEqual(frappe.db.get_value("Attendance", present, "docstatus"), 2)
		self.assertEqual(self._status_on(employee, "2026-06-15"), "On Leave")

	def _logs(self, checkin, since):
		# Check-in names are reused once a test rolls back, so only this test's rows.
		return frappe.get_all(
			"Clinic API Log",
			filters={
				"reference_doctype": "Clinic Checkin",
				"reference_name": checkin,
				"action": "sick_leave",
				"creation": [">=", since],
			},
			fields=["action", "status", "message"],
		)

	def test_filed_sick_leave_is_logged(self):
		employee = make_employee("CV-TEST-9020")
		if not ensure_sick_leave_prerequisites(employee, "2026-06-22"):
			self.skipTest("this site will not allow the sick-leave prerequisites to be built")
		since = frappe.utils.now()
		doc = self._checkin(employee=employee, start_date="2026-06-22", end_date="2026-06-22", reason="flu")
		logs = self._logs(doc.name, since)
		self.assertEqual([r.status for r in logs], ["Success"], logs)
		self.assertIn(doc.leave_application, logs[0].message)

	def test_no_allocation_parks_a_draft_and_keeps_attendance(self):
		# Without a sick allocation HRMS refuses the leave outright. The sick off
		# is kept as a draft with a comment for HR; attendance is left alone
		# until HR allocates and submits it.
		employee = make_employee("CV-TEST-9021")
		if not ensure_sick_leave_prerequisites(employee, "2026-06-24"):
			self.skipTest("this site will not allow the sick-leave prerequisites to be built")
		for name in frappe.get_all(
			"Leave Allocation", filters={"employee": employee, "docstatus": 1}, pluck="name"
		):
			frappe.get_doc("Leave Allocation", name).cancel()
		present = self._mark(employee, "2026-06-24", "Present")

		since = frappe.utils.now()
		doc = self._checkin(employee=employee, start_date="2026-06-24", end_date="2026-06-25", reason="flu")
		self.assertIsNotNone(doc.leave_application)
		leave = frappe.get_doc("Leave Application", doc.leave_application)
		self.assertEqual(leave.docstatus, 0)
		self.assertEqual(leave.status, "Open")
		self.assertEqual(str(leave.from_date), "2026-06-24")
		self.assertEqual(str(leave.to_date), "2026-06-25")
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{
					"reference_doctype": "Leave Application",
					"reference_name": leave.name,
					"comment_type": "Comment",
				},
			)
		)
		self.assertEqual(frappe.db.get_value("Attendance", present, "docstatus"), 1)
		self.assertEqual([r.status for r in self._logs(doc.name, since)], ["Warning"])

		# A later save does not park a second draft.
		doc.reason = "flu (edited)"
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.count("Leave Application", {"employee": employee, "docstatus": 0}), 1)

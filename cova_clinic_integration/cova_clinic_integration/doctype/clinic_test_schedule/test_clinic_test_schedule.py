# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.testing import IntegrationTestCase, make_employee

IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Employee", "Company", "Department", "Designation", "User",
	"Clinic Checkin", "Clinic Test Request", "Leave Application", "Cova Members", "Test Package",
]


class TestClinicTestSchedule(IntegrationTestCase):
	def setUp(self):
		self.employee = make_employee("CV-SCH-0001", "Schedule Tester")
		emp = frappe.get_doc("Employee", self.employee)
		self.company = emp.company
		self.designation = emp.designation
		if not frappe.db.exists("Test Package", "Annual Medical"):
			frappe.get_doc({"doctype": "Test Package", "package_name": "Annual Medical"}).insert()

	def tearDown(self):
		frappe.db.rollback()

	def _schedule(self, **kw):
		doc = {
			"doctype": "Clinic Test Schedule",
			"title": "Annual Medical",
			"test_package": "Annual Medical",
			"company": self.company,
			"scheduled_from": "2026-10-01",
			"scheduled_to": "2026-10-15",
			"send_to_cova": 0,
		}
		doc.update(kw)
		return frappe.get_doc(doc).insert()

	def test_get_employees_honours_the_filters(self):
		sched = self._schedule()
		sched.get_employees()
		self.assertIn(self.employee, [r.employee for r in sched.employees])
		self.assertEqual(sched.year, 2026)

		# A designation nobody holds narrows the list to no one.
		if not frappe.db.exists("Designation", "CV Nobody Holds This"):
			frappe.get_doc({"doctype": "Designation", "designation_name": "CV Nobody Holds This"}).insert()
		narrowed = self._schedule(designations=[{"designation": "CV Nobody Holds This"}])
		narrowed.get_employees()
		self.assertEqual(narrowed.employees, [])

	def test_rows_take_the_schedule_dates(self):
		sched = self._schedule(employees=[{"employee": self.employee}])
		self.assertEqual(str(sched.employees[0].scheduled_from), "2026-10-01")
		self.assertEqual(str(sched.employees[0].scheduled_to), "2026-10-15")

	def test_create_test_requests_once_and_skip_next_time(self):
		sched = self._schedule(employees=[{"employee": self.employee}], test_group="Cholinesterase")
		result = sched.create_test_requests()
		self.assertEqual(result["created"], 1)
		request = sched.employees[0].test_request
		self.assertEqual(frappe.db.get_value("Clinic Test Request", request, "test_package"), "Annual Medical")
		# The request remembers the schedule, group and the department and
		# designation it was scheduled under.
		values = frappe.db.get_value(
			"Clinic Test Request", request, ["clinic_test_schedule", "test_group", "designation"], as_dict=True
		)
		self.assertEqual(values.clinic_test_schedule, sched.name)
		self.assertEqual(values.test_group, "Cholinesterase")
		self.assertEqual(values.designation, sched.employees[0].designation)
		self.assertEqual(sched.status, "Scheduled")

		# Running again raises nothing new.
		self.assertEqual(sched.create_test_requests()["created"], 0)

		# And a new schedule for the same package and year leaves them out.
		again = self._schedule()
		again.get_employees()
		self.assertNotIn(self.employee, [r.employee for r in again.employees])

	def test_rejects_a_reversed_window(self):
		with self.assertRaises(frappe.ValidationError):
			self._schedule(scheduled_from="2026-10-15", scheduled_to="2026-10-01")

	def test_a_group_from_settings_fills_the_filters(self):
		from cova_clinic_integration.cova_clinic_integration.doctype.clinic_test_schedule.clinic_test_schedule import (
			test_group_filters,
		)

		if not frappe.db.exists("Designation", "CV Sprayer"):
			frappe.get_doc({"doctype": "Designation", "designation_name": "CV Sprayer"}).insert()
		settings = frappe.get_doc("Cova Clinic Settings")
		settings.set("test_groups", [
			{"group_name": "CV Chol", "test_package": "Annual Medical", "designation": "CV Sprayer",
			 "employees_per_designation": 5},
		])
		settings.save(ignore_permissions=True)

		self.assertIn("CV Chol", test_group_filters())
		group = test_group_filters("CV Chol")
		self.assertEqual(group["designations"], ["CV Sprayer"])
		self.assertEqual(group["test_package"], "Annual Medical")
		self.assertEqual(group["employees_per_designation"], 5)

	def test_schedule_tests_from_the_dashboard(self):
		from cova_clinic_integration import api
		from cova_clinic_integration.testing import stub_request

		settings = frappe.get_doc("Cova Clinic Settings")
		settings.company = self.company
		settings.set("test_groups", [
			{"group_name": "CV Dash", "test_package": "Annual Medical", "designation": self.designation},
		])
		settings.save(ignore_permissions=True)

		body = {"test_group": "CV Dash", "scheduled_from": "2026-11-01", "scheduled_to": "2026-11-10",
			"send_to_cova": False}
		with stub_request(json_body=body):
			preview = api.preview_test_schedule()
		self.assertGreaterEqual(preview["count"], 1)

		with stub_request(json_body=body):
			result = api.schedule_tests()
		self.assertEqual(result["created"], result["employees"])
		self.assertTrue(frappe.db.exists("Clinic Test Request", {"clinic_test_schedule": result["schedule"],
			"employee": self.employee}))

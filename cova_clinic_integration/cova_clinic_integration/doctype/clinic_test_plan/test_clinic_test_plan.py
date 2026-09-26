# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.testing import IntegrationTestCase, make_employee

IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Employee", "Company", "Department", "Designation", "User", "Leave Application",
	"Clinic Checkin", "Clinic Test Request", "Clinic Test Schedule", "Clinic Ticket", "Cova Members", "Test Package",
]
ROLE = "CV Plan Sprayer"


class TestClinicTestPlan(IntegrationTestCase):
	def setUp(self):
		if not frappe.db.exists("Designation", ROLE):
			frappe.get_doc({"doctype": "Designation", "designation_name": ROLE}).insert()
		if not frappe.db.exists("Test Package", "Annual Medical"):
			frappe.get_doc({"doctype": "Test Package", "package_name": "Annual Medical"}).insert()
		self.present = make_employee("CV-PLAN-0001", "Plan Present")
		self.away = make_employee("CV-PLAN-0002", "Plan Away")
		for e in (self.present, self.away):
			frappe.db.set_value("Employee", e, "designation", ROLE)
		self.company = frappe.db.get_value("Employee", self.present, "company")

		settings = frappe.get_doc("Cova Clinic Settings")
		settings.company = self.company
		settings.set("test_groups", [{"group_name": "CV Sprayers", "test_package": "Annual Medical", "designation": ROLE}])
		settings.save(ignore_permissions=True)

		# Approved leave over the whole first round, written straight in: the
		# leave rules are HRMS's business, only the approved record matters here.
		leave = frappe.new_doc("Leave Application")
		leave.update({"employee": self.away, "from_date": "2026-10-01", "to_date": "2026-10-31",
			"status": "Approved", "docstatus": 1, "leave_type": "Casual Leave", "company": self.company,
			"posting_date": "2026-09-01"})
		leave.name = "CV-PLAN-LEAVE-1"
		leave.db_insert()

	def tearDown(self):
		frappe.db.rollback()

	def _plan(self):
		return frappe.get_doc({
			"doctype": "Clinic Test Plan", "test_group": "CV Sprayers", "company": self.company,
			"period_from": "2026-10-01", "period_to": "2026-11-30", "number_of_rounds": 2, "send_to_cova": 0,
		}).insert()

	def test_metrics_and_rounds(self):
		plan = self._plan()
		self.assertEqual(plan.test_package, "Annual Medical")
		self.assertEqual(plan.total_employees, 2)
		self.assertEqual(plan.employees_per_round, 1)
		plan.generate_rounds()
		self.assertEqual([(str(r.scheduled_from), str(r.scheduled_to)) for r in plan.rounds],
			[("2026-10-01", "2026-10-31"), ("2026-11-01", "2026-11-30")])

	def test_employees_on_leave_carry_over_to_the_next_round(self):
		plan = self._plan()
		plan.generate_rounds()

		first = plan.schedule_next_round()
		self.assertEqual([e["employee"] for e in first["on_leave"]], [self.away])
		scheduled = frappe.get_all("Clinic Test Request",
			filters={"clinic_test_schedule": first["schedule"]}, pluck="employee")
		self.assertEqual(scheduled, [self.present])
		self.assertEqual(plan.rounds[0].skipped_on_leave, 1)
		self.assertEqual(plan.status, "In Progress")

		# Back from leave in round two, and the only one left to test.
		second = plan.schedule_next_round()
		self.assertEqual(frappe.get_all("Clinic Test Request",
			filters={"clinic_test_schedule": second["schedule"]}, pluck="employee"), [self.away])
		self.assertEqual(plan.scheduled_employees, 2)
		self.assertEqual(plan.remaining_employees, 0)

		with self.assertRaises(frappe.ValidationError):
			plan.schedule_next_round()

# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration import api
from cova_clinic_integration.testing import IntegrationTestCase, make_employee, stub_request

IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Employee",
	"Company",
	"Department",
	"Designation",
	"User",
	"Clinic Ticket",
	"Clinic Checkin",
	"Leave Application",
	"Cova Members",
	"Clinic Test Request",
	"Clinic Test Schedule",
	"Branch",
]


class TestWorkAccident(IntegrationTestCase):
	def setUp(self):
		self.employee = make_employee("CV-WA-0001", "Accident Tester")

	def tearDown(self):
		frappe.db.rollback()

	def _accident(self, **kw):
		doc = {
			"doctype": "Work Accident",
			"employee": self.employee,
			"accident_date": "2026-09-01",
			"accident_type": "Cut or Laceration",
			"severity": "Minor",
			"description": "cut on a pruning knife",
		}
		doc.update(kw)
		return frappe.get_doc(doc).insert()

	def test_days_lost_make_it_a_lost_time_injury(self):
		self.assertEqual(self._accident().lost_time_injury, 0)
		self.assertEqual(self._accident(days_lost=3, severity="Serious").lost_time_injury, 1)

	def test_no_future_dates(self):
		with self.assertRaises(frappe.ValidationError):
			self._accident(accident_date="2099-01-01")

	def test_sending_to_the_clinic_issues_an_urgent_ticket(self):
		accident = self._accident(severity="Moderate", accident_time="09:15:00")
		ticket = accident.send_to_clinic()
		self.assertEqual(frappe.db.get_value("Work Accident", accident.name, "clinic_ticket"), ticket)
		self.assertEqual(frappe.db.get_value("Clinic Ticket", ticket, "urgency"), "Urgent")
		# Sending again keeps the one ticket.
		accident.reload()
		self.assertEqual(accident.send_to_clinic(), ticket)

	def test_record_from_the_dashboard_and_report(self):
		body = {
			"employee": self.employee,
			"accident_date": "2026-09-02",
			"accident_type": "Chemical Exposure",
			"severity": "Serious",
			"body_part": "Eye",
			"days_lost": 2,
			"description": "splash while mixing",
			"send_to_clinic": True,
		}
		with stub_request(json_body=body):
			out = api.record_work_accident()
		self.assertTrue(out["clinic_ticket"])
		with stub_request(json_body={"year": "2026"}):
			report = api.work_accident_report()
		self.assertGreaterEqual(report["kpis"]["lost_time"], 1)
		self.assertIn(out["name"], [r["name"] for r in report["rows"]])

	def _mark_first_aider(self, **body):
		with stub_request(json_body={"employee": self.employee, **body}):
			return api.add_first_aider()

	def test_first_aiders_are_employees_with_their_own_phone_and_farm(self):
		frappe.db.set_value("Employee", self.employee, "cell_number", "+254700111222")
		if not frappe.db.exists("Branch", "CV Test Farm"):
			frappe.get_doc({"doctype": "Branch", "branch": "CV Test Farm"}).insert()
		frappe.db.set_value("Employee", self.employee, "branch", "CV Test Farm")
		if frappe.db.has_column("Employee", "custom_farm"):
			frappe.db.set_value("Employee", self.employee, "custom_farm", None)

		out = self._mark_first_aider(certified_until="2020-01-01")
		self.assertEqual(frappe.db.get_value("Employee", self.employee, "is_first_aider"), 1)
		self.assertEqual(out["phone"], "+254700111222")

		with stub_request(json_body={"year": "2026"}):
			report = api.work_accident_report()
		me = next(a for a in report["first_aiders"] if a["employee"] == self.employee)
		self.assertEqual(me["farm"], "CV Test Farm")
		self.assertEqual(me["phone"], "+254700111222")
		self.assertEqual(me["certificate"], "Expired")

	def test_the_accident_records_the_first_aider_who_attended(self):
		frappe.db.set_value("Employee", self.employee, {"is_first_aider": 1, "cell_number": "0700"})
		accident = self._accident(first_aider=self.employee)
		# A first aider attending means first aid was given.
		self.assertEqual(accident.first_aid_given, 1)

		with stub_request(json_body={"year": "2026"}):
			report = api.work_accident_report()
		row = next(r for r in report["rows"] if r["name"] == accident.name)
		self.assertEqual(row["first_aider_name"], "Accident Tester")
		me = next(a for a in report["first_aiders"] if a["employee"] == self.employee)
		self.assertEqual(me["attended"], 1)

	def test_attended_by_searches_every_employee_first_aiders_first(self):
		other = make_employee("CV-WA-0002", "Accident Helper")
		frappe.db.set_value("Employee", self.employee, "is_first_aider", 0)
		frappe.db.set_value("Employee", other, "is_first_aider", 1)

		found = api.first_aider_link_query("Employee", "Accident", "name", 0, 20, None)
		names = [r[0] for r in found]
		# Anyone can be picked, and the marked first aider comes first, labelled.
		self.assertIn(self.employee, names)
		self.assertLess(names.index(other), names.index(self.employee))
		self.assertIn("First Aider", found[names.index(other)][2])

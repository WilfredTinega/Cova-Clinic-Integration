# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.cova_clinic_integration.doctype.clinic_ticket.clinic_ticket import expire_tickets
from cova_clinic_integration import api
from cova_clinic_integration.testing import IntegrationTestCase, make_employee, stub_request

IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Employee", "Company", "Department", "Designation", "User",
	"Clinic Checkin", "Clinic Test Request", "Leave Application", "Cova Members", "Test Package",
]


class TestClinicTicket(IntegrationTestCase):
	def setUp(self):
		self.employee = make_employee("CV-TKT-0001", "Ticket Tester")

	def tearDown(self):
		frappe.db.rollback()

	def _ticket(self, **kw):
		doc = {"doctype": "Clinic Ticket", "employee": self.employee, "ticket_date": "2026-09-01", "reason": "headache"}
		doc.update(kw)
		return frappe.get_doc(doc).insert()

	def test_defaults_valid_until_to_the_ticket_date(self):
		ticket = self._ticket()
		self.assertEqual(str(ticket.valid_until), "2026-09-01")
		self.assertEqual(ticket.status, "Issued")

	def test_one_open_ticket_per_day(self):
		self._ticket()
		with self.assertRaises(frappe.ValidationError):
			self._ticket()

	def test_checkin_marks_the_ticket_visited(self):
		ticket = self._ticket(valid_until="2026-09-02")
		checkin = frappe.get_doc({
			"doctype": "Clinic Checkin", "employee": self.employee, "log_type": "IN", "time": "2026-09-02 09:10:00",
		}).insert(ignore_permissions=True)
		ticket.reload()
		self.assertEqual(ticket.status, "Visited")
		self.assertEqual(ticket.clinic_checkin, checkin.name)

	def test_checkin_outside_the_window_leaves_the_ticket_open(self):
		ticket = self._ticket()
		frappe.get_doc({
			"doctype": "Clinic Checkin", "employee": self.employee, "log_type": "IN", "time": "2026-09-05 09:10:00",
		}).insert(ignore_permissions=True)
		ticket.reload()
		self.assertEqual(ticket.status, "Issued")

	def test_unused_tickets_expire(self):
		ticket = self._ticket(ticket_date="2020-01-01")
		expire_tickets()
		self.assertEqual(frappe.db.get_value("Clinic Ticket", ticket.name, "status"), "Expired")

	def test_arrival_flags_the_ticket_with_the_time_taken(self):
		ticket = self._ticket(time_issued="08:00:00")
		frappe.get_doc({
			"doctype": "Clinic Checkin", "employee": self.employee, "log_type": "IN", "time": "2026-09-01 08:25:00",
		}).insert(ignore_permissions=True)
		ticket.reload()
		self.assertEqual(ticket.arrived, 1)
		self.assertEqual(str(ticket.visited_at), "2026-09-01 08:25:00")
		self.assertEqual(str(ticket.left_for_clinic_at), "2026-09-01 08:00:00")
		self.assertEqual(ticket.minutes_to_reach, 25)

	def test_request_and_mark_arrived_from_the_dashboard(self):
		body = {"employee": self.employee, "ticket_date": "2026-09-03", "appointment_time": "10:30",
			"urgency": "Urgent", "reason": "chest pain"}
		with stub_request(json_body=body):
			out = api.request_medical_attention()
		ticket = frappe.get_doc("Clinic Ticket", out["name"])
		self.assertEqual(ticket.urgency, "Urgent")
		self.assertEqual(str(ticket.appointment_time)[:5], "10:30")

		frappe.db.set_value("Clinic Ticket", ticket.name, "time_issued", "10:00:00")
		with stub_request(json_body={"ticket": ticket.name, "arrived_at": "2026-09-03 10:40:00"}):
			arrived = api.mark_ticket_arrived()
		self.assertEqual(arrived["status"], "Visited")
		self.assertEqual(arrived["minutes_to_reach"], 40)

		# A second arrival on a used ticket is refused.
		with self.assertRaises(frappe.ValidationError):
			with stub_request(json_body={"ticket": ticket.name}):
				api.mark_ticket_arrived()

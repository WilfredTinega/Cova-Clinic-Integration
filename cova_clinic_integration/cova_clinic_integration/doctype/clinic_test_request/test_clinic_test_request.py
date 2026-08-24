# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.testing import IntegrationTestCase, make_employee


# The automatic link crawl reaches Company and trips the Fiscal Year overlap on
# a site that already has one. These fixtures build the Employee they need
# themselves (see cova_clinic_integration.testing.make_employee).
IGNORE_TEST_RECORD_DEPENDENCIES = ["Employee", "Company", "Leave Application"]

class TestClinicTestRequest(IntegrationTestCase):
	"""The request is what the app sends COVA and what ``receive_test_result``
	closes out, so its mandatory set is part of the API contract."""

	def _base(self, **kw):
		doc = {
			"doctype": "Clinic Test Request",
			"member_type": "Active",
			"status": "Pending",
			"test_package": "Annual Medical",
			"scheduled_from": "2026-01-01",
			"scheduled_to": "2026-01-16",
		}
		doc.update(kw)
		return frappe.get_doc(doc)

	def tearDown(self):
		frappe.db.rollback()

	def test_series(self):
		doc = self._base(employee=make_employee("CV-TEST-9002")).insert()
		self.assertTrue(doc.name.startswith("TR-"))
		self.assertEqual(doc.status, "Pending")

	def test_scheduling_window_is_mandatory(self):
		doc = self._base()
		doc.scheduled_from = None
		with self.assertRaises(frappe.MandatoryError):
			doc.insert()

	def test_test_package_is_a_fixed_list(self):
		with self.assertRaises(frappe.ValidationError):
			self._base(test_package="Something Else").insert()

	def test_pre_employment_request_needs_no_employee(self):
		doc = self._base(
			member_type="Pre Employment",
			nationa_id="12312312",
			full_name="Jane Pre",
			test_package="Pre Employment Wellness",
		).insert()
		self.assertIsNone(doc.employee)
		self.assertEqual(doc.nationa_id, "12312312")

# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.testing import IntegrationTestCase, make_employee


# The automatic link crawl reaches Company and trips the Fiscal Year overlap on
# a site that already has one. These fixtures build the Employee they need
# themselves (see cova_clinic_integration.testing.make_employee).
IGNORE_TEST_RECORD_DEPENDENCIES = ["Employee", "Company", "Leave Application"]

class TestClinicTestResult(IntegrationTestCase):
	"""Results carry the per-condition risk grades COVA returns, on the
	``results`` child table (Test Result)."""

	def tearDown(self):
		frappe.db.rollback()

	def _case(self, name="Cova Annual Medical"):
		if not frappe.db.exists("Medical Case", name):
			frappe.get_doc({"doctype": "Medical Case", "cases": name}).insert()
		return name

	def test_series_and_risk_rows(self):
		doc = frappe.get_doc(
			{
				"doctype": "Clinic Test Result",
				"member_type": "Active",
				"employee": make_employee("CV-TEST-9003"),
				"payroll_number": "CV-TEST-9003",
				"test_package": "Annual Medical",
				"clinical_outcome": "FitForWork",
				"results": [{"test": self._case(), "select_tezd": "Low Risk"}],
			}
		).insert()

		self.assertTrue(doc.name.startswith("CTR-"))
		self.assertEqual(doc.results[0].select_tezd, "Low Risk")

	def test_risk_grade_is_a_fixed_list(self):
		doc = frappe.get_doc(
			{
				"doctype": "Clinic Test Result",
				"member_type": "Active",
				"results": [{"test": self._case(), "select_tezd": "Catastrophic"}],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_visit_reference_points_at_clinic_visit_cost(self):
		# The link target matters: api.py used to write to a "Clinic Visit"
		# doctype this app does not ship.
		meta = frappe.get_meta("Clinic Test Result")
		self.assertEqual(meta.get_field("clinic_visit_reference").options, "Clinic Visit Cost")

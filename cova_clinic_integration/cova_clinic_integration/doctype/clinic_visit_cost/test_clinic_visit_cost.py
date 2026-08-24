# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.testing import IntegrationTestCase, make_employee


# The automatic link crawl reaches Company and trips the Fiscal Year overlap on
# a site that already has one. These fixtures build the Employee they need
# themselves (see cova_clinic_integration.testing.make_employee).
IGNORE_TEST_RECORD_DEPENDENCIES = ["Employee", "Company", "Leave Application"]

class TestClinicVisitCost(IntegrationTestCase):
	"""The visit the ``receive_visit`` webhook writes: line items plus the
	per-category benefit balances COVA reported as remaining."""

	def tearDown(self):
		frappe.db.rollback()

	def test_autoname_uses_full_name(self):
		employee = make_employee("CV-TEST-9004", "Mary Kamau")
		doc = frappe.get_doc(
			{
				"doctype": "Clinic Visit Cost",
				"employee": employee,
				"full_name": "Mary Kamau",
				"payroll_number": "CV-TEST-9004",
				"visit_date": "2026-05-04",
				"total_cost": 1700,
				"visit_line_item": [
					{"purpose": "Consultation", "cost": 500},
					{"purpose": "Pharmacy", "cost": 1200},
				],
			}
		).insert()

		self.assertTrue(doc.name.startswith("CV-"))
		self.assertIn("Mary Kamau", doc.name)
		self.assertEqual(sum(r.cost for r in doc.visit_line_item), doc.total_cost)

	def test_line_item_purpose_is_a_fixed_list(self):
		doc = frappe.get_doc(
			{
				"doctype": "Clinic Visit Cost",
				"employee": make_employee("CV-TEST-9005"),
				"visit_date": "2026-05-04",
				"visit_line_item": [{"purpose": "Massage", "cost": 100}],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_there_is_no_combined_benefit_total_field(self):
		# api.py used to assign benefit_balance_after, which this doctype has
		# never had — the assignment was silently dropped.
		meta = frappe.get_meta("Clinic Visit Cost")
		self.assertIsNone(meta.get_field("benefit_balance_after"))
		self.assertIsNone(meta.get_field("visit_datetime"))
		for f in ("benefit_consultation", "benefit_pharmacy", "benefit_laboratory",
				  "benefit_diagnostic", "benefit_specialist"):
			self.assertIsNotNone(meta.get_field(f), f)

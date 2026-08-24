# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.testing import IntegrationTestCase


# The automatic link crawl reaches Company and trips the Fiscal Year overlap on
# a site that already has one. These fixtures build the Employee they need
# themselves (see cova_clinic_integration.testing.make_employee).
IGNORE_TEST_RECORD_DEPENDENCIES = ["Employee", "Company", "Leave Application"]

class TestHealthMonthlyReport(IntegrationTestCase):
	"""The monthly report is the parent the ``receive_health_report`` webhook
	builds: one Health Report child row per condition."""

	def tearDown(self):
		frappe.db.rollback()

	def _case(self, name):
		if not frappe.db.exists("Medical Case", name):
			frappe.get_doc({"doctype": "Medical Case", "cases": name}).insert()
		return name

	def test_series_and_children(self):
		doc = frappe.get_doc(
			{
				"doctype": "Health Monthly Report",
				"month": "MAR",
				"posting_date": "2026-03-31",
				"total_cases": 42,
				"medical_cases": [
					{"medical_case": self._case("Cova Malaria"), "case_count": 12},
					{"medical_case": self._case("Cova URTI"), "case_count": 30},
				],
			}
		).insert()

		self.assertTrue(doc.name.startswith("HMR-"))
		self.assertEqual(len(doc.medical_cases), 2)
		self.assertEqual(sum(r.case_count for r in doc.medical_cases), doc.total_cases)

	def test_month_is_mandatory(self):
		with self.assertRaises(frappe.MandatoryError):
			frappe.get_doc({"doctype": "Health Monthly Report", "posting_date": "2026-03-31"}).insert()

	def test_month_is_a_fixed_list(self):
		doc = frappe.get_doc(
			{"doctype": "Health Monthly Report", "month": "MARCH", "posting_date": "2026-03-31"}
		)
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_child_row_needs_a_condition(self):
		doc = frappe.get_doc(
			{
				"doctype": "Health Monthly Report",
				"month": "APR",
				"posting_date": "2026-04-30",
				"medical_cases": [{"case_count": 5}],
			}
		)
		with self.assertRaises(frappe.MandatoryError):
			doc.insert()

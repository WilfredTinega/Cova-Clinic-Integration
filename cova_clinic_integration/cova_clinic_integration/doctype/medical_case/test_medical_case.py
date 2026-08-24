# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.exceptions import DuplicateEntryError

from cova_clinic_integration.testing import IntegrationTestCase


# The automatic link crawl reaches Company and trips the Fiscal Year overlap on
# a site that already has one. These fixtures build the Employee they need
# themselves (see cova_clinic_integration.testing.make_employee).
IGNORE_TEST_RECORD_DEPENDENCIES = ["Employee", "Company", "Leave Application"]

class TestMedicalCase(IntegrationTestCase):
	"""Medical Case is autonamed ``field:cases``, so the condition name *is* the
	primary key — which is what lets the COVA webhooks upsert conditions by name."""

	CASE = "Cova Test Condition"

	def tearDown(self):
		frappe.db.rollback()

	def test_name_is_the_condition(self):
		doc = frappe.get_doc({"doctype": "Medical Case", "cases": self.CASE}).insert()
		self.assertEqual(doc.name, self.CASE)

	def test_condition_is_unique(self):
		frappe.get_doc({"doctype": "Medical Case", "cases": self.CASE}).insert()
		with self.assertRaises(DuplicateEntryError):
			frappe.get_doc({"doctype": "Medical Case", "cases": self.CASE}).insert()

	def test_condition_is_mandatory(self):
		# autoname (field:cases) runs before mandatory validation, so the refusal
		# arrives as a plain ValidationError rather than MandatoryError.
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Medical Case"}).insert()

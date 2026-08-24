# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.testing import IntegrationTestCase, make_employee


# The automatic link crawl reaches Company and trips the Fiscal Year overlap on
# a site that already has one. These fixtures build the Employee they need
# themselves (see cova_clinic_integration.testing.make_employee).
IGNORE_TEST_RECORD_DEPENDENCIES = ["Employee", "Company", "Leave Application"]

class TestCovaMembers(IntegrationTestCase):
	"""Cova Members is named by its controller's ``autoname()``.

	The doctype used to declare ``{full_name}.-.{national_id}.-.{employee}`` under
	the "Expression" naming rule, but that rule needs a ``format:`` prefix — frappe
	matched no rule and every row fell through to a hash. The controller joins the
	parts that are present instead, which the two member types need because they
	carry different identifiers.
	"""

	def tearDown(self):
		frappe.db.rollback()

	def test_active_member_named_after_employee_and_id(self):
		employee = make_employee("CV-TEST-9001", "Grace Wanjiru")
		doc = frappe.get_doc(
			{
				"doctype": "Cova Members",
				"member_type": "Active",
				"employee": employee,
				"full_name": "Grace Wanjiru",
				"payroll_number": employee,
				"status": "Active",
			}
		).insert()
		self.assertEqual(doc.name, "Grace Wanjiru - %s" % employee)

	def test_pre_employment_member_named_after_national_id(self):
		doc = frappe.get_doc(
			{
				"doctype": "Cova Members",
				"member_type": "Pre Employment",
				"national_id": "33445566",
				"full_name": "Peter Otieno",
				"status": "Active",
			}
		).insert()
		self.assertEqual(doc.name, "Peter Otieno - 33445566")

	def test_national_id_wins_over_payroll_number(self):
		employee = make_employee("CV-TEST-9007", "Both Keys")
		doc = frappe.get_doc(
			{
				"doctype": "Cova Members",
				"member_type": "Active",
				"employee": employee,
				"national_id": "99887766",
				"full_name": "Both Keys",
				"payroll_number": employee,
				"status": "Active",
			}
		).insert()
		self.assertEqual(doc.name, "Both Keys - 99887766")

	def test_national_id_is_unique(self):
		# So the suffix path below can never be reached through a shared national id.
		frappe.get_doc(
			{
				"doctype": "Cova Members",
				"member_type": "Pre Employment",
				"national_id": "55555555",
				"full_name": "First Person",
				"status": "Active",
			}
		).insert()
		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc(
				{
					"doctype": "Cova Members",
					"member_type": "Pre Employment",
					"national_id": "55555555",
					"full_name": "Second Person",
					"status": "Active",
				}
			).insert()

	def test_two_members_with_only_a_name_are_suffixed_apart(self):
		# No national id, no payroll number, no Employee — the name is just the
		# full name, so the second one has to be de-duplicated.
		for _ in range(2):
			frappe.get_doc(
				{
					"doctype": "Cova Members",
					"member_type": "Pre Employment",
					"full_name": "Same Person",
					"status": "Active",
				}
			).insert()
		names = sorted(frappe.get_all("Cova Members", filters={"full_name": "Same Person"}, pluck="name"))
		self.assertEqual(names, ["Same Person", "Same Person-1"])

	def test_a_member_with_nothing_to_name_it_after_still_inserts(self):
		doc = frappe.get_doc(
			{"doctype": "Cova Members", "member_type": "Pre Employment", "status": "Active"}
		).insert()
		self.assertTrue(doc.name)

	def test_visit_and_test_references_are_optional(self):
		doc = frappe.get_doc(
			{"doctype": "Cova Members", "member_type": "Pre Employment", "national_id": "77", "full_name": "Ann"}
		).insert()
		self.assertIsNone(doc.visit_reference)
		self.assertIsNone(doc.test_request_reference)

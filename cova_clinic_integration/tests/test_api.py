# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

"""Endpoint tests for cova_clinic_integration.api.

These drive the whitelisted functions the way an HTTP caller would — the COVA
webhooks, the dashboard reports, and the getClinicData override — by stubbing
``frappe.request``. The outbound actions (register_member and friends) are not
exercised here because they POST to the COVA API; use the Node-RED harness for
those so the payload can be inspected on the wire.
"""

from unittest.mock import patch

import frappe

from cova_clinic_integration import api
from cova_clinic_integration.testing import (
	IntegrationTestCase,
	ensure_hr_role,
	make_employee,
	stub_request,
)


class TestCovaClinicApi(IntegrationTestCase):
	"""The ``cova_clinic_api`` dispatcher — inbound actions only."""

	def setUp(self):
		self.payroll = "CV-API-8001"
		self.employee = make_employee(self.payroll, "Api Tester")

	def tearDown(self):
		frappe.db.rollback()

	def _call(self, body):
		with stub_request(json_body=body):
			return api.cova_clinic_api()

	# ── dispatcher ────────────────────────────────────────────────────
	def test_unknown_action(self):
		resp = self._call({"action": "no_such_action"})
		self.assertEqual(resp, {"error": "Unknown action: no_such_action"})

	def test_missing_action(self):
		resp = self._call({})
		self.assertIn("Unknown action", resp["error"])

	# ── receive_visit ─────────────────────────────────────────────────
	def _visit_body(self, when="2026-08-10 09:30:00", **kw):
		body = {
			"action": "receive_visit",
			"payrollNumber": self.payroll,
			"visitDateTime": when,
			"lineItems": [
				{"type": "Consultation", "amount": 500, "notes": "api test"},
				{"type": "Pharmacy", "amount": 1200, "notes": "api test"},
			],
			"benefitBalanceAfter": {
				"Consultation": 4500,
				"Pharmacy": 8800,
				"Laboratory": 3000,
				"Diagnostic": 2000,
				"Specialist": 1000,
			},
		}
		body.update(kw)
		return body

	def test_receive_visit_writes_a_clinic_visit_cost(self):
		resp = self._call(self._visit_body())
		self.assertEqual(resp["status"], "success")

		doc = frappe.get_doc("Clinic Visit Cost", resp["name"])
		self.assertEqual(doc.payroll_number, self.payroll)
		self.assertEqual(doc.employee, self.employee)
		self.assertEqual(doc.total_cost, 1700)
		self.assertEqual(len(doc.visit_line_item), 2)
		self.assertEqual(doc.benefit_pharmacy, 8800)
		# full_name feeds the autoname, so the visit is readable in a list view
		self.assertIn("Api Tester", doc.name)

	def test_receive_visit_stores_the_raw_payload(self):
		resp = self._call(self._visit_body(when="2026-08-11 09:30:00"))
		raw = frappe.db.get_value("Clinic Visit Cost", resp["name"], "cova_raw")
		self.assertIn("benefitBalanceAfter", raw)

	def test_receive_visit_accepts_the_v1_scalar_balance(self):
		resp = self._call(self._visit_body(when="2026-08-12 09:30:00", benefitBalanceAfter=9000))
		self.assertEqual(resp["status"], "success")
		doc = frappe.get_doc("Clinic Visit Cost", resp["name"])
		# A bare number has no category to land in; it survives in cova_raw only.
		self.assertEqual(doc.benefit_consultation, 0)
		self.assertIn("9000", doc.cova_raw)

	def test_receive_visit_is_deduplicated_per_day(self):
		# visit_date is a Date, so two timestamps on the same day are one visit.
		first = self._call(self._visit_body(when="2026-08-13 08:00:00"))
		self.assertEqual(first["status"], "success")
		second = self._call(self._visit_body(when="2026-08-13 16:45:00"))
		self.assertEqual(second["status"], "duplicate")

	def test_receive_visit_unknown_payroll(self):
		resp = self._call(self._visit_body(payrollNumber="CV-API-NOPE"))
		self.assertIn("Employee not found", resp["error"])

	def test_receive_visit_updates_the_cova_member(self):
		member = frappe.get_doc(
			{
				"doctype": "Cova Members",
				"member_type": "Active",
				"employee": self.employee,
				"full_name": "Api Tester",
				"payroll_number": self.payroll,
				"status": "Active",
			}
		).insert()
		resp = self._call(self._visit_body(when="2026-08-14 10:00:00"))
		member.reload()
		self.assertEqual(member.visit_reference, resp["name"])
		self.assertEqual(str(member.last_visit), "2026-08-14")

	# ── receive_test_result ───────────────────────────────────────────
	def _request(self, **kw):
		body = {
			"doctype": "Clinic Test Request",
			"member_type": "Active",
			"employee": self.employee,
			"payroll_number": self.payroll,
			"status": "Pending",
			"test_package": "Annual Medical",
			"scheduled_from": "2026-08-01",
			"scheduled_to": "2026-08-16",
		}
		body.update(kw)
		return frappe.get_doc(body).insert()

	def test_receive_test_result_grades_and_closes_the_request(self):
		req = self._request()
		resp = self._call(
			{
				"action": "receive_test_result",
				"requestId": req.name,
				"testPackage": "Annual Medical",
				"clinicalOutcome": "FitForWork",
			}
		)
		self.assertEqual(resp["status"], "success")
		self.assertEqual(resp["risk"], "Low Risk")

		req.reload()
		self.assertEqual(req.status, "Completed")
		self.assertEqual(req.linked_test_result, resp["result"])

		result = frappe.get_doc("Clinic Test Result", resp["result"])
		self.assertEqual(result.results[0].select_tezd, "Low Risk")
		self.assertEqual(result.results[0].test, "Annual Medical")

	def test_outcome_risk_map(self):
		expected = {
			"FitForWork": "Low Risk",
			"FitWithRestrictions": "Medium Risk",
			"InconclusiveRetestRequired": "Medium Risk",
			"UnfitForWork": "High Risk",
			"SomethingElse": "No Risk",
		}
		for outcome, risk in expected.items():
			req = self._request(test_package="Exit Medical")
			resp = self._call(
				{
					"action": "receive_test_result",
					"requestId": req.name,
					"testPackage": "Exit Medical",
					"clinicalOutcome": outcome,
				}
			)
			self.assertEqual(resp["risk"], risk, outcome)

	def test_receive_test_result_attaches_the_latest_visit(self):
		self._call(self._visit_body(when="2026-08-20 09:00:00"))
		latest = self._call(self._visit_body(when="2026-08-21 09:00:00"))
		req = self._request()
		resp = self._call(
			{
				"action": "receive_test_result",
				"requestId": req.name,
				"testPackage": "Annual Medical",
				"clinicalOutcome": "FitForWork",
			}
		)
		self.assertEqual(resp["visit"], latest["name"])

	def test_receive_test_result_unknown_request(self):
		resp = self._call(
			{
				"action": "receive_test_result",
				"requestId": "TR-NOPE",
				"testPackage": "Annual Medical",
				"clinicalOutcome": "FitForWork",
			}
		)
		self.assertIn("not found", resp["error"])

	# ── receive_health_report ─────────────────────────────────────────
	def test_receive_health_report_builds_the_month(self):
		resp = self._call(
			{
				"action": "receive_health_report",
				"month": "nov",
				"date": "2026-11-30",
				"cases": [
					{"condition": "Cova Api Malaria", "count": 12},
					{"condition": "Cova Api URTI", "count": 30},
				],
			}
		)
		self.assertEqual(resp["status"], "success")
		self.assertEqual(resp["total_cases"], 42)

		doc = frappe.get_doc("Health Monthly Report", resp["name"])
		self.assertEqual(doc.month, "NOV")          # upper-cased by the endpoint
		self.assertEqual(len(doc.medical_cases), 2)
		# unseen conditions are created as Medical Cases on the way through
		self.assertTrue(frappe.db.exists("Medical Case", "Cova Api Malaria"))

	def test_receive_health_report_is_deduplicated(self):
		body = {
			"action": "receive_health_report",
			"month": "DEC",
			"date": "2026-12-31",
			"cases": [{"condition": "Cova Api Flu", "count": 3}],
		}
		self.assertEqual(self._call(body)["status"], "success")
		self.assertEqual(self._call(body)["status"], "duplicate")

	def test_receive_health_report_requires_month_and_cases(self):
		self.assertIn("required", self._call({"action": "receive_health_report", "cases": []})["error"])
		self.assertIn(
			"required", self._call({"action": "receive_health_report", "month": "JAN"})["error"]
		)


class TestPayrollIdentifier(IntegrationTestCase):
	"""``payrollNumber`` is the key COVA files everything under, so what goes out
	and what comes back have to agree.

	It is the **Employee ID**. ``employee_number`` is optional in HR and was
	being sent as ``null`` wherever it was blank; on the live site the two are
	the same string, so this is a no-op there.
	"""

	def tearDown(self):
		frappe.db.rollback()

	def test_identifier_is_the_employee_id(self):
		employee = make_employee("CV-API-8100", "Payroll Ident")
		doc = frappe.get_doc("Employee", employee)
		self.assertEqual(api.employee_payroll_id(doc), employee)

	def test_identifier_survives_a_blank_employee_number(self):
		employee = make_employee("CV-API-8101", "No Number")
		frappe.db.set_value("Employee", employee, "employee_number", "")
		doc = frappe.get_doc("Employee", employee)
		self.assertEqual(api.employee_payroll_id(doc), employee)
		self.assertTrue(api.employee_payroll_id(doc))  # never null/empty

	def test_identifier_accepts_a_plain_row(self):
		employee = make_employee("CV-API-8102", "Row Form")
		row = frappe.db.get_all(
			"Employee", filters={"name": employee}, fields=["name", "employee_number"]
		)[0]
		self.assertEqual(api.employee_payroll_id(row), employee)

	def test_inbound_resolves_the_employee_id(self):
		employee = make_employee("CV-API-8103", "Round Trip")
		self.assertEqual(api.employee_from_payroll(employee), employee)

	def test_inbound_still_resolves_a_legacy_employee_number(self):
		# Rows COVA already holds are keyed by employee_number.
		employee = make_employee("CV-API-8104", "Legacy Key")
		self.assertEqual(api.employee_from_payroll("CV-API-8104"), employee)

	def test_inbound_rejects_an_unknown_key(self):
		self.assertIsNone(api.employee_from_payroll("CV-API-NOBODY"))
		self.assertIsNone(api.employee_from_payroll(""))
		self.assertIsNone(api.employee_from_payroll(None))

	def test_receive_visit_accepts_a_payload_keyed_by_employee_id(self):
		employee = make_employee("CV-API-8105", "By Employee Id")
		with stub_request(
			json_body={
				"action": "receive_visit",
				"payrollNumber": employee,          # what the app now sends out
				"visitDateTime": "2026-09-01 10:00:00",
				"lineItems": [{"type": "Consultation", "amount": 400}],
				"benefitBalanceAfter": {},
			}
		):
			resp = api.cova_clinic_api()
		self.assertEqual(resp["status"], "success")
		self.assertEqual(frappe.db.get_value("Clinic Visit Cost", resp["name"], "employee"), employee)


class TestPhoneNumber(IntegrationTestCase):
	"""``phone_number`` is stored as Data (rendered as a phone input), so any
	format saves. ``normalize_phone`` is what makes what is stored and what is
	sent to COVA agree.
	"""

	def tearDown(self):
		frappe.db.rollback()

	def test_kenyan_forms_all_reach_the_same_e164(self):
		for given in (
			"0740781289",
			"740781289",
			"254740781289",
			"+254740781289",
			"0740-781 289",
			"(0740) 781.289",
			" +254 740 781 289 ",
		):
			self.assertEqual(api.normalize_phone(given), "+254740781289", given)

	def test_an_international_number_is_left_on_its_own_country_code(self):
		self.assertEqual(api.normalize_phone("+44 7911 123456"), "+447911123456")
		self.assertEqual(api.normalize_phone("+1 (415) 555-2671"), "+14155552671")

	def test_a_foreign_number_without_its_plus_is_not_made_kenyan(self):
		# The old version prefixed +254 onto anything unprefixed.
		out = api.normalize_phone("447911123456")
		self.assertEqual(out, "447911123456")
		self.assertNotIn("+254", out)

	def test_blank_and_junk(self):
		for given in (None, "", "   ", "n/a", "--"):
			self.assertEqual(api.normalize_phone(given), "", repr(given))

	def test_saving_a_local_number_keeps_the_country_code(self):
		doc = frappe.get_doc(
			{
				"doctype": "Cova Members",
				"member_type": "Pre Employment",
				"national_id": "223344559",
				"full_name": "Phone Saver",
				"phone_number": api.normalize_phone("0740781289"),
				"status": "Active",
			}
		).insert()
		doc.reload()
		self.assertEqual(doc.phone_number, "+254740781289")

	def test_the_field_accepts_a_number_without_a_country_code(self):
		# The point of Data-over-Phone: a bare local number must still save
		# rather than being rejected for having no country code.
		doc = frappe.get_doc(
			{
				"doctype": "Cova Members",
				"member_type": "Pre Employment",
				"national_id": "223344560",
				"full_name": "No Code",
				"phone_number": "0740781289",
				"status": "Active",
			}
		).insert()
		doc.reload()
		self.assertEqual(doc.phone_number, "0740781289")

	def test_field_is_data_not_the_strict_phone_type(self):
		for doctype in ("Cova Members", "Clinic Test Request"):
			field = frappe.get_meta(doctype).get_field("phone_number")
			self.assertEqual(field.fieldtype, "Data", doctype)
			self.assertEqual(field.options, "Phone", doctype)

	def test_member_created_through_the_api_is_stored_normalised(self):
		cm = api.create_or_update_cova_member(
			"Pre Employment", "", "223344561", "Api Phone", "", "0740781289", "Male"
		)
		self.assertEqual(frappe.db.get_value("Cova Members", cm, "phone_number"), "+254740781289")


class TestGender(IntegrationTestCase):
	"""``gender`` links to the Gender doctype, the same way ``Employee.gender``
	does, rather than carrying its own hardcoded Male/Female list."""

	def tearDown(self):
		frappe.db.rollback()

	def test_fields_link_to_the_gender_doctype(self):
		for doctype, fieldname in (
			("Cova Members", "gender"),
			("Clinic Test Request", "gender"),
			("Job Offer", "custom_gender"),
		):
			field = frappe.get_meta(doctype).get_field(fieldname)
			self.assertIsNotNone(field, "%s.%s missing" % (doctype, fieldname))
			self.assertEqual(field.fieldtype, "Link", doctype)
			self.assertEqual(field.options, "Gender", doctype)

	def _member(self, gender, national_id):
		return frappe.get_doc(
			{
				"doctype": "Cova Members",
				"member_type": "Pre Employment",
				"national_id": national_id,
				"full_name": "Gender Case",
				"gender": gender,
				"status": "Active",
			}
		).insert()

	def test_any_gender_record_is_accepted(self):
		# The old Select allowed only Male/Female; the link opens up whatever the
		# site actually has on the Gender doctype.
		for i, gender in enumerate(frappe.get_all("Gender", pluck="name")[:4]):
			doc = self._member(gender, "9000000%d" % i)
			self.assertEqual(doc.gender, gender)

	def test_blank_gender_is_allowed(self):
		doc = self._member("", "90000090")
		self.assertFalse(doc.gender)

	def test_an_unknown_gender_is_rejected(self):
		with self.assertRaises(frappe.exceptions.LinkValidationError):
			self._member("M", "90000092")

	def test_a_padded_gender_never_reaches_the_column(self):
		# "Male " is not a Gender, but whether it is *rejected* is not the app's
		# call: MariaDB's collation is PAD SPACE, so on frappe v16 the link
		# resolves to the real record and the field is rewritten to its name,
		# while v17's bulk link prefetch matches in python and throws. Either
		# outcome is fine — the padded string must simply never be stored.
		try:
			doc = self._member("Male ", "90000091")
		except frappe.exceptions.LinkValidationError:
			return
		self.assertEqual(doc.gender, "Male")

	def test_gender_survives_the_member_helper(self):
		cm = api.create_or_update_cova_member(
			"Pre Employment", "", "90000093", "Helper Gender", "", "0740781289", "Female"
		)
		self.assertEqual(frappe.db.get_value("Cova Members", cm, "gender"), "Female")

	def test_employee_gender_flows_through_unchanged(self):
		# Employee.gender is already a Gender link, so the value the outbound
		# payload carries is a valid Gender name with no translation needed.
		employee = make_employee("CV-API-8200", "Gender Flow")
		self.assertEqual(frappe.db.get_value("Employee", employee, "gender"), "Male")
		cm = api.create_or_update_cova_member(
			"Active", employee, "", "Gender Flow", employee, "", "Male"
		)
		self.assertEqual(frappe.db.get_value("Cova Members", cm, "gender"), "Male")


class TestReactivation(IntegrationTestCase):
	"""Deactivate on exit, then register the same person again — a rehire, or a
	deactivation done by mistake — and the membership has to come back.

	The COVA call is mocked, so these cover the whole action without leaving the
	machine.
	"""

	COVA_OK = {"status": "registered", "covaMemberId": "COVA-TEST-1"}

	def setUp(self):
		self.payroll = "CV-API-8300"
		self.employee = make_employee(self.payroll, "Rehire Case")
		settings = frappe.get_single("Cova Clinic Settings")
		settings.base_url = "https://cova.invalid"
		settings.register_endpoint = "/members/register"
		settings.deactivation_endpoint = "/members/deactivate"
		settings.save(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()

	def _call(self, body, reply=None):
		with patch.object(api, "make_post_request", return_value=reply or self.COVA_OK) as m:
			with stub_request(json_body=body):
				return api.cova_clinic_api(), m

	def _register(self):
		return self._call(
			{"action": "register_member", "member_type": "Active", "employee": self.employee}
		)

	def _deactivate(self):
		return self._call(
			{
				"action": "deactivate_member",
				"employee": self.employee,
				"exit_date": "2026-08-31",
				"requires_exit_medical": False,
			},
			reply={"status": "deactivated"},
		)

	def _member(self):
		name = frappe.db.get_value("Cova Members", {"employee": self.employee}, "name")
		return frappe.get_doc("Cova Members", name) if name else None

	def test_deactivate_then_register_returns_the_member_to_active(self):
		self._register()
		self.assertEqual(self._member().status, "Active")

		self._deactivate()
		self.assertEqual(self._member().status, "Inactive")

		self._register()
		self.assertEqual(self._member().status, "Active")

	def test_reactivation_reuses_the_same_member_row(self):
		self._register()
		first = self._member().name
		self._deactivate()
		self._register()
		self.assertEqual(self._member().name, first)
		self.assertEqual(
			frappe.db.count("Cova Members", {"employee": self.employee}), 1
		)

	def test_registering_again_clears_the_employee_deactivation_flag(self):
		self._register()
		self._deactivate()
		self.assertEqual(frappe.db.get_value("Employee", self.employee, "cova_deactivated"), 1)

		self._register()
		self.assertEqual(frappe.db.get_value("Employee", self.employee, "cova_deactivated"), 0)

	def test_a_failed_registration_does_not_clear_the_flag(self):
		self._register()
		self._deactivate()
		self._call(
			{"action": "register_member", "member_type": "Active", "employee": self.employee},
			reply={"error": "cova rejected it"},
		)
		self.assertEqual(frappe.db.get_value("Employee", self.employee, "cova_deactivated"), 1)
		self.assertEqual(self._member().status, "Inactive")

	def test_reactivation_refreshes_the_mutable_fields(self):
		self._register()
		member = self._member()
		member.phone_number = ""
		member.gender = ""
		member.save(ignore_permissions=True)

		frappe.db.set_value("Employee", self.employee, "cell_number", "0740781289")
		self._deactivate()
		self._register()

		member = self._member()
		self.assertEqual(member.phone_number, "+254740781289")
		self.assertEqual(member.gender, "Male")

	def test_reactivation_does_not_wipe_a_stored_national_id(self):
		# An Active registration passes national_id="" — it must not clear a value
		# captured earlier during pre-employment.
		self._register()
		member = self._member()
		member.national_id = "77665544"
		member.save(ignore_permissions=True)

		self._deactivate()
		self._register()
		self.assertEqual(self._member().national_id, "77665544")

	def test_pre_employment_reactivation_keeps_the_payroll_number(self):
		# The mirror: a pre-employment registration passes payroll_number="".
		created = api.create_or_update_cova_member(
			"Pre Employment", "", "88776655", "Pre Rehire", "PAYROLL-1", "0740781289", "Male"
		)
		frappe.db.set_value("Cova Members", created, "status", "Inactive")

		again = api.create_or_update_cova_member(
			"Pre Employment", "", "88776655", "Pre Rehire", "", "0740781289", "Male"
		)
		self.assertEqual(again, created)
		doc = frappe.get_doc("Cova Members", created)
		self.assertEqual(doc.status, "Active")
		self.assertEqual(doc.payroll_number, "PAYROLL-1")

	def test_the_outbound_payload_carries_the_employee_id(self):
		_resp, mock = self._register()
		payload = mock.call_args.kwargs["json"]
		self.assertEqual(payload["payrollNumber"], self.employee)
		self.assertEqual(payload["schemeType"], "Active")


class TestConnections(IntegrationTestCase):
	"""Connections (DocType Link rows) are what make a record traceable from the
	forms around it. A typo in link_fieldname produces an empty tab rather than an
	error, so every declared connection is resolved here."""

	APP_DOCTYPES = (
		"Cova Members",
		"Clinic Test Request",
		"Clinic Test Result",
		"Clinic Visit Cost",
		"Clinic Checkin",
		"Health Monthly Report",
		"Medical Case",
	)

	def test_every_declared_connection_resolves(self):
		checked = 0
		for doctype in self.APP_DOCTYPES + ("Employee",):
			for link in frappe.get_meta(doctype).links:
				where = "%s -> %s.%s" % (doctype, link.link_doctype, link.link_fieldname)

				# a child-table connection points at the child, reached through a
				# table field on the parent
				if link.is_child_table:
					self.assertTrue(link.parent_doctype, where + " (no parent_doctype)")
					parent_meta = frappe.get_meta(link.parent_doctype)
					table_field = parent_meta.get_field(link.table_fieldname)
					self.assertIsNotNone(table_field, where + " (bad table_fieldname)")
					self.assertEqual(table_field.options, link.link_doctype, where)

				field = frappe.get_meta(link.link_doctype).get_field(link.link_fieldname)
				self.assertIsNotNone(field, where + " (field does not exist)")
				self.assertEqual(field.fieldtype, "Link", where + " (not a Link field)")
				self.assertEqual(field.options, doctype, where + " (points elsewhere)")
				checked += 1

		self.assertGreaterEqual(checked, 15, "expected the full set of connections")

	def test_employee_reaches_every_clinic_record(self):
		links = {l.link_doctype for l in frappe.get_meta("Employee").links}
		for doctype in (
			"Cova Members",
			"Clinic Visit Cost",
			"Clinic Test Request",
			"Clinic Test Result",
			"Clinic Checkin",
		):
			self.assertIn(doctype, links, doctype)

	def test_both_sides_of_clinic_checkin_are_reachable(self):
		# sick-off records hang off `employee`, biometric punches off `b_employee`
		fieldnames = {
			l.link_fieldname for l in frappe.get_meta("Employee").links
			if l.link_doctype == "Clinic Checkin"
		}
		self.assertEqual(fieldnames, {"employee", "b_employee"})

	def test_request_and_result_are_linked_both_ways(self):
		# request_id used to be a plain Data field, which broke Request -> Result
		field = frappe.get_meta("Clinic Test Result").get_field("request_id")
		self.assertEqual(field.fieldtype, "Link")
		self.assertEqual(field.options, "Clinic Test Request")

		forward = {l.link_doctype for l in frappe.get_meta("Clinic Test Request").links}
		self.assertIn("Clinic Test Result", forward)
		back = {l.link_doctype for l in frappe.get_meta("Clinic Test Result").links}
		self.assertIn("Clinic Test Request", back)

	def test_medical_case_reaches_results_and_reports(self):
		links = {l.link_doctype: l for l in frappe.get_meta("Medical Case").links}
		self.assertIn("Test Result", links)
		self.assertIn("Health Report", links)
		self.assertEqual(links["Test Result"].parent_doctype, "Clinic Test Result")
		self.assertEqual(links["Health Report"].parent_doctype, "Health Monthly Report")

	def test_no_duplicate_connections(self):
		# What a non-idempotent installer would leave behind. Asserted as state
		# rather than by re-running install_clinic_links(), which commits and
		# saves a standard doctype — not something to do inside a test.
		for doctype in self.APP_DOCTYPES + ("Employee",):
			keys = [
				(l.link_doctype, l.link_fieldname, l.table_fieldname or "")
				for l in frappe.get_meta(doctype).links
			]
			self.assertEqual(len(keys), len(set(keys)), "duplicate connections on " + doctype)


class TestCovaMemberLinks(IntegrationTestCase):
	"""Each per-member doctype carries a ``cova_member`` link so the member form
	can show its own history — Connections is reverse-only, and Cova Members'
	own reference fields all point outwards.
	"""

	def setUp(self):
		self.payroll = "CV-API-8400"
		self.employee = make_employee(self.payroll, "Member Link")
		self.member = api.create_or_update_cova_member(
			"Active", self.employee, "", "Member Link", self.payroll, "0740781289", "Male"
		)

	def tearDown(self):
		frappe.db.rollback()

	def test_the_member_is_reachable_from_every_per_member_doctype(self):
		links = {l.link_doctype: l for l in frappe.get_meta("Cova Members").links}
		for doctype in (
			"Clinic Visit Cost",
			"Clinic Test Request",
			"Clinic Test Result",
			"Clinic Checkin",
		):
			self.assertIn(doctype, links, doctype)
			self.assertEqual(links[doctype].link_fieldname, "cova_member", doctype)

	def test_aggregate_doctypes_are_deliberately_not_linked(self):
		# Health Monthly Report spans everyone and Medical Case is a catalogue of
		# conditions — neither belongs to a member.
		for doctype in ("Health Monthly Report", "Medical Case"):
			self.assertIsNone(
				frappe.get_meta(doctype).get_field("cova_member"), doctype
			)

	def test_a_visit_links_itself_to_the_member(self):
		doc = frappe.get_doc(
			{
				"doctype": "Clinic Visit Cost",
				"employee": self.employee,
				"full_name": "Member Link",
				"payroll_number": self.payroll,
				"visit_date": "2026-09-10",
			}
		).insert()
		self.assertEqual(doc.cova_member, self.member)

	def test_a_request_links_itself_to_the_member(self):
		doc = frappe.get_doc(
			{
				"doctype": "Clinic Test Request",
				"member_type": "Active",
				"employee": self.employee,
				"status": "Pending",
				"test_package": "Annual Medical",
				"scheduled_from": "2026-09-01",
				"scheduled_to": "2026-09-16",
			}
		).insert()
		self.assertEqual(doc.cova_member, self.member)

	def test_a_biometric_punch_links_through_b_employee(self):
		doc = frappe.get_doc(
			{
				"doctype": "Clinic Checkin",
				"b_employee": self.employee,
				"log_type": "IN",
				"time": "2026-09-10 08:05:00",
			}
		).insert()
		self.assertEqual(doc.cova_member, self.member)

	def test_a_manual_link_is_not_overwritten(self):
		other = api.create_or_update_cova_member(
			"Pre Employment", "", "31313131", "Someone Else", "", "", "Male"
		)
		doc = frappe.get_doc(
			{
				"doctype": "Clinic Visit Cost",
				"employee": self.employee,
				"visit_date": "2026-09-11",
				"cova_member": other,
			}
		).insert()
		self.assertEqual(doc.cova_member, other)

	def test_a_record_with_no_member_is_left_unlinked(self):
		stranger = make_employee("CV-API-8401", "No Member")
		doc = frappe.get_doc(
			{"doctype": "Clinic Visit Cost", "employee": stranger, "visit_date": "2026-09-12"}
		).insert()
		self.assertIsNone(doc.cova_member)

	def test_records_created_before_the_member_are_backfilled(self):
		# register_preemployment_candidate inserts the Clinic Test Request first
		# and only then creates the member, so validate had nothing to resolve.
		national_id = "42424242"
		req = frappe.get_doc(
			{
				"doctype": "Clinic Test Request",
				"member_type": "Pre Employment",
				"nationa_id": national_id,
				"full_name": "Backfill Me",
				"status": "Pending",
				"test_package": "Pre Employment Wellness",
				"scheduled_from": "2026-09-01",
				"scheduled_to": "2026-09-16",
			}
		).insert()
		self.assertIsNone(req.cova_member)

		member = api.create_or_update_cova_member(
			"Pre Employment", "", national_id, "Backfill Me", "", "0740781289", "Male"
		)
		req.reload()
		self.assertEqual(req.cova_member, member)

	def test_receive_visit_links_the_member(self):
		with stub_request(
			json_body={
				"action": "receive_visit",
				"payrollNumber": self.employee,
				"visitDateTime": "2026-09-13 09:00:00",
				"lineItems": [{"type": "Consultation", "amount": 400}],
				"benefitBalanceAfter": {},
			}
		):
			resp = api.cova_clinic_api()
		self.assertEqual(resp["status"], "success")
		self.assertEqual(
			frappe.db.get_value("Clinic Visit Cost", resp["name"], "cova_member"), self.member
		)


class TestClinicReports(IntegrationTestCase):
	"""The dashboard endpoints. All of them gate on an HR role."""

	def setUp(self):
		ensure_hr_role()

	def tearDown(self):
		frappe.db.rollback()

	def _report(self, fn, body=None):
		with stub_request(json_body=body or {}):
			return fn()

	def test_disease_report_shape(self):
		out = self._report(api.clinic_disease_report, {"year": "2026", "month": ""})
		for key in ("months", "rows", "grand_total", "trend", "filter_options"):
			self.assertIn(key, out)

	def test_checkin_report_shape(self):
		out = self._report(api.clinic_checkin_report, {"year": "2026"})
		self.assertIsInstance(out, dict)

	def test_visit_cost_report_shape(self):
		out = self._report(api.clinic_visit_cost_report, {"year": "2026"})
		self.assertIsInstance(out, dict)

	def test_test_request_report_shape(self):
		out = self._report(api.clinic_test_request_report, {"year": "2026"})
		self.assertIsInstance(out, dict)

	def test_test_result_report_shape(self):
		out = self._report(api.clinic_test_result_report, {"year": "2026"})
		self.assertIsInstance(out, dict)

	def test_bad_date_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._report(api.clinic_checkin_report, {"from_date": "not-a-date"})

	def test_access_is_denied_without_an_hr_role(self):
		user = "cova-no-hr@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{"doctype": "User", "email": user, "first_name": "No", "last_name": "Hr"}
			).insert(ignore_permissions=True)
		frappe.set_user(user)
		try:
			self.assertFalse(api.has_health_report_access())
			with self.assertRaises(frappe.PermissionError):
				api.assert_health_report_access()
		finally:
			frappe.set_user("Administrator")


class TestClinicReportXlsx(IntegrationTestCase):
	"""One definition only — api.py used to declare clinic_report_xlsx twice, so
	the first (with the sheet-name sanitising) was dead code."""

	def setUp(self):
		ensure_hr_role()

	def tearDown(self):
		frappe.db.rollback()

	def test_rows_become_a_workbook(self):
		rows = [["Employee", "Visits", "Cost"], ["EMP-1", 3, 4200]]
		with stub_request(form={"title": "Harness", "rows": frappe.as_json(rows)}) as response:
			api.clinic_report_xlsx()
		self.assertTrue(response.filename.endswith(".xlsx"))
		self.assertEqual(response.filecontent[:2], b"PK")

	def test_a_title_with_a_colon_is_sanitised(self):
		# Excel rejects : \ / ? * [ ] in a sheet name; make_xlsx raises without this.
		rows = [["A", 1]]
		with stub_request(form={"title": "Visits: 2026", "rows": frappe.as_json(rows)}) as response:
			api.clinic_report_xlsx()
		self.assertEqual(response.filecontent[:2], b"PK")

	def test_empty_rows_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			with stub_request(form={"title": "Empty", "rows": "[]"}):
				api.clinic_report_xlsx()


class TestGetClinicData(IntegrationTestCase):
	"""The ``getClinicData`` override: GET reads sick-off rows by creation date,
	POST writes a Clinic Checkin. Unlike the live Server Script it needs only
	``employee`` and does not block duplicates."""

	def setUp(self):
		self.employee = make_employee("CV-API-8002", "Clinic Data")

	def tearDown(self):
		# get_clinic_data's POST branch calls frappe.db.commit() itself, so the
		# usual rollback does not undo the check-ins these tests create — they
		# have to be deleted explicitly or they accumulate on the site.
		for name in frappe.get_all(
			"Clinic Checkin", filters={"employee": self.employee}, pluck="name"
		):
			frappe.delete_doc("Clinic Checkin", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		frappe.db.rollback()

	# The GET branch catches its own throws and answers with status=error/500
	# rather than letting the exception out, so these assert on the response.
	def test_get_requires_both_dates(self):
		with stub_request(method="GET", args={}) as response:
			api.get_clinic_data()
		self.assertEqual(response.status, "error")
		self.assertIn("Missing required query parameters", response.message)
		self.assertEqual(response.http_status_code, 500)

	def test_get_rejects_a_reversed_range(self):
		with stub_request(
			method="GET", args={"start_date": "2026-08-31", "end_date": "2026-08-01"}
		) as response:
			api.get_clinic_data()
		self.assertEqual(response.status, "error")
		self.assertIn("cannot be before", response.message)

	def test_get_returns_records(self):
		with stub_request(method="GET", args={"start_date": "2026-01-01", "end_date": "2026-12-31"}) as response:
			api.get_clinic_data()
		self.assertEqual(response.status, "success")
		self.assertIn("data", response)

	def test_post_employee_only(self):
		with stub_request(json_body={"employee": self.employee, "reason": "walk-in"}) as response:
			api.get_clinic_data()
		self.assertEqual(response.status, "success")
		self.assertEqual(response.http_status_code, 201)
		# no dates -> no Leave Application
		self.assertIsNone(response.data["leave_application"])

	def test_post_requires_an_employee(self):
		with stub_request(json_body={"reason": "nobody"}) as response:
			api.get_clinic_data()
		self.assertEqual(response.status, "error")
		self.assertIn("Missing required field", response.message)

	def test_post_rejects_an_unknown_employee(self):
		with stub_request(json_body={"employee": "CV-API-NOPE"}) as response:
			api.get_clinic_data()
		self.assertEqual(response.status, "error")
		self.assertIn("does not exist", response.message)

	def test_post_rejects_a_reversed_range(self):
		with stub_request(
			json_body={"employee": self.employee, "start_date": "2026-08-31", "end_date": "2026-08-01"}
		) as response:
			api.get_clinic_data()
		self.assertEqual(response.status, "error")
		self.assertIn("cannot be before", response.message)

	def test_post_requires_json(self):
		with self.assertRaises(frappe.ValidationError):
			with stub_request(json_body={"employee": self.employee}, content_type="text/plain"):
				api.get_clinic_data()

	def test_post_allows_duplicates(self):
		body = {"employee": self.employee, "start_date": "2026-08-05", "end_date": "2026-08-05"}
		names = []
		for _ in range(2):
			with stub_request(json_body=dict(body)) as response:
				api.get_clinic_data()
			self.assertEqual(response.status, "success")
			names.append(response.data["id"])
		self.assertNotEqual(names[0], names[1])

	def test_other_methods_are_rejected(self):
		with stub_request(method="PUT") as response:
			api.get_clinic_data()
		self.assertEqual(response.http_status_code, 405)

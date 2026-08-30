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
			("Job Offer", "gender"),
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

	def test_clinic_checkin_is_reachable_from_the_employee(self):
		"""Both payloads name their person in `employee` now, so one link covers
		them. There used to be a second field for punches, which meant a row
		loaded into the wrong column silently changed meaning."""
		fieldnames = {
			l.link_fieldname for l in frappe.get_meta("Employee").links
			if l.link_doctype == "Clinic Checkin"
		}
		self.assertEqual(fieldnames, {"employee"})

	def test_clinic_checkin_has_one_employee_field(self):
		meta = frappe.get_meta("Clinic Checkin")
		self.assertIsNone(meta.get_field("b_employee"), "the second employee field is back")
		self.assertIsNotNone(meta.get_field("employee"))

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

	def test_a_biometric_punch_links_through_employee(self):
		doc = frappe.get_doc(
			{
				"doctype": "Clinic Checkin",
				"employee": self.employee,
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


class TestPortability(IntegrationTestCase):
	"""Regressions for code that assumed the live kaitet schema.

	The integration is written against kaitet-group.upande.com, where csf_ke
	supplies ``Employee.national_id`` and a Workflow supplies
	``Leave Application.workflow_state``. Neither is guaranteed on a site that
	only has frappe + erpnext + hrms + this app, and each one used to take a
	whole code path down with an "Unknown column" OperationalError.
	"""

	def tearDown(self):
		frappe.db.rollback()

	def test_sync_members_does_not_select_csf_ke_columns(self):
		"""sync_members must not name any Employee column beyond `name`.

		It re-reads every row with frappe.get_doc anyway, so selecting more was
		pure risk: `national_id` is owned by csf_ke and its absence made the
		whole action die before a single member was synced.
		"""
		captured = []
		real_get_all = frappe.db.get_all

		def spy(doctype, *args, **kwargs):
			# Only the action's own two sweeps, not the link-title lookups frappe
			# fires while saving the documents underneath.
			filters = kwargs.get("filters") or {}
			if doctype == "Employee" and "cova_member_id" in filters:
				captured.append(kwargs)
			return real_get_all(doctype, *args, **kwargs)

		with patch.object(api, "make_post_request", return_value={"covaMemberId": "CV-1"}):
			with patch.object(frappe.db, "get_all", side_effect=spy):
				with stub_request(json_body={"action": "sync_members"}):
					resp = api.cova_clinic_api()

		self.assertEqual(resp["status"], "sync complete")
		self.assertEqual(len(captured), 2, "expected the register + deactivate sweeps")
		for kwargs in captured:
			self.assertNotIn(
				"fields", kwargs, "sync_members selected Employee columns it does not use"
			)
			self.assertEqual(kwargs.get("pluck"), "name")

	def test_sick_leave_survives_a_site_without_workflow_state(self):
		"""The Leave Application link-back must be written even where
		`workflow_state` has no column."""
		employee = make_employee("CV-PORT-9002", "Portability Tester")
		checkin = frappe.get_doc(
			{
				"doctype": "Clinic Checkin",
				"employee": employee,
				"start_date": "2026-08-03",
				"end_date": "2026-08-04",
				"reason": "portability",
			}
		)

		real_has_column = frappe.db.has_column

		def no_workflow_state(doctype, column):
			if doctype == "Leave Application" and column == "workflow_state":
				return False
			return real_has_column(doctype, column)

		with patch.object(frappe.db, "has_column", side_effect=no_workflow_state):
			checkin.insert(ignore_permissions=True)

		# The leave itself needs a Leave Type / allocation / holiday list this
		# site may not have, so assert on what is in our control: the guard is
		# consulted and nothing raised out of the controller.
		self.assertTrue(frappe.db.exists("Clinic Checkin", checkin.name))


class TestJobOfferBiodata(IntegrationTestCase):
	"""Job Offer must carry every COVA field the Job Applicant block used to
	hold, and must not make HR retype what the applicant already supplied."""

	# fieldname -> the Job Applicant field it replaces (None = standard on
	# Job Offer already, or new to the offer).
	COVA_BLOCK = {
		"national_id": "custom_national_id",
		"date_of_birth": "custom_date_of_birth",
		"gender": "custom_gender",
		"cova_registered": "custom_cova_registered",
		"cova_tested": "custom_cova_tested",
		"linked_test_result": "custom_linked_test_result",
		"phone_number": "phone_number",
	}

	def test_every_applicant_field_has_a_home_on_job_offer(self):
		meta = frappe.get_meta("Job Offer")
		for fieldname in self.COVA_BLOCK:
			self.assertTrue(meta.has_field(fieldname), f"Job Offer is missing {fieldname}")
		# custom_company had no counterpart to install: Job Offer ships one.
		self.assertTrue(meta.has_field("company"))
		# Everything the app owns has to survive submit — registration happens
		# after the offer goes out.
		for fieldname in self.COVA_BLOCK:
			self.assertEqual(
				meta.get_field(fieldname).allow_on_submit, 1, f"{fieldname} is not allow_on_submit"
			)

	def test_phone_number_is_carried_over_from_the_applicant(self):
		field = frappe.get_meta("Job Offer").get_field("phone_number")
		self.assertEqual(field.fetch_from, "job_applicant.phone_number")
		# fetch_if_empty keeps it editable: it is mandatory for the clinic
		# company, so an applicant with no number on file must still be fillable.
		self.assertEqual(field.fetch_if_empty, 1)

	def test_gender_is_a_gender_link_not_a_hardcoded_select(self):
		field = frappe.get_meta("Job Offer").get_field("gender")
		self.assertEqual(field.fieldtype, "Link")
		self.assertEqual(field.options, "Gender")

	def test_tracking_fields_do_not_survive_an_amend(self):
		meta = frappe.get_meta("Job Offer")
		for fieldname in ("cova_registered", "cova_tested", "linked_test_result"):
			self.assertEqual(meta.get_field(fieldname).no_copy, 1, f"{fieldname} is not no_copy")


class TestCovaPostFailures(IntegrationTestCase):
	"""COVA rejecting a row must be reported, not raised.

	``frappe.integrations.utils.make_post_request`` calls ``raise_for_status()``,
	so a 400 became an exception and the response body — the only place COVA says
	*why* — was discarded. Every call site checks ``result.get("error")``, a
	contract that could therefore never be met on failure: one bad row took the
	whole bulk sweep down with a raw traceback.
	"""

	def setUp(self):
		self.payroll = "CV-POST-7001"
		self.employee = make_employee(self.payroll, "Post Tester")

	def tearDown(self):
		frappe.db.rollback()

	@staticmethod
	def _http_error(status_code, body):
		"""A requests.HTTPError shaped like the one make_post_request re-raises."""
		from requests import HTTPError, Response

		response = Response()
		response.status_code = status_code
		response._content = frappe.as_json(body).encode() if isinstance(body, dict) else body.encode()
		response.headers["content-type"] = "application/json"
		return HTTPError(f"{status_code} Client Error", response=response)

	def _call(self, body):
		with stub_request(json_body=body):
			return api.cova_clinic_api()

	def test_cova_post_returns_the_reason_instead_of_raising(self):
		error = self._http_error(400, {"message": "phone is required"})
		with patch.object(api, "make_post_request", side_effect=error):
			result = api.cova_post("https://cova.example/members/register", {}, {"payrollNumber": "x"})

		self.assertEqual(result["error"], "phone is required")
		self.assertEqual(result["status_code"], 400)
		self.assertEqual(result["cova_response"], {"message": "phone is required"})

	def test_cova_post_survives_a_body_that_is_not_json(self):
		error = self._http_error(502, "<html>Bad Gateway</html>")
		error.response.headers["content-type"] = "text/html"
		with patch.object(api, "make_post_request", side_effect=error):
			result = api.cova_post("https://cova.example/members/register", {}, {})

		self.assertEqual(result["status_code"], 502)
		self.assertIn("Bad Gateway", result["cova_response"])

	def test_cova_post_survives_a_connection_failure(self):
		"""A timeout or DNS failure carries no response at all."""
		with patch.object(api, "make_post_request", side_effect=OSError("connection refused")):
			result = api.cova_post("https://cova.example/members/register", {}, {})

		self.assertIn("connection refused", result["error"])
		self.assertIsNone(result["status_code"])

	def test_register_member_reports_a_rejection_rather_than_raising(self):
		error = self._http_error(400, {"message": "phone is required"})
		with patch.object(api, "make_post_request", side_effect=error):
			resp = self._call(
				{"action": "register_member", "member_type": "Active", "employee": self.employee}
			)

		self.assertEqual(resp["error"], "phone is required")
		# A rejected registration must not leave the member looking enrolled.
		status = frappe.db.get_value("Cova Members", {"employee": self.employee}, "status")
		self.assertEqual(status, "Inactive")

	def test_submit_test_request_reports_a_rejection(self):
		request = frappe.get_doc(
			{
				"doctype": "Clinic Test Request",
				"member_type": "Active",
				"employee": self.employee,
				"payroll_number": self.payroll,
				"status": "Pending",
				"test_package": "Annual Medical",
				"scheduled_from": "2026-08-01",
				"scheduled_to": "2026-08-15",
			}
		).insert(ignore_permissions=True)

		error = self._http_error(422, {"error": "unknown test package"})
		with patch.object(api, "make_post_request", side_effect=error):
			resp = self._call({"action": "submit_test_request", "request_name": request.name})

		self.assertEqual(resp["error"], "unknown test package")

	def test_sync_members_counts_a_rejection_with_its_reason(self):
		error = self._http_error(400, {"message": "nationalId is required"})
		with patch.object(api, "make_post_request", side_effect=error):
			resp = self._call({"action": "sync_members"})

		self.assertEqual(resp["status"], "sync complete")
		self.assertEqual(resp["registered"], 0)
		self.assertGreater(resp["register_failed"], 0)
		self.assertGreater(resp["failure_count"], 0)
		self.assertEqual(resp["failures"][0]["reason"], "nationalId is required")

	def test_sync_members_counts_a_duplicate_apart_from_a_registration(self):
		"""COVA answers an already-enrolled member with a 200 and status=duplicate.
		Counting that as a fresh registration overstates what the sweep did."""
		duplicate = {
			"status": "duplicate",
			"message": "Member is already enrolled.",
			"covaMemberId": "CHSC00015643",
		}
		with patch.object(api, "make_post_request", return_value=duplicate):
			resp = self._call({"action": "sync_members"})

		self.assertEqual(resp["registered"], 0)
		self.assertGreater(resp["duplicates"], 0)
		self.assertEqual(resp["register_failed"], 0)
		# It is still a member — COVA handed back the id.
		self.assertEqual(
			frappe.db.get_value("Employee", self.employee, "cova_member_id"), "CHSC00015643"
		)

	def test_preemployment_registration_reports_a_rejection(self):
		error = self._http_error(400, {"message": "dateOfBirth is required"})
		applicant = frappe.get_doc(
			{
				"doctype": "Job Applicant",
				"applicant_name": "Rejected Candidate",
				"email_id": "rejected.candidate@example.com",
				"phone_number": "0700111222",
				"status": "Open",
			}
		).insert(ignore_permissions=True)
		offer = frappe.get_doc(
			{
				"doctype": "Job Offer",
				"job_applicant": applicant.name,
				"status": "Awaiting Response",
				"offer_date": frappe.utils.nowdate(),
				"designation": frappe.db.get_value("Designation", {}, "name"),
				"company": frappe.db.get_value("Employee", self.employee, "company"),
				"national_id": "CV-POST-NID-1",
				"phone_number": "0700111222",
			}
		).insert(ignore_permissions=True)

		with patch.object(api, "make_post_request", side_effect=error):
			resp = self._call({"action": "register_preemployment_candidate", "job_offer": offer.name})

		# The tracking request is created first on purpose, so it survives a
		# refusal and the candidate is not silently lost.
		self.assertTrue(frappe.db.exists("Clinic Test Request", resp["test_request"]))
		self.assertEqual(resp["registration"]["error"], "dateOfBirth is required")


class TestOnboarding(IntegrationTestCase):
	"""The Getting Started panel has to cover the sidebar, not a subset of it.

	A step whose path points at nothing, or a sidebar entry with no step, both
	fail silently in the UI — the panel just quietly does less than it claims.
	"""

	ONBOARDING = "Cova Clinic Onboarding"
	TOUR = "Cova Pre-Employment on Job Offer"

	def _steps(self):
		onboarding = frappe.get_doc("Module Onboarding", self.ONBOARDING)
		return [frappe.get_doc("Onboarding Step", row.step) for row in onboarding.steps]

	# Configured once when the app is installed, not part of the day-to-day walk,
	# so it was dropped from the onboarding panel and is exempt here. Its step
	# still exists as a Form Tour — see test_the_settings_step_walks_the_single.
	NOT_ONBOARDED = {"Cova Clinic Settings"}

	def test_every_sidebar_doctype_has_a_step(self):
		sidebar = frappe.get_doc("Workspace Sidebar", "Cova Clinic")
		linked = {
			item.link_to
			for item in sidebar.items
			if item.link_type == "DocType" and item.link_to not in self.NOT_ONBOARDED
		}

		covered = set()
		for step in self._steps():
			if step.action in ("Update Settings", "Show Form Tour", "Create Entry"):
				covered.add(step.reference_document)
			elif (step.path or "").startswith("List/"):
				covered.add(step.path.split("/", 1)[1])

		self.assertEqual(
			linked - covered, set(), "sidebar doctypes with no onboarding step: %s" % (linked - covered)
		)

	def test_every_step_points_somewhere_real(self):
		for step in self._steps():
			where = f"{step.name} ({step.action})"
			if step.action in ("Update Settings", "Show Form Tour"):
				self.assertTrue(step.reference_document, where + " has no reference_document")
				self.assertTrue(
					frappe.db.exists("DocType", step.reference_document),
					where + f" points at a missing doctype {step.reference_document}",
				)
			elif step.action == "Go to Page":
				self.assertTrue(step.path, where + " has no path")
				if step.path.startswith("List/"):
					doctype = step.path.split("/", 1)[1]
					self.assertTrue(
						frappe.db.exists("DocType", doctype),
						where + f" lists a missing doctype {doctype}",
					)
			elif step.action == "View Docs":
				self.assertTrue(step.path, where + " has no path")

	def test_the_job_offer_step_walks_the_form_then_creates_one(self):
		step = frappe.get_doc("Onboarding Step", "Fill the COVA Block on a Job Offer")
		self.assertEqual(step.action, "Create Entry")
		self.assertEqual(step.reference_document, "Job Offer")
		self.assertEqual(step.form_tour, self.TOUR)
		# createEntry() only starts the tour and only routes to the full form when
		# both of these are set; with either missing you land on a blank form with
		# no walkthrough and nothing tells you.
		self.assertEqual(step.show_form_tour, 1)
		self.assertEqual(step.show_full_form, 1)

	def test_every_doctype_step_walks_a_tour_and_ends_in_a_record(self):
		"""A step that opens a doctype must run its walkthrough, and the record it
		opens must be one a person can actually save — otherwise the walkthrough
		dead-ends on a form that will not complete."""
		for step in self._steps():
			if step.action != "Create Entry":
				continue
			where = step.name
			self.assertTrue(step.form_tour, where + " opens a form with no walkthrough")
			self.assertEqual(step.show_form_tour, 1, where + ": show_form_tour is off, the tour never starts")
			self.assertEqual(step.show_full_form, 1, where + ": show_full_form is off")

			tour = frappe.get_doc("Form Tour", step.form_tour)
			self.assertEqual(tour.reference_doctype, step.reference_document)

			# reqd + read_only is unfillable — the form demands a value the user
			# cannot type, so the walkthrough can never be completed.
			meta = frappe.get_meta(step.reference_document)
			unfillable = [
				f.fieldname
				for f in meta.fields
				if f.reqd and f.read_only and not f.default and not f.fetch_from
			]
			self.assertEqual(
				unfillable, [], f"{step.reference_document} has required read-only fields: {unfillable}"
			)

	def test_the_settings_step_walks_the_single(self):
		step = frappe.get_doc("Onboarding Step", "Connect the COVA Clinic API")
		self.assertEqual(step.action, "Show Form Tour")
		self.assertEqual(step.reference_document, "Cova Clinic Settings")
		# showFormTour() routes to the Single itself rather than `<doctype>/new`.
		self.assertEqual(step.is_single, 1)
		self.assertTrue(step.form_tour)

	def test_every_walkthrough_covers_the_mandatory_fields(self):
		"""The user has to be shown what they will be asked for. A tour that skips
		a required field leaves them at a save that fails for a reason the
		walkthrough never mentioned."""
		for tour_name in frappe.get_all(
			"Form Tour", filters={"module": "Cova Clinic Integration"}, pluck="name"
		):
			tour = frappe.get_doc("Form Tour", tour_name)
			meta = frappe.get_meta(tour.reference_doctype)
			walked = {step.fieldname for step in tour.steps}
			required = {
				f.fieldname
				for f in meta.fields
				if f.reqd
				and not f.default
				and not f.fetch_from
				and f.fieldname not in ("amended_from", "naming_series")
			}
			self.assertEqual(
				required - walked,
				set(),
				f"{tour_name} never shows required field(s): {sorted(required - walked)}",
			)

	def test_no_walkthrough_reaches_into_a_child_table(self):
		"""Walkthroughs stay on the parent form and leave grids alone.

		A step marked as a child-table field, or one immediately after a Table
		step that names it as its parent, makes frappe drive the grid: it adds an
		"Add a Row" step, opens the row, and then the mandatory columns inside it
		have to be filled before the tour's save step can complete. Health Report
		requires a Medical Case and Job Offer Term requires both of its columns,
		so a walkthrough that opened a row would leave the user stuck behind
		values the tour never meant to ask for — or worse, a half-filled row
		saved on a real document.

		The parent Table fields are all optional, so an empty grid saves cleanly.
		That only holds while no step reaches inside one.
		"""
		for tour_name in frappe.get_all(
			"Form Tour", filters={"module": "Cova Clinic Integration"}, pluck="name"
		):
			tour = frappe.get_doc("Form Tour", tour_name)
			for step in tour.steps:
				where = f"{tour_name} step {step.idx} ({step.fieldname})"
				self.assertFalse(step.is_table_field, f"{where} is a child-table field")
				self.assertFalse(
					step.parent_fieldname,
					f"{where} names a parent table, which makes frappe open a row",
				)

	def test_every_walkthrough_is_reachable_from_its_form(self):
		"""public/js/form_walkthrough.js puts a "Walk Me Through" button on each
		doctype that ships a tour. A tour missing from that map can only ever be
		started by the onboarding panel — and if it starts crooked there is no way
		to restart it."""
		import re

		app_path = frappe.get_app_path("cova_clinic_integration")
		with open(f"{app_path}/public/js/form_walkthrough.js") as handle:
			source = handle.read()
		mapped = dict(re.findall(r'"([^"]+)":\s*"([^"]+ Walkthrough|Cova Pre-Employment on Job Offer)"', source))

		for tour_name in frappe.get_all(
			"Form Tour", filters={"module": "Cova Clinic Integration"}, pluck="name"
		):
			tour = frappe.get_doc("Form Tour", tour_name)
			self.assertEqual(
				mapped.get(tour.reference_doctype),
				tour_name,
				f"{tour_name} is not wired to a Walk Me Through button on {tour.reference_doctype}",
			)

	def test_no_walkthrough_highlights_a_read_only_field(self):
		"""A read-only field is nothing the user can act on — stopping the tour to
		point at one just pads it out. Whatever is worth saying about them belongs
		in the closing step's copy, not in an anchor of its own."""
		for tour_name in frappe.get_all(
			"Form Tour", filters={"module": "Cova Clinic Integration"}, pluck="name"
		):
			tour = frappe.get_doc("Form Tour", tour_name)
			meta = frappe.get_meta(tour.reference_doctype)
			highlighted = sorted(
				{step.fieldname for step in tour.steps if meta.get_field(step.fieldname).read_only}
			)
			self.assertEqual(
				highlighted, [], f"{tour_name} highlights read-only field(s): {highlighted}"
			)

	def test_every_walkthrough_ends_by_asking_for_the_entry(self):
		for tour_name in frappe.get_all(
			"Form Tour", filters={"module": "Cova Clinic Integration"}, pluck="name"
		):
			tour = frappe.get_doc("Form Tour", tour_name)
			self.assertTrue(tour.steps, tour_name + " has no steps")
			self.assertIn(
				"Save",
				tour.steps[-1].description or "",
				tour_name + " does not end by asking the user to save",
			)

	def test_every_form_tour_step_anchors_on_a_real_field(self):
		"""A tour step whose fieldname does not exist highlights nothing — the
		popover renders detached from the form with no error anywhere."""
		tour = frappe.get_doc("Form Tour", self.TOUR)
		meta = frappe.get_meta(tour.reference_doctype)
		self.assertTrue(tour.steps, "the tour has no steps")
		for step in tour.steps:
			self.assertTrue(
				meta.has_field(step.fieldname),
				f"{tour.reference_doctype} has no field {step.fieldname} (tour step: {step.title})",
			)

	def test_the_tour_reveals_the_cova_block_before_pointing_into_it(self):
		"""The COVA section is hidden until `company` matches the clinic company,
		so the tour has to set company before it walks the fields inside it."""
		tour = frappe.get_doc("Form Tour", self.TOUR)
		fieldnames = [step.fieldname for step in tour.steps]
		self.assertEqual(fieldnames[0], "company")

		meta = frappe.get_meta(tour.reference_doctype)
		gated = [fn for fn in fieldnames if (meta.get_field(fn).depends_on or "").find("cova_clinic_company") >= 0]
		for fieldname in gated:
			self.assertGreater(fieldnames.index(fieldname), 0)


class TestPayrollNumberOnDeskRecords(IntegrationTestCase):
	"""A record created from a button must carry the same identifier the API sends.

	`payroll_number` fetches from `employee.employee_number`, which is **optional**
	in HR. So the Employee form's *Request Medical Test* button — a plain
	`frappe.client.insert` — produced a request with an empty payroll number
	wherever that field was unset, and `submit_test_request` then handed COVA
	`memberIdentifier: ""`. The API path never had the bug because it fills the
	field with `employee_payroll_id()`.
	"""

	def tearDown(self):
		frappe.db.rollback()

	def _employee_without_a_payroll_number(self):
		employee = make_employee("CV-DESK-6001", "Desk Tester")
		frappe.db.set_value("Employee", employee, "employee_number", "")
		return employee

	def test_a_desk_created_request_gets_the_employee_id(self):
		employee = self._employee_without_a_payroll_number()
		request = frappe.get_doc(
			{
				"doctype": "Clinic Test Request",
				"member_type": "Active",
				"employee": employee,
				"status": "Pending",
				"test_package": "Annual Medical",
				"scheduled_from": "2026-08-01",
				"scheduled_to": "2026-08-15",
			}
		).insert(ignore_permissions=True)

		self.assertTrue(request.payroll_number, "the request was saved with no payroll number")
		self.assertEqual(request.payroll_number, api.employee_payroll_id(frappe.get_doc("Employee", employee)))

	def test_a_fetched_employee_number_still_wins(self):
		"""Only an empty value is filled — inbound resolution accepts either, and
		a site where the two differ must keep sending what COVA already holds."""
		employee = make_employee("CV-DESK-6002", "Numbered Tester")
		frappe.db.set_value("Employee", employee, "employee_number", "CV-DESK-6002")
		request = frappe.get_doc(
			{
				"doctype": "Clinic Test Request",
				"member_type": "Active",
				"employee": employee,
				"status": "Pending",
				"test_package": "Annual Medical",
				"scheduled_from": "2026-08-01",
				"scheduled_to": "2026-08-15",
			}
		).insert(ignore_permissions=True)

		self.assertEqual(request.payroll_number, "CV-DESK-6002")

	def test_a_pre_employment_request_is_left_alone(self):
		request = frappe.get_doc(
			{
				"doctype": "Clinic Test Request",
				"member_type": "Pre Employment",
				"nationa_id": "CV-DESK-NID-1",
				"full_name": "Desk Candidate",
				"status": "Pending",
				"test_package": "Pre Employment Wellness",
				"scheduled_from": "2026-08-01",
				"scheduled_to": "2026-08-15",
			}
		).insert(ignore_permissions=True)

		self.assertFalse(request.payroll_number)

	def test_the_identifier_survives_the_round_trip_to_cova(self):
		"""What the button ends up sending COVA, and whether the reply resolves
		back to the same employee."""
		employee = self._employee_without_a_payroll_number()
		request = frappe.get_doc(
			{
				"doctype": "Clinic Test Request",
				"member_type": "Active",
				"employee": employee,
				"status": "Pending",
				"test_package": "Annual Medical",
				"scheduled_from": "2026-08-01",
				"scheduled_to": "2026-08-15",
			}
		).insert(ignore_permissions=True)

		sent = {}

		def capture(url, headers=None, json=None, **kwargs):
			sent.update(json or {})
			return {"status": "ok"}

		with patch.object(api, "make_post_request", side_effect=capture):
			with stub_request(json_body={"action": "submit_test_request", "request_name": request.name}):
				api.cova_clinic_api()

		self.assertTrue(sent.get("memberIdentifier"), "COVA was sent an empty memberIdentifier")
		self.assertEqual(api.employee_from_payroll(sent["memberIdentifier"]), employee)

	def test_a_desk_created_visit_gets_the_employee_id(self):
		employee = self._employee_without_a_payroll_number()
		visit = frappe.get_doc(
			{
				"doctype": "Clinic Visit Cost",
				"employee": employee,
				"full_name": "Desk Tester",
				"visit_date": "2026-08-20",
				"visit_line_item": [
					{"purpose": "Consultation", "cost": 500},
					{"purpose": "Pharmacy", "cost": 1250.5},
				],
			}
		).insert(ignore_permissions=True)

		self.assertTrue(visit.payroll_number)
		self.assertEqual(api.employee_from_payroll(visit.payroll_number), employee)
		# The total is derived, so a hand-entered visit adds up like a pushed one.
		self.assertEqual(visit.total_cost, 1750.5)

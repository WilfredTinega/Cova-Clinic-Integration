# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt
"""COVA / CheckUps clinic integration endpoints.

These functions are the codebase equivalents of the "Cova Clinic API" and
"Health Report" Server Scripts that previously lived on kaitet-group.upande.com.
Their public URLs are preserved via ``override_whitelisted_methods`` in hooks.py:

    POST /api/method/cova_clinic_api        -> cova_clinic_api()
    POST /api/method/clinic_disease_report  -> clinic_disease_report()

Both require an authenticated session (the original Server Scripts had
``allow_guest = 0``); COVA authenticates its webhook calls with a token.
"""

import json
import re

import frappe
from frappe import _
from frappe.integrations.utils import make_post_request

from cova_clinic_integration.member_link import backfill_member_links

# ─── access control ───────────────────────────────────────────────────────
# The health / disease report is HR-only. Besides the standard HRMS roles, each
# site carries its own per-company HR role ("HR Ravine", "HR Kaitet", …), so the
# rule is: any role in the HR family. Matching on the "HR " prefix keeps new
# company roles working without a code change.

HEALTH_REPORT_ROLES = ("HR Manager", "HR User")


def health_report_roles(user: str | None = None) -> list[str]:
	"""The HR roles this user holds — HR Manager, HR User, or any other role in
	the HR family (HR Ravine, HR Kaitet, …). The dashboard shows these so a
	viewer can see which role is granting them access."""
	user = user or frappe.session.user
	if user == "Guest":
		return []

	held = []
	for role in frappe.get_roles(user):
		role = (role or "").strip()
		if role in HEALTH_REPORT_ROLES or role.upper().startswith("HR "):
			held.append(role)
	return sorted(set(held))


def has_health_report_access(user: str | None = None) -> bool:
	"""True when the user holds any role in the HR family."""
	return bool(health_report_roles(user))


def assert_health_report_access() -> None:
	if not has_health_report_access():
		frappe.throw(
			_("You are not permitted to view the Health Report."),
			frappe.PermissionError,
		)


# ─── helpers ──────────────────────────────────────────────────────────────


def build_headers():
	api_key = frappe.db.get_single_value("Cova Clinic Settings", "api_key")
	headers = {}
	if api_key:
		headers["X-Api-Key"] = api_key
	return headers


def normalize_phone(phone):
	"""Best-effort E.164 for the Kenyan numbers this integration handles.

	The stored field is Data (rendered as a phone input), so anything saves — this
	is about storing *and sending COVA* one consistent shape:

	    0740781289      -> +254740781289
	    740781289       -> +254740781289
	    254740781289    -> +254740781289
	    0740-781 289    -> +254740781289
	    +44 7911 123456 -> +447911123456   (already international, left alone)

	Separators are dropped and a leading ``+`` is trusted. A number that is
	neither a recognisable Kenyan local form nor explicitly international is
	returned digits-only rather than guessed at — the previous version prefixed
	``+254`` onto *anything* unprefixed, which turned a foreign number typed
	without its ``+`` into a bogus Kenyan one.
	"""
	raw = (phone or "").strip()
	if not raw:
		return ""

	international = raw.startswith("+")
	digits = re.sub(r"\D", "", raw)
	if not digits:
		return ""

	if international or digits.startswith("254"):
		return "+" + digits

	# Kenyan local: 0 + 9 digits (07xx xxx xxx / 01xx xxx xxx)
	if digits.startswith("0") and len(digits) == 10:
		return "+254" + digits[1:]

	# the same number with the trunk 0 already dropped
	if len(digits) == 9:
		return "+254" + digits

	return digits


def create_or_update_cova_member(
	member_type, employee, national_id, full_name, payroll_number, phone, gender, activate=True
):
	"""Upsert the local Cova Members row for a registration attempt.

	``activate`` carries whether Cova actually accepted it. ``status`` means
	"registered on Cova", so a rejected call must not leave the member looking
	Active — the row is still written either way, because save_cova_response
	needs somewhere to record what Cova said.
	"""
	# Find existing member
	existing = None
	if member_type == "Active" and employee:
		existing = frappe.db.get_value("Cova Members", {"employee": employee}, "name")
	elif member_type == "Pre Employment" and national_id:
		existing = frappe.db.get_value(
			"Cova Members",
			{"national_id": national_id, "member_type": "Pre Employment"},
			"name",
		)

	if existing:
		# Re-registering someone brings their membership back rather than leaving
		# it as it was — a member deactivated on exit and then registered again
		# (a rehire, or a deactivation done in error) has to come back Active.
		doc = frappe.get_doc("Cova Members", existing)
		if activate:
			doc.status = "Active"

		# Only overwrite what the caller actually supplied. An Active registration
		# passes national_id="" and a pre-employment one passes payroll_number="",
		# so assigning unconditionally would wipe the stored value.
		for fieldname, value in (
			("employee", employee),
			("national_id", national_id),
			("full_name", full_name),
			("payroll_number", payroll_number),
			("gender", gender),
		):
			if value:
				doc.set(fieldname, value)
		if phone:
			doc.phone_number = normalize_phone(phone)

		doc.save(ignore_permissions=True)
		backfill_member_links(doc.name, doc.employee, doc.national_id)
		return doc.name

	doc = frappe.new_doc("Cova Members")
	doc.member_type = member_type
	doc.status = "Active" if activate else "Inactive"
	doc.employee = employee or ""
	doc.national_id = national_id or ""
	doc.full_name = full_name or ""
	doc.payroll_number = payroll_number or ""
	doc.phone_number = normalize_phone(phone) or ""
	doc.gender = gender or ""
	doc.insert(ignore_permissions=True)
	# Records created before the member existed (register_preemployment_candidate
	# inserts the Clinic Test Request first) are attached here.
	backfill_member_links(doc.name, doc.employee, doc.national_id)
	return doc.name


def save_cova_response(cm_name, result):
	"""Persist COVA's register response on the Cova Members record so we always
	track how each member is registered on their end (covaMemberId, wallet,
	branch, package, patient id) plus the full raw payload as a catch-all."""
	if not cm_name:
		return
	mapping = {
		"cova_member_id": "covaMemberId",
		"wallet_id": "walletId",
		"wallet_reference": "walletReference",
		"company_code": "companyCode",
		"branch_code": "branchCode",
		"package_code": "packageCode",
		"patient_id": "patientId",
	}
	# doc.save() writes only columns that physically exist, so this is safe before/after
	# the fields' columns are materialised by a deploy; never let capture break the flow.
	try:
		cm = frappe.get_doc("Cova Members", cm_name)
		for erp_field in mapping:
			v = result.get(mapping[erp_field])
			if v is not None:
				cm.set(erp_field, v)
		cm.set("cova_raw", json.dumps(result, default=str))
		cm.save(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="COVA save_cova_response", message=frappe.get_traceback())


def employee_payroll_id(emp) -> str:
	"""The identifier COVA knows an employee by.

	This is the **Employee ID**. ``employee_number`` is optional in HR, and where
	it is blank the outbound payload used to carry ``payrollNumber: null``, which
	leaves COVA with no key at all. On the live site the two are the same string
	(Employee is named by payroll number there), so this changes nothing in
	production and gives every other site a usable identifier.

	Accepts a Document, a ``frappe._dict`` row, or a plain dict.
	"""
	name = emp.get("name") if isinstance(emp, dict) else emp.name
	return str(name or "")


def employee_from_payroll(payroll_number) -> str | None:
	"""Resolve the Employee a COVA payload refers to.

	Inbound payloads echo back whatever went out as ``payrollNumber``. New ones
	carry the Employee ID; rows COVA already holds may still carry an
	``employee_number``, so both are accepted.
	"""
	if not payroll_number:
		return None
	by_number = frappe.db.get_value("Employee", {"employee_number": payroll_number}, "name")
	if by_number:
		return by_number
	return payroll_number if frappe.db.exists("Employee", payroll_number) else None


def get_cova_config():
	base_url = frappe.db.get_single_value("Cova Clinic Settings", "base_url")
	register_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "register_endpoint")
	deactivation_endpoint = frappe.db.get_single_value(
		"Cova Clinic Settings", "deactivation_endpoint"
	)
	return base_url, register_endpoint, deactivation_endpoint


def register_one(base_url, register_endpoint, headers, emp):
	payload = {
		"schemeType": "Active",
		"payrollNumber": employee_payroll_id(emp),
		"fullName": emp.employee_name,
		"nationalId": emp.get("national_id") or "",
		"dateOfBirth": str(emp.date_of_birth) if emp.date_of_birth else "",
		"gender": emp.gender or "",
		"phone": normalize_phone(emp.cell_number),
	}
	result = make_post_request(base_url + register_endpoint, headers=headers, json=payload)
	if result.get("covaMemberId"):
		try:
			frappe.db.set_value("Employee", emp.name, "cova_member_id", result.get("covaMemberId"))
		except Exception:
			pass
	if not result.get("error"):
		# Mirror of deactivate_one. Left set, the employee is excluded from every
		# future sync_members deactivation sweep, which filters on cova_deactivated=0.
		frappe.db.set_value("Employee", emp.name, "cova_deactivated", 0)
	cm_name = create_or_update_cova_member(
		"Active", emp.name, "", emp.employee_name, employee_payroll_id(emp), emp.cell_number or "", emp.gender or "",
		activate=not result.get("error"),
	)
	save_cova_response(cm_name, result)
	return result


def deactivate_one(base_url, deactivation_endpoint, headers, emp):
	payload = {
		"payrollNumber": employee_payroll_id(emp),
		"covaMemberId": emp.get("cova_member_id") or "",
		"exitDate": str(emp.relieving_date) if emp.get("relieving_date") else "",
		"requiresExitMedical": False,
	}
	result = make_post_request(
		base_url + deactivation_endpoint, headers=headers, json=payload
	)
	if not result.get("error"):
		frappe.db.set_value("Employee", emp.name, "cova_deactivated", 1)
	return result


# ─── main dispatcher endpoint ───────────────────────────────────────────────


@frappe.whitelist()
def cova_clinic_api():
	"""Single POST entry point for every COVA action. The action is selected by
	the ``action`` key in the JSON body."""
	data = frappe.request.get_json() or {}
	action = data.get("action")
	resp = None

	# ─── 1. REGISTER MEMBER (single) ──────────────────────────────────
	if action == "register_member":
		base_url = frappe.db.get_single_value("Cova Clinic Settings", "base_url")
		register_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "register_endpoint")
		headers = build_headers()

		member_type = data.get("member_type")
		employee = data.get("employee")
		emp = None

		if member_type == "Active":
			emp = frappe.get_doc("Employee", employee)
			payload = {
				"schemeType": "Active",
				"payrollNumber": employee_payroll_id(emp),
				"fullName": emp.employee_name,
				"nationalId": emp.get("national_id") or "",
				"dateOfBirth": str(emp.date_of_birth) if emp.date_of_birth else "",
				"gender": emp.gender or "",
				"phone": normalize_phone(emp.cell_number),
			}
		else:
			payload = {
				"schemeType": "PreEmployment",
				"nationalId": data.get("nationa_id"),
				"fullName": data.get("full_name"),
				"dateOfBirth": data.get("date_of_birth"),
				"gender": data.get("gender"),
				"phone": normalize_phone(data.get("phone_number")),
			}

		result = make_post_request(base_url + register_endpoint, headers=headers, json=payload)

		if member_type == "Active" and result.get("covaMemberId"):
			try:
				frappe.db.set_value("Employee", employee, "cova_member_id", result.get("covaMemberId"))
			except Exception:
				pass

		# Registering again clears the deactivation flag, whether or not Cova
		# returned a member id (a mocked or partial reply still means registered).
		if member_type == "Active" and employee and not result.get("error"):
			frappe.db.set_value("Employee", employee, "cova_deactivated", 0)

		# Create/refresh the Cova Member and store COVA's registration response on it.
		if member_type == "Active":
			cm_name = create_or_update_cova_member(
				"Active", employee, "", emp.employee_name, employee_payroll_id(emp), emp.cell_number or "", emp.gender or "",
				activate=not result.get("error"),
			)
		else:
			cm_name = create_or_update_cova_member(
				"Pre Employment", "", data.get("nationa_id") or "", data.get("full_name") or "", "", data.get("phone_number") or "", data.get("gender") or "",
				activate=not result.get("error"),
			)
		save_cova_response(cm_name, result)

		resp = result

	# ─── 2. DEACTIVATE MEMBER (single) ────────────────────────────────
	elif action == "deactivate_member":
		base_url = frappe.db.get_single_value("Cova Clinic Settings", "base_url")
		deactivation_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "deactivation_endpoint")
		headers = build_headers()

		employee = data.get("employee")
		exit_date = data.get("exit_date")
		requires_exit_medical = data.get("requires_exit_medical", False)
		emp = frappe.get_doc("Employee", employee)

		payload = {
			"payrollNumber": employee_payroll_id(emp),
			"covaMemberId": emp.get("cova_member_id") or "",
			"exitDate": str(exit_date),
			"requiresExitMedical": requires_exit_medical,
		}

		# Create the exit medical request FIRST (before the Cova call which may fail)
		exit_request = None
		if requires_exit_medical:
			req_doc = frappe.new_doc("Clinic Test Request")
			req_doc.member_type = "Active"
			req_doc.employee = emp.name
			req_doc.payroll_number = employee_payroll_id(emp)
			req_doc.status = "Pending"
			req_doc.test_package = "Exit Medical"
			req_doc.scheduled_from = frappe.utils.nowdate()
			req_doc.scheduled_to = frappe.utils.add_days(frappe.utils.nowdate(), 15)
			req_doc.notes = "Auto-created on Cova deactivation"
			req_doc.insert(ignore_permissions=True)
			exit_request = req_doc.name

		result = make_post_request(base_url + deactivation_endpoint, headers=headers, json=payload)

		# Update Cova Member to Inactive
		cm_name = frappe.db.get_value("Cova Members", {"employee": employee}, "name")
		if cm_name:
			frappe.db.set_value("Cova Members", cm_name, "status", "Inactive")

		# Mark the Employee too, as the bulk deactivate_one does. Without this the
		# next sync_members sweep sees cova_deactivated=0 and deactivates them all
		# over again.
		if not result.get("error"):
			frappe.db.set_value("Employee", employee, "cova_deactivated", 1)

		resp = {"deactivation": result, "exit_request": exit_request}

	# ─── 3. SYNC MEMBERS (bulk register + deactivate) ─────────────────
	elif action == "sync_members":
		base_url, register_endpoint, deactivation_endpoint = get_cova_config()
		headers = build_headers()

		registered = 0
		register_failed = 0
		deactivated = 0
		deactivate_failed = 0

		to_register = frappe.db.get_all(
			"Employee",
			filters={"status": "Active", "cova_member_id": ["in", ["", None]]},
			fields=["name", "employee_number", "employee_name", "national_id", "date_of_birth", "gender", "cell_number"],
		)

		for row in to_register:
			emp = frappe.get_doc("Employee", row.name)
			try:
				result = register_one(base_url, register_endpoint, headers, emp)
				if result.get("error"):
					register_failed = register_failed + 1
				else:
					registered = registered + 1
			except Exception:
				register_failed = register_failed + 1

		to_deactivate = frappe.db.get_all(
			"Employee",
			filters={"status": ["!=", "Active"], "cova_member_id": ["not in", ["", None]], "cova_deactivated": 0},
			fields=["name", "employee_number", "cova_member_id", "relieving_date"],
		)

		for row in to_deactivate:
			emp = frappe.get_doc("Employee", row.name)
			try:
				result = deactivate_one(base_url, deactivation_endpoint, headers, emp)
				if result.get("error"):
					deactivate_failed = deactivate_failed + 1
				else:
					deactivated = deactivated + 1
			except Exception:
				deactivate_failed = deactivate_failed + 1

		resp = {
			"status": "sync complete",
			"registered": registered,
			"register_failed": register_failed,
			"deactivated": deactivated,
			"deactivate_failed": deactivate_failed,
		}

	# ─── 4. SUBMIT TEST REQUEST ───────────────────────────────────────
	elif action == "submit_test_request":
		base_url = frappe.db.get_single_value("Cova Clinic Settings", "base_url")
		test_request_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "test_request_endpoint")
		headers = build_headers()

		request_name = data.get("request_name")
		doc = frappe.get_doc("Clinic Test Request", request_name)

		member_identifier = doc.payroll_number if doc.member_type == "Active" else doc.get("nationa_id") or ""

		payload = {
			"requestId": doc.name,
			"memberIdentifier": member_identifier,
			"memberType": doc.member_type.replace(" ", ""),
			"testPackage": doc.test_package.replace(" ", ""),
			"scheduledWindow": {
				"from": str(doc.scheduled_from) if doc.get("scheduled_from") else "",
				"to": str(doc.scheduled_to) if doc.get("scheduled_to") else "",
			},
			"notes": doc.get("notes") or "",
		}

		if doc.member_type == "Pre Employment":
			payload["candidateBiodata"] = {
				"fullName": doc.full_name or "",
				"nationalId": doc.get("nationa_id") or "",
				"dateOfBirth": str(doc.date_of_birth) if doc.get("date_of_birth") else "",
				"gender": doc.gender or "",
				"phone": normalize_phone(doc.phone_number),
			}

		result = make_post_request(base_url + test_request_endpoint, headers=headers, json=payload)

		# Status stays Pending until the test result webhook comes back from Cova
		resp = result

	# ─── REGISTER PRE-EMPLOYMENT CANDIDATE (Job Offer) ────────────────
	elif action == "register_preemployment_candidate":
		base_url = frappe.db.get_single_value("Cova Clinic Settings", "base_url")
		register_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "register_endpoint")
		test_request_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "test_request_endpoint")
		headers = build_headers()

		offer_name = data.get("job_offer")
		offer = frappe.get_doc("Job Offer", offer_name)

		national_id = offer.get("custom_national_id") or ""
		full_name = offer.get("applicant_name") or ""
		date_of_birth = str(offer.get("custom_date_of_birth")) if offer.get("custom_date_of_birth") else ""
		gender = offer.get("custom_gender") or ""
		# Falls back to the linked applicant for offers predating custom_phone_number.
		phone = offer.get("custom_phone_number") or ""
		if not phone and offer.get("job_applicant"):
			phone = frappe.db.get_value("Job Applicant", offer.job_applicant, "phone_number") or ""

		# Normalize phone to E.164 with Kenya country code (Phone field requires a country code)
		phone = normalize_phone(phone)

		if offer.get("custom_cova_tested"):
			resp = {"error": "Candidate already tested. Pre-employment is locked to one test."}
		elif not national_id:
			resp = {"error": "National ID is required for pre-employment registration."}
		else:
			# Step 1: create the tracking Clinic Test Request FIRST (persists even if Cova calls fail)
			req_doc = frappe.new_doc("Clinic Test Request")
			req_doc.member_type = "Pre Employment"
			req_doc.nationa_id = national_id
			req_doc.full_name = full_name
			req_doc.date_of_birth = offer.get("custom_date_of_birth")
			req_doc.gender = gender
			req_doc.phone_number = phone
			req_doc.status = "Pending"
			req_doc.test_package = "Pre Employment Wellness"
			req_doc.scheduled_from = frappe.utils.nowdate()
			req_doc.scheduled_to = frappe.utils.add_days(frappe.utils.nowdate(), 15)
			req_doc.notes = "Pre-employment wellness for Job Offer %s" % offer_name
			req_doc.insert(ignore_permissions=True)

			# Step 2: register to Cova pre-employment scheme (same register endpoint)
			register_payload = {
				"schemeType": "PreEmployment",
				"nationalId": national_id,
				"fullName": full_name,
				"dateOfBirth": date_of_birth,
				"gender": gender,
				"phone": phone,
			}
			register_result = make_post_request(base_url + register_endpoint, headers=headers, json=register_payload)

			# Step 3: submit the single PreEmploymentWellness test (same test endpoint)
			test_payload = {
				"requestId": req_doc.name,
				"memberIdentifier": national_id,
				"memberType": "PreEmployment",
				"testPackage": "PreEmploymentWellness",
				"scheduledWindow": {"from": str(req_doc.scheduled_from), "to": str(req_doc.scheduled_to)},
				"candidateBiodata": {
					"fullName": full_name,
					"nationalId": national_id,
					"dateOfBirth": date_of_birth,
					"gender": gender,
					"phone": phone,
				},
				"notes": req_doc.notes,
			}
			test_result = make_post_request(base_url + test_request_endpoint, headers=headers, json=test_payload)

			# Step 4: create Cova Member and store COVA's registration response on it
			cm_name = create_or_update_cova_member(
				"Pre Employment", "", national_id, full_name, "", phone, gender,
				activate=not register_result.get("error"),
			)
			save_cova_response(cm_name, register_result)

			# Step 5: mark offer registered
			frappe.db.set_value("Job Offer", offer_name, "custom_cova_registered", 1)

			resp = {
				"status": "success",
				"registration": register_result,
				"test_request": req_doc.name,
				"test_submission": test_result,
			}

	# ─── 5. RECEIVE VISIT (webhook from COVA) ─────────────────────────
	elif action == "receive_visit":
		payroll_number = data.get("payrollNumber")
		visit_date = data.get("visitDateTime")
		line_items = data.get("lineItems") or []
		bba = data.get("benefitBalanceAfter")
		if not bba:
			bba = {}

		# COVA sends a full timestamp, but the visit doctype stores a Date, so the
		# duplicate check has to compare on the day — matching the raw timestamp
		# against a Date column never hits.
		visit_day = frappe.utils.getdate(visit_date) if visit_date else None
		already_exists = frappe.db.exists(
			"Clinic Visit Cost", {"payroll_number": payroll_number, "visit_date": visit_day}
		)
		employee = employee_from_payroll(payroll_number)

		if already_exists:
			resp = {"status": "duplicate"}
		elif not employee:
			resp = {"error": "Employee not found for payroll %s" % payroll_number}
		else:
			doc = frappe.new_doc("Clinic Visit Cost")
			doc.employee = employee
			# full_name feeds the CV.-.{full_name}.-.{employee}.-.### autoname; without
			# it the series falls back to a hash and the visit is unreadable in a list.
			doc.full_name = frappe.db.get_value("Employee", employee, "employee_name") or ""
			doc.payroll_number = payroll_number
			doc.visit_date = visit_day
			# v2: benefitBalanceAfter is now an OBJECT keyed by benefit category
			# (was a single number). Capture each category; a missing key = zero.
			# There is no combined-total field — the doctype keeps the per-category
			# balances and cova_raw holds whatever else COVA sent.
			b_consult = b_pharm = b_lab = b_diag = b_spec = 0
			if isinstance(bba, dict):
				b_consult = bba.get("Consultation") or 0
				b_pharm = bba.get("Pharmacy") or 0
				b_lab = bba.get("Laboratory") or 0
				b_diag = bba.get("Diagnostic") or 0
				b_spec = bba.get("Specialist") or 0
			doc.benefit_consultation = b_consult
			doc.benefit_pharmacy = b_pharm
			doc.benefit_laboratory = b_lab
			doc.benefit_diagnostic = b_diag
			doc.benefit_specialist = b_spec
			doc.cova_raw = json.dumps(data, default=str)

			total = 0
			for item in line_items:
				doc.append("visit_line_item", {
					"purpose": item.get("type"),
					"cost": item.get("amount") or 0,
					"notes": item.get("notes") or "",
				})
				total = total + (item.get("amount") or 0)

			doc.total_cost = total
			doc.insert(ignore_permissions=True)

			# Update Cova Member last visit
			cm_name = frappe.db.get_value("Cova Members", {"payroll_number": payroll_number}, "name")
			if cm_name:
				frappe.db.set_value("Cova Members", cm_name, "last_visit", visit_day)
				frappe.db.set_value("Cova Members", cm_name, "visit_reference", doc.name)

			resp = {"status": "success", "name": doc.name}

	# ─── 6. RECEIVE TEST RESULT (webhook from COVA) ───────────────────
	elif action == "receive_test_result":
		request_id = data.get("requestId")
		test_package = data.get("testPackage")
		clinical_outcome = data.get("clinicalOutcome")

		request_exists = frappe.db.exists("Clinic Test Request", request_id)

		if not request_exists:
			resp = {"error": "Request %s not found" % request_id}
		else:
			outcome_risk_map = {
				"FitForWork": "Low Risk",
				"FitWithRestrictions": "Medium Risk",
				"InconclusiveRetestRequired": "Medium Risk",
				"UnfitForWork": "High Risk",
			}
			risk = outcome_risk_map.get(clinical_outcome, "No Risk")

			req = frappe.get_doc("Clinic Test Request", request_id)

			if not frappe.db.exists("Medical Case", {"cases": test_package}):
				mc = frappe.new_doc("Medical Case")
				mc.cases = test_package
				mc.insert(ignore_permissions=True)

			mc_name = frappe.db.get_value("Medical Case", {"cases": test_package}, "name")

			latest_visit = frappe.db.get_value(
				"Clinic Visit Cost", {"payroll_number": req.payroll_number}, "name", order_by="visit_date desc"
			)

			result_doc = frappe.new_doc("Clinic Test Result")
			result_doc.member_type = req.member_type
			result_doc.employee = req.employee
			result_doc.payroll_number = req.payroll_number
			result_doc.clinic_visit_reference = latest_visit
			result_doc.request_id = request_id
			result_doc.test_package = test_package
			result_doc.clinical_outcome = clinical_outcome
			result_doc.cova_raw = json.dumps(data, default=str)
			result_doc.append("results", {"test": mc_name, "select_tezd": risk})
			result_doc.insert(ignore_permissions=True)

			frappe.db.set_value("Clinic Test Request", request_id, "status", "Completed")
			frappe.db.set_value("Clinic Test Request", request_id, "linked_test_result", result_doc.name)

			# Update Cova Member test references
			cm_name = None
			if req.member_type == "Active" and req.employee:
				cm_name = frappe.db.get_value("Cova Members", {"employee": req.employee}, "name")
			elif req.member_type == "Pre Employment" and req.get("nationa_id"):
				cm_name = frappe.db.get_value(
					"Cova Members", {"national_id": req.nationa_id, "member_type": "Pre Employment"}, "name"
				)
			if cm_name:
				frappe.db.set_value("Cova Members", cm_name, "test_request_reference", request_id)
				frappe.db.set_value("Cova Members", cm_name, "test_result_reference", result_doc.name)

			# If this is a pre-employment candidate, lock the Job Offer (one test only)
			if req.member_type == "Pre Employment" and req.get("nationa_id"):
				offer_name = frappe.db.get_value("Job Offer", {"custom_national_id": req.nationa_id}, "name")
				if offer_name:
					frappe.db.set_value("Job Offer", offer_name, "custom_cova_tested", 1)
					frappe.db.set_value("Job Offer", offer_name, "custom_linked_test_result", result_doc.name)

			resp = {
				"status": "success",
				"request": request_id,
				"result": result_doc.name,
				"visit": latest_visit,
				"risk": risk,
			}

	# ─── 7. RECEIVE HEALTH REPORT (webhook from COVA) ─────────────────
	elif action == "receive_health_report":
		month = data.get("month")
		posting_date = data.get("date")
		cases = data.get("cases") or []

		if not month or not cases:
			resp = {"error": "month and cases are required"}
		else:
			already_exists = frappe.db.exists(
				"Health Monthly Report", {"month": month.upper(), "posting_date": posting_date}
			)

			if already_exists:
				resp = {"status": "duplicate", "message": "Report for %s already exists" % month}
			else:
				doc = frappe.new_doc("Health Monthly Report")
				doc.month = month.upper()
				doc.posting_date = posting_date
				total = 0

				for case in cases:
					condition = case.get("condition")
					count = case.get("count") or 0

					if not frappe.db.exists("Medical Case", {"cases": condition}):
						mc = frappe.new_doc("Medical Case")
						mc.cases = condition
						mc.insert(ignore_permissions=True)

					mc_name = frappe.db.get_value("Medical Case", {"cases": condition}, "name")
					doc.append("medical_cases", {"medical_case": mc_name, "case_count": count})
					total = total + count

				doc.total_cases = total
				doc.insert(ignore_permissions=True)
				resp = {"status": "success", "name": doc.name, "total_cases": total}

	# ─── 8. RUN STATUTORY TESTS ───────────────────────────────────────
	elif action == "run_statutory_tests":
		cholinesterase_designations = [
			"Sprayer", "Spray Pump Operator", "Spray Applicator",
			"Spray Supervisor", "Crop Protection Section Head", "Scouter",
		]
		food_handler_designations = [
			"Chef", "Cook / Cleaner", "Directors Cook / Cleaner",
			"Hospitality", "Cleaner/Feeder", "Feeder",
			"Milker", "Dairy Assistant", "Cold Room Attendant",
		]

		COMPANY = "Karen Roses"
		PICK_COUNT = int(data.get("pick_count") or 10)

		test_map = [
			{"package": "Annual Medical", "designations": cholinesterase_designations, "label": "Cholinesterase"},
			{"package": "Annual Medical", "designations": food_handler_designations, "label": "Food Handler"},
		]

		base_url = frappe.db.get_single_value("Cova Clinic Settings", "base_url")
		test_request_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "test_request_endpoint")
		headers = build_headers()
		current_year = frappe.utils.nowdate().split("-")[0]
		today = frappe.utils.nowdate()
		window_end = str(frappe.utils.add_days(today, 15))

		results = []

		for mapping in test_map:
			package = mapping["package"]
			desig_list = mapping["designations"]
			if not desig_list:
				continue

			year_start = current_year + "-01-01"

			# Pick PICK_COUNT random employees PER DESIGNATION (not per package)
			for desig in desig_list:
				query = (
					"SELECT e.name, e.employee_number, e.employee_name, e.designation "
					"FROM `tabEmployee` e "
					"WHERE e.company = %s "
					"AND e.status = 'Active' "
					"AND e.designation = %s "
					"AND e.name NOT IN ("
					"  SELECT tr.employee FROM `tabClinic Test Request` tr "
					"  WHERE tr.test_package = %s "
					"  AND tr.scheduled_from >= %s "
					"  AND tr.employee IS NOT NULL "
					"  AND tr.employee != ''"
					") "
					"ORDER BY RAND() "
					"LIMIT %s"
				)
				params = (COMPANY, desig, package, year_start, PICK_COUNT)
				candidates = frappe.db.sql(query, params, as_dict=True)

				created = 0
				sent = 0
				failed = 0

				for emp in candidates:
					req_doc = frappe.new_doc("Clinic Test Request")
					req_doc.member_type = "Active"
					req_doc.employee = emp.get("name")
					req_doc.payroll_number = employee_payroll_id(emp)
					req_doc.status = "Pending"
					req_doc.test_package = package
					req_doc.scheduled_from = today
					req_doc.scheduled_to = window_end
					req_doc.notes = "Statutory annual medical tests"
					req_doc.insert(ignore_permissions=True)
					created = created + 1

					if base_url and test_request_endpoint:
						payload = {
							"requestId": req_doc.name,
							"memberIdentifier": employee_payroll_id(emp),
							"memberType": "Active",
							"testPackage": package.replace(" ", ""),
							"scheduledWindow": {"from": today, "to": window_end},
							"notes": req_doc.notes,
						}
						try:
							make_post_request(base_url + test_request_endpoint, headers=headers, json=payload)
							sent = sent + 1
						except Exception:
							failed = failed + 1

				results.append({
					"package": package,
					"designation": desig,
					"eligible": len(candidates),
					"created": created,
					"sent": sent,
					"failed": failed,
					"employees": [{"name": e.get("name"), "employee_name": e.get("employee_name")} for e in candidates],
				})

		resp = {"status": "statutory tests complete", "results": results}

	else:
		resp = {"error": "Unknown action: %s" % action}

	# ─── AUDIT LOG ────────────────────────────────────────────────────
	# Record every COVA action's outcome (success responses included, not just
	# exceptions) in the Error Log so the full request/response history is visible.
	try:
		cova_status = resp.get("status") if isinstance(resp, dict) else None
		frappe.log_error(
			title=("COVA " + str(action or "unknown") + " · " + str(cova_status or "done"))[:140],
			message=json.dumps({"action": action, "request": data, "response": resp}, default=str, indent=2),
		)
	except Exception:
		pass

	frappe.response["message"] = resp
	return resp


# ─── health / disease report endpoint ───────────────────────────────────────


@frappe.whitelist()
def clinic_disease_report():
	"""Pivot the Health Monthly Report / Health Report data into a condition x
	month matrix plus a multi-year monthly trend and filter option lists.

	HR-only — the /health-report portal page gates on the same check, and this
	guard stops the endpoint being read directly by non-HR sessions."""
	assert_health_report_access()

	data = frappe.request.get_json() or {}
	year = data.get("year")
	f_month = (data.get("month") or "").upper()
	f_posting_date = data.get("posting_date")
	f_medical_case = data.get("medical_case")

	month_order = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

	# Build WHERE clause with named params
	filters = {}
	clauses = []

	if year:
		clauses.append("YEAR(hmr.posting_date) = %(year)s")
		filters["year"] = year
	if f_month:
		clauses.append("UPPER(hmr.month) = %(month)s")
		filters["month"] = f_month
	if f_posting_date:
		clauses.append("hmr.posting_date = %(posting_date)s")
		filters["posting_date"] = f_posting_date
	if f_medical_case:
		clauses.append("hr.medical_case = %(medical_case)s")
		filters["medical_case"] = f_medical_case

	range_clauses, range_params = _range_clauses("hmr.posting_date", data)
	clauses.extend(range_clauses)
	filters.update(range_params)

	where_clause = ""
	if clauses:
		where_clause = " WHERE " + " AND ".join(clauses)

	query = (
		"SELECT hmr.month AS month, hr.medical_case AS m_condition, SUM(hr.case_count) AS cnt "
		"FROM `tabHealth Monthly Report` hmr "
		"INNER JOIN `tabHealth Report` hr ON hr.parent = hmr.name"
		+ where_clause
		+ " GROUP BY hmr.month, hr.medical_case"
	)

	rows = frappe.db.sql(query, filters, as_dict=True)

	months_present = []
	for m in month_order:
		has = False
		for r in rows:
			if (r.get("month") or "").upper() == m:
				has = True
		if has:
			months_present.append(m)

	matrix = {}
	conditions = []
	for r in rows:
		cond = r.get("m_condition") or "Unspecified"
		mon = (r.get("month") or "").upper()
		# SUM() comes back as Decimal; these are rendered verbatim in the grid.
		cnt = int(r.get("cnt") or 0)
		if cond not in matrix:
			matrix[cond] = {}
			conditions.append(cond)
		matrix[cond][mon] = (matrix[cond].get(mon) or 0) + cnt

	grand_total = 0
	for cond in conditions:
		for m in months_present:
			grand_total = grand_total + (matrix[cond].get(m) or 0)

	num_months = len(months_present) if months_present else 1

	table = []
	for cond in conditions:
		cells = []
		row_total = 0
		for m in months_present:
			v = matrix[cond].get(m) or 0
			cells.append(v)
			row_total = row_total + v
		pct = round((row_total * 100.0) / grand_total, 1) if grand_total else 0
		avg = round((row_total * 1.0) / num_months)
		table.append({"condition": cond, "cells": cells, "total": row_total, "percent": pct, "average": avg})

	table_sorted = sorted(table, key=lambda x: x["total"], reverse=True)

	col_totals = []
	for idx in range(len(months_present)):
		s = 0
		for t in table_sorted:
			s = s + t["cells"][idx]
		col_totals.append(s)

	# ─── YEARLY COMPARISON: monthly totals per year (unfiltered by year) ───
	trend_query = (
		"SELECT YEAR(hmr.posting_date) AS yr, UPPER(hmr.month) AS month, SUM(hr.case_count) AS cnt "
		"FROM `tabHealth Monthly Report` hmr "
		"INNER JOIN `tabHealth Report` hr ON hr.parent = hmr.name "
		"GROUP BY YEAR(hmr.posting_date), hmr.month "
		"ORDER BY yr, FIELD(UPPER(hmr.month),'JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC')"
	)
	trend_rows = frappe.db.sql(trend_query, as_dict=True)

	yearly_trend = {}
	trend_years = []
	for tr in trend_rows:
		yr = str(tr.get("yr") or "")
		mon = (tr.get("month") or "").upper()
		cnt = int(tr.get("cnt") or 0)
		if yr not in yearly_trend:
			yearly_trend[yr] = {}
			trend_years.append(yr)
		yearly_trend[yr][mon] = cnt

	trend_years_sorted = sorted(trend_years)
	# Only show months up to the current month (no future zeros)
	current_month_idx = int(frappe.utils.nowdate().split("-")[1])
	trend_months = month_order[:current_month_idx]

	trend_data = []
	for yr in trend_years_sorted:
		monthly_vals = []
		for m in trend_months:
			monthly_vals.append(yearly_trend[yr].get(m) or 0)
		trend_data.append({"year": yr, "values": monthly_vals})

	# ─── FILTER OPTIONS ───
	all_years_rows = frappe.db.sql(
		"SELECT DISTINCT YEAR(posting_date) AS yr FROM `tabHealth Monthly Report` ORDER BY yr DESC", as_dict=True
	)
	all_years = [str(r.get("yr")) for r in all_years_rows if r.get("yr")]

	all_months_rows = frappe.db.sql(
		"SELECT DISTINCT UPPER(month) AS month FROM `tabHealth Monthly Report`", as_dict=True
	)
	all_months = []
	for m in month_order:
		for r in all_months_rows:
			if r.get("month") == m:
				all_months.append(m)

	all_dates_rows = frappe.db.sql(
		"SELECT DISTINCT posting_date FROM `tabHealth Monthly Report` ORDER BY posting_date DESC", as_dict=True
	)
	all_dates = [str(r.get("posting_date")) for r in all_dates_rows if r.get("posting_date")]

	all_cases_rows = frappe.db.sql(
		"SELECT DISTINCT medical_case FROM `tabHealth Report` ORDER BY medical_case ASC", as_dict=True
	)
	all_cases = [r.get("medical_case") for r in all_cases_rows if r.get("medical_case")]

	resp = {
		"months": months_present,
		"rows": table_sorted,
		"col_totals": col_totals,
		"grand_total": grand_total,
		"monthly_average": round((grand_total * 1.0) / num_months),
		"condition_count": len(conditions),
		"trend": {"months": trend_months, "years": trend_data},
		"filter_options": {
			"years": all_years,
			"months": all_months,
			"posting_dates": all_dates,
			"medical_cases": all_cases,
		},
	}
	frappe.response["message"] = resp
	return resp


# ─── clinic dashboards ──────────────────────────────────────────────────────
# Read-only aggregates for the /health-report portal page, behind the same
# HR-only guard as the disease report:
#   POST /api/method/cova_clinic_integration.api.clinic_checkin_report
#   POST /api/method/cova_clinic_integration.api.clinic_test_request_report
#   POST /api/method/cova_clinic_integration.api.clinic_test_result_report

MONTH_LABELS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _dashboard_request():
	"""Parse the shared {year, month} filter body used by every dashboard."""
	data = frappe.request.get_json() or {}
	year = data.get("year") or None
	month = (data.get("month") or "").upper()
	month_num = MONTH_LABELS.index(month) + 1 if month in MONTH_LABELS else None
	return data, year, month_num


def _period_clauses(field, year, month_num):
	"""Year/month restriction on a date column. ``field`` is always a literal
	written here, never user input."""
	clauses = []
	params = {}
	if year:
		clauses.append(f"YEAR({field}) = %(year)s")
		params["year"] = year
	if month_num:
		clauses.append(f"MONTH({field}) = %(month_num)s")
		params["month_num"] = month_num
	return clauses, params


def _range_clauses(field, data, prefix=""):
	"""from_date / to_date restriction, as sent by the date-picker filters.
	``field`` is always a literal written here, never user input; the dates are
	parsed through getdate() so anything unparseable is rejected outright."""
	clauses = []
	params = {}
	for key, op in (("from_date", ">="), ("to_date", "<=")):
		raw = data.get(key)
		if not raw:
			continue
		try:
			value = frappe.utils.getdate(raw)
		except Exception:
			frappe.throw(_("{0} is not a valid date.").format(frappe.bold(raw)))
		name = prefix + key
		clauses.append(f"DATE({field}) {op} %({name})s")
		params[name] = str(value)
	return clauses, params


def _where(clauses):
	return (" WHERE " + " AND ".join(clauses)) if clauses else ""


def registered_employee_options():
	"""Active employees that are registered with COVA — either carrying the
	covaMemberId written back on registration, or holding an active Cova Members
	record. Scoped to the company picked in Cova Clinic Settings when there is
	one, so the list matches the population the integration syncs."""
	from cova_clinic_integration.cova_clinic_integration.doctype.cova_clinic_settings.cova_clinic_settings import (
		get_clinic_company,
	)

	clauses = ["e.status = 'Active'"]
	params = {}

	company = get_clinic_company()
	if company:
		clauses.append("e.company = %(company)s")
		params["company"] = company

	# The field is installed by the after_install / after_migrate hook; guard so
	# a site part-way through a deploy degrades to the Cova Members check alone.
	registered = ["EXISTS (SELECT 1 FROM `tabCova Members` cm WHERE cm.employee = e.name "
				  "AND cm.status = 'Active')"]
	if frappe.db.has_column("Employee", "cova_member_id"):
		registered.insert(0, "COALESCE(e.cova_member_id, '') != ''")
	clauses.append("(" + " OR ".join(registered) + ")")

	rows = frappe.db.sql(
		"SELECT e.name AS value, e.employee_name AS label, e.employee_number AS payroll_number "
		"FROM `tabEmployee` e" + _where(clauses) + " ORDER BY e.employee_name ASC LIMIT 5000",
		params,
		as_dict=True,
	)
	for r in rows:
		# Payroll number disambiguates the namesakes every payroll has.
		if r.get("payroll_number"):
			r["label"] = f"{r['label']} · {r['payroll_number']}"
		r.pop("payroll_number", None)
	return rows


def _month_series(rows):
	"""Expand [{'m': 3, 'cnt': 5}] into a dense 12-month series."""
	values = [0] * 12
	for r in rows:
		m = int(r.get("m") or 0)
		if 1 <= m <= 12:
			values[m - 1] = int(r.get("cnt") or 0)
	return {"months": list(MONTH_LABELS), "values": values}


def _pct(part, whole):
	return round((part * 100.0) / whole, 1) if whole else 0


@frappe.whitelist()
def clinic_checkin_report():
	"""Clinic Checkin carries two unrelated payloads on one doctype:

	* **biometric** punches (``b_employee`` / ``log_type`` / ``time``) — the
	  turnstile-style log used to count how many employees visited the clinic;
	* **sick-off** records (``employee`` + ``start_date`` + ``end_date``, the
	  API tab) — the ones whose controller raises a Sick Leave Application.

	Both are summarised here so the dashboard can present them separately."""
	assert_health_report_access()
	data, year, month_num = _dashboard_request()

	# ── biometric punches ────────────────────────────────────────────
	# The visits view filters by an explicit date range (date pickers); the
	# sick-off view still uses year/month. Both sets are honoured here.
	bio_clauses, bio_params = _period_clauses("cc.time", year, month_num)
	range_clauses, range_params = _range_clauses("cc.time", data)
	bio_clauses.extend(range_clauses)
	bio_params.update(range_params)
	bio_clauses.insert(0, "cc.b_employee IS NOT NULL AND cc.b_employee != ''")
	bio_where = _where(bio_clauses)

	bio_totals = frappe.db.sql(
		"SELECT COUNT(*) AS punches, COUNT(DISTINCT cc.b_employee) AS employees, "
		"SUM(CASE WHEN cc.log_type = 'IN' THEN 1 ELSE 0 END) AS in_punches, "
		"SUM(CASE WHEN cc.log_type = 'OUT' THEN 1 ELSE 0 END) AS out_punches, "
		"COUNT(DISTINCT DATE(cc.time)) AS days "
		"FROM `tabClinic Checkin` cc" + bio_where,
		bio_params,
		as_dict=True,
	)[0]

	bio_punches = int(bio_totals.get("punches") or 0)
	bio_days = int(bio_totals.get("days") or 0)

	bio_by_month = frappe.db.sql(
		"SELECT MONTH(cc.time) AS m, COUNT(*) AS cnt FROM `tabClinic Checkin` cc"
		+ bio_where
		+ " GROUP BY MONTH(cc.time)",
		bio_params,
		as_dict=True,
	)

	# Split by log type so the chart shows when people arrive versus leave.
	bio_by_hour_rows = frappe.db.sql(
		"SELECT HOUR(cc.time) AS h, "
		"SUM(CASE WHEN cc.log_type = 'OUT' THEN 0 ELSE 1 END) AS in_cnt, "
		"SUM(CASE WHEN cc.log_type = 'OUT' THEN 1 ELSE 0 END) AS out_cnt "
		"FROM `tabClinic Checkin` cc" + bio_where + " GROUP BY HOUR(cc.time)",
		bio_params,
		as_dict=True,
	)
	hours_in = [0] * 24
	hours_out = [0] * 24
	for r in bio_by_hour_rows:
		h = r.get("h")
		if h is not None and 0 <= int(h) <= 23:
			hours_in[int(h)] = int(r.get("in_cnt") or 0)
			hours_out[int(h)] = int(r.get("out_cnt") or 0)

	bio_top = frappe.db.sql(
		"SELECT cc.b_employee AS employee, COALESCE(e.employee_name, cc.b_employee) AS employee_name, "
		"MAX(cc.employee_payroll_number) AS payroll_number, COUNT(*) AS visits, "
		"SUM(CASE WHEN cc.log_type = 'OUT' THEN 0 ELSE 1 END) AS in_punches, "
		"SUM(CASE WHEN cc.log_type = 'OUT' THEN 1 ELSE 0 END) AS out_punches, "
		"MIN(CASE WHEN cc.log_type = 'OUT' THEN NULL ELSE cc.time END) AS first_in, "
		"MAX(CASE WHEN cc.log_type = 'OUT' THEN cc.time ELSE NULL END) AS last_out, "
		"MAX(cc.time) AS last_visit "
		"FROM `tabClinic Checkin` cc LEFT JOIN `tabEmployee` e ON e.name = cc.b_employee"
		+ bio_where
		+ " GROUP BY cc.b_employee, e.employee_name"
		" ORDER BY visits DESC LIMIT 15",
		bio_params,
		as_dict=True,
	)
	for r in bio_top:
		for k in ("visits", "in_punches", "out_punches"):
			r[k] = int(r.get(k) or 0)
		for k in ("last_visit", "first_in", "last_out"):
			r[k] = str(r[k])[:16] if r.get(k) else ""

	# ── sick-off records (the ones that raise a Leave Application) ────
	so_clauses, so_params = _period_clauses("cc.start_date", year, month_num)
	so_range_clauses, so_range_params = _range_clauses("cc.start_date", data, prefix="so_")
	so_clauses.extend(so_range_clauses)
	so_params.update(so_range_params)
	so_clauses.insert(0, "cc.employee IS NOT NULL AND cc.employee != ''")
	so_clauses.insert(1, "cc.start_date IS NOT NULL")
	so_clauses.insert(2, "cc.end_date IS NOT NULL")
	so_where = _where(so_clauses)

	so_totals = frappe.db.sql(
		"SELECT COUNT(*) AS records, COUNT(DISTINCT cc.employee) AS employees, "
		"SUM(DATEDIFF(cc.end_date, cc.start_date) + 1) AS days, "
		"SUM(CASE WHEN cc.leave_application IS NOT NULL AND cc.leave_application != '' THEN 1 ELSE 0 END) AS with_leave "
		"FROM `tabClinic Checkin` cc" + so_where,
		so_params,
		as_dict=True,
	)[0]

	so_records = int(so_totals.get("records") or 0)
	so_days = int(so_totals.get("days") or 0)
	so_with_leave = int(so_totals.get("with_leave") or 0)

	so_by_month = frappe.db.sql(
		"SELECT MONTH(cc.start_date) AS m, COUNT(*) AS cnt FROM `tabClinic Checkin` cc"
		+ so_where
		+ " GROUP BY MONTH(cc.start_date)",
		so_params,
		as_dict=True,
	)

	duration_rows = frappe.db.sql(
		"SELECT CASE "
		"  WHEN DATEDIFF(cc.end_date, cc.start_date) + 1 = 1 THEN '1 day' "
		"  WHEN DATEDIFF(cc.end_date, cc.start_date) + 1 = 2 THEN '2 days' "
		"  WHEN DATEDIFF(cc.end_date, cc.start_date) + 1 = 3 THEN '3 days' "
		"  WHEN DATEDIFF(cc.end_date, cc.start_date) + 1 <= 7 THEN '4-7 days' "
		"  ELSE '8+ days' END AS bucket, COUNT(*) AS cnt "
		"FROM `tabClinic Checkin` cc" + so_where + " GROUP BY bucket",
		so_params,
		as_dict=True,
	)
	bucket_order = ["1 day", "2 days", "3 days", "4-7 days", "8+ days"]
	bucket_map = {r["bucket"]: int(r["cnt"] or 0) for r in duration_rows}
	durations = {"labels": bucket_order, "values": [bucket_map.get(b, 0) for b in bucket_order]}

	so_top = frappe.db.sql(
		"SELECT cc.employee AS employee, COALESCE(e.employee_name, cc.full_name, cc.employee) AS employee_name, "
		"MAX(cc.payroll_number) AS payroll_number, COUNT(*) AS episodes, "
		"SUM(DATEDIFF(cc.end_date, cc.start_date) + 1) AS days "
		"FROM `tabClinic Checkin` cc LEFT JOIN `tabEmployee` e ON e.name = cc.employee"
		+ so_where
		+ " GROUP BY cc.employee, e.employee_name, cc.full_name"
		" ORDER BY days DESC LIMIT 15",
		so_params,
		as_dict=True,
	)
	for r in so_top:
		r["days"] = int(r.get("days") or 0)
		r["episodes"] = int(r.get("episodes") or 0)

	# ── filter options: every year either payload appears in ─────────
	year_rows = frappe.db.sql(
		"SELECT DISTINCT YEAR(`time`) AS yr FROM `tabClinic Checkin` WHERE `time` IS NOT NULL "
		"UNION SELECT DISTINCT YEAR(start_date) FROM `tabClinic Checkin` WHERE start_date IS NOT NULL "
		"ORDER BY yr DESC",
		as_dict=True,
	)

	resp = {
		"biometric": {
			"kpis": {
				"punches": bio_punches,
				"employees": int(bio_totals.get("employees") or 0),
				"in_punches": int(bio_totals.get("in_punches") or 0),
				"out_punches": int(bio_totals.get("out_punches") or 0),
				"days": bio_days,
				"avg_per_day": round((bio_punches * 1.0) / bio_days, 1) if bio_days else 0,
			},
			"by_month": _month_series(bio_by_month),
			"by_hour": {
				"labels": [f"{h:02d}:00" for h in range(24)],
				"in": hours_in,
				"out": hours_out,
			},
			"top_employees": bio_top,
		},
		"sick_off": {
			"kpis": {
				"records": so_records,
				"employees": int(so_totals.get("employees") or 0),
				"days": so_days,
				"with_leave": so_with_leave,
				"without_leave": so_records - so_with_leave,
				"leave_rate": _pct(so_with_leave, so_records),
				"avg_days": round((so_days * 1.0) / so_records, 1) if so_records else 0,
			},
			"by_month": _month_series(so_by_month),
			"durations": durations,
			"top_employees": so_top,
		},
		"filter_options": {
			"years": [str(r.get("yr")) for r in year_rows if r.get("yr")],
			"months": list(MONTH_LABELS),
		},
	}
	frappe.response["message"] = resp
	return resp


@frappe.whitelist()
def clinic_visit_cost_report():
	"""Employee visits and what they cost, from Clinic Visit Cost and its
	Visit Line Item rows.

	``benefit_*`` on a visit is the balance COVA reported as *remaining* after
	that visit, so the trend follows the latest visit in each month rather than
	summing across visits — a sum of balances would be meaningless."""
	assert_health_report_access()
	data, year, month_num = _dashboard_request()

	clauses, params = _period_clauses("cv.visit_date", year, month_num)
	range_clauses, range_params = _range_clauses("cv.visit_date", data)
	clauses.extend(range_clauses)
	params.update(range_params)
	if data.get("employee"):
		clauses.append("cv.employee = %(employee)s")
		params["employee"] = data["employee"]
	where = _where(clauses)

	totals = frappe.db.sql(
		"SELECT COUNT(*) AS visits, COUNT(DISTINCT cv.employee) AS employees, "
		"COALESCE(SUM(cv.total_cost), 0) AS cost, COALESCE(AVG(cv.total_cost), 0) AS avg_cost, "
		"COALESCE(MAX(cv.total_cost), 0) AS max_cost "
		"FROM `tabClinic Visit Cost` cv" + where,
		params,
		as_dict=True,
	)[0]

	visits = int(totals.get("visits") or 0)
	cost = float(totals.get("cost") or 0)

	by_month = frappe.db.sql(
		"SELECT MONTH(cv.visit_date) AS m, COUNT(*) AS cnt, COALESCE(SUM(cv.total_cost), 0) AS cost "
		"FROM `tabClinic Visit Cost` cv" + where + " GROUP BY MONTH(cv.visit_date)",
		params,
		as_dict=True,
	)
	visit_counts = [0] * 12
	visit_cost = [0] * 12
	for r in by_month:
		m = int(r.get("m") or 0)
		if 1 <= m <= 12:
			visit_counts[m - 1] = int(r.get("cnt") or 0)
			visit_cost[m - 1] = round(float(r.get("cost") or 0), 2)

	# Spend split by what the money was spent on (the line items).
	by_purpose = frappe.db.sql(
		"SELECT COALESCE(NULLIF(li.purpose, ''), 'Unspecified') AS purpose, "
		"COUNT(*) AS items, COALESCE(SUM(li.cost), 0) AS cost "
		"FROM `tabVisit Line Item` li INNER JOIN `tabClinic Visit Cost` cv ON li.parent = cv.name"
		+ where
		+ " GROUP BY li.purpose ORDER BY cost DESC",
		params,
		as_dict=True,
	)
	for r in by_purpose:
		r["items"] = int(r.get("items") or 0)
		r["cost"] = round(float(r.get("cost") or 0), 2)
		r["percent"] = _pct(r["cost"], cost)

	# Benefit balance trend: one point per month, taken from the most recent
	# visit in that month (balances are a running remainder, not an amount).
	BENEFITS = [
		("benefit_consultation", "Consultation"),
		("benefit_pharmacy", "Pharmacy"),
		("benefit_laboratory", "Laboratory"),
		("benefit_diagnostic", "Diagnostic"),
		("benefit_specialist", "Specialist"),
	]
	latest_per_month = frappe.db.sql(
		"SELECT MONTH(cv.visit_date) AS m, "
		+ ", ".join(f"COALESCE(cv.{f}, 0) AS {f}" for f, _label in BENEFITS)
		+ " FROM `tabClinic Visit Cost` cv"
		+ _where(list(clauses) + ["cv.visit_date IS NOT NULL"])
		+ " ORDER BY cv.visit_date ASC, cv.creation ASC",
		params,
		as_dict=True,
	)
	balance_by_month = {}
	for r in latest_per_month:
		balance_by_month[int(r["m"])] = r  # later rows overwrite: last visit wins

	# A balance is a running remainder: a month with no visits leaves it
	# unchanged, so carry the last known figure forward rather than dropping to
	# zero. Months before the first visit stay null so the line starts there.
	balance_series = []
	for field, label in BENEFITS:
		values = []
		carried = None
		for m in range(1, 13):
			r = balance_by_month.get(m)
			if r is not None:
				carried = round(float(r[field]), 2)
			values.append(carried)
		balance_series.append({"label": label, "values": values})

	latest_month = max(balance_by_month) if balance_by_month else None
	latest_balances = balance_by_month.get(latest_month) if latest_month else None
	balance_total = (
		round(sum(float(latest_balances[f]) for f, _label in BENEFITS), 2) if latest_balances else 0
	)

	# Each category's own latest reading, for the balance tiles.
	latest_by_benefit = [
		{
			"label": label,
			"value": round(float(latest_balances[field]), 2) if latest_balances else 0,
		}
		for field, label in BENEFITS
	]

	top_employees = frappe.db.sql(
		"SELECT cv.employee AS employee, "
		"COALESCE(e.employee_name, cv.full_name, cv.candidate_name, cv.employee) AS employee_name, "
		"MAX(cv.payroll_number) AS payroll_number, COUNT(*) AS visits, "
		"COALESCE(SUM(cv.total_cost), 0) AS cost, MAX(cv.visit_date) AS last_visit "
		"FROM `tabClinic Visit Cost` cv LEFT JOIN `tabEmployee` e ON e.name = cv.employee"
		+ where
		+ " GROUP BY cv.employee, e.employee_name, cv.full_name, cv.candidate_name"
		" ORDER BY cost DESC LIMIT 15",
		params,
		as_dict=True,
	)
	for r in top_employees:
		r["visits"] = int(r.get("visits") or 0)
		r["cost"] = round(float(r.get("cost") or 0), 2)
		r["last_visit"] = str(r["last_visit"]) if r.get("last_visit") else ""

	# Cost per employee per month — the spend grid plus the series behind it.
	matrix_rows = frappe.db.sql(
		"SELECT cv.employee AS employee, "
		"COALESCE(e.employee_name, cv.full_name, cv.candidate_name, cv.employee) AS employee_name, "
		"MONTH(cv.visit_date) AS m, COALESCE(SUM(cv.total_cost), 0) AS cost "
		"FROM `tabClinic Visit Cost` cv LEFT JOIN `tabEmployee` e ON e.name = cv.employee"
		+ _where(list(clauses) + ["cv.visit_date IS NOT NULL"])
		+ " GROUP BY cv.employee, e.employee_name, cv.full_name, cv.candidate_name, MONTH(cv.visit_date)",
		params,
		as_dict=True,
	)

	grid = {}
	for r in matrix_rows:
		key = r["employee"] or r["employee_name"]
		row = grid.setdefault(key, {"employee": key, "employee_name": r["employee_name"], "cells": [0] * 12})
		m = int(r.get("m") or 0)
		if 1 <= m <= 12:
			row["cells"][m - 1] = round(float(r.get("cost") or 0), 2)

	cost_rows = []
	for row in grid.values():
		row["total"] = round(sum(row["cells"]), 2)
		cost_rows.append(row)
	cost_rows.sort(key=lambda x: x["total"], reverse=True)

	col_totals = [round(sum(r["cells"][i] for r in cost_rows), 2) for i in range(12)]

	opt_years = frappe.db.sql(
		"SELECT DISTINCT YEAR(visit_date) AS yr FROM `tabClinic Visit Cost` "
		"WHERE visit_date IS NOT NULL ORDER BY yr DESC",
		as_dict=True,
	)

	resp = {
		"kpis": {
			"visits": visits,
			"employees": int(totals.get("employees") or 0),
			"cost": round(cost, 2),
			"avg_cost": round(float(totals.get("avg_cost") or 0), 2),
			"max_cost": round(float(totals.get("max_cost") or 0), 2),
			"cost_per_employee": round(cost / int(totals.get("employees") or 1), 2)
			if totals.get("employees")
			else 0,
			"balance_total": balance_total,
			"balance_month": MONTH_LABELS[latest_month - 1] if latest_month else "",
		},
		"balances_latest": latest_by_benefit,
		"cost_by_employee": {
			"months": list(MONTH_LABELS),
			"rows": cost_rows,
			"col_totals": col_totals,
			"grand_total": round(sum(col_totals), 2),
		},
		"by_month": {"months": list(MONTH_LABELS), "visits": visit_counts, "cost": visit_cost},
		"by_purpose": by_purpose,
		"balances": {"months": list(MONTH_LABELS), "series": balance_series},
		"top_employees": top_employees,
		"filter_options": {
			"years": [str(r.get("yr")) for r in opt_years if r.get("yr")],
			"months": list(MONTH_LABELS),
			"employees": registered_employee_options(),
		},
	}
	frappe.response["message"] = resp
	return resp


@frappe.whitelist()
def clinic_test_request_report():
	"""Clinic Test Request pipeline: what was asked of COVA and what came back.
	Scheduling dates drive the period, so a request shows up in the month it was
	scheduled for rather than the month the row happened to be created."""
	assert_health_report_access()
	data, year, month_num = _dashboard_request()

	clauses, params = _period_clauses("tr.scheduled_from", year, month_num)
	range_clauses, range_params = _range_clauses("tr.scheduled_from", data)
	clauses.extend(range_clauses)
	params.update(range_params)
	if data.get("status"):
		clauses.append("tr.status = %(status)s")
		params["status"] = data["status"]
	if data.get("test_package"):
		clauses.append("tr.test_package = %(test_package)s")
		params["test_package"] = data["test_package"]
	if data.get("member_type"):
		clauses.append("tr.member_type = %(member_type)s")
		params["member_type"] = data["member_type"]
	where = _where(clauses)

	totals = frappe.db.sql(
		"SELECT COUNT(*) AS total, "
		"SUM(CASE WHEN tr.status = 'Pending' THEN 1 ELSE 0 END) AS pending, "
		"SUM(CASE WHEN tr.status = 'Completed' THEN 1 ELSE 0 END) AS completed, "
		"SUM(CASE WHEN tr.status = 'Cancelled' THEN 1 ELSE 0 END) AS cancelled, "
		"SUM(CASE WHEN tr.status = 'Pending' AND tr.scheduled_to < CURDATE() THEN 1 ELSE 0 END) AS overdue "
		"FROM `tabClinic Test Request` tr" + where,
		params,
		as_dict=True,
	)[0]

	total = int(totals.get("total") or 0)
	completed = int(totals.get("completed") or 0)

	by_package = frappe.db.sql(
		"SELECT tr.test_package AS test_package, COUNT(*) AS total, "
		"SUM(CASE WHEN tr.status = 'Pending' THEN 1 ELSE 0 END) AS pending, "
		"SUM(CASE WHEN tr.status = 'Completed' THEN 1 ELSE 0 END) AS completed, "
		"SUM(CASE WHEN tr.status = 'Cancelled' THEN 1 ELSE 0 END) AS cancelled "
		"FROM `tabClinic Test Request` tr" + where + " GROUP BY tr.test_package ORDER BY total DESC",
		params,
		as_dict=True,
	)
	for r in by_package:
		# SUM() comes back as Decimal; the dashboard renders these verbatim.
		for k in ("total", "pending", "completed", "cancelled"):
			r[k] = int(r.get(k) or 0)
		r["completion_rate"] = _pct(r["completed"], r["total"])

	by_month = frappe.db.sql(
		"SELECT MONTH(tr.scheduled_from) AS m, COUNT(*) AS cnt FROM `tabClinic Test Request` tr"
		+ where
		+ " GROUP BY MONTH(tr.scheduled_from)",
		params,
		as_dict=True,
	)

	by_member_type = frappe.db.sql(
		"SELECT COALESCE(NULLIF(tr.member_type, ''), 'Unspecified') AS member_type, COUNT(*) AS cnt "
		"FROM `tabClinic Test Request` tr" + where + " GROUP BY tr.member_type ORDER BY cnt DESC",
		params,
		as_dict=True,
	)

	overdue_clauses = list(clauses) + ["tr.status = 'Pending'", "tr.scheduled_to < CURDATE()"]
	overdue_rows = frappe.db.sql(
		# Clinic Test Request has no full_name column, so pre-employment rows fall
		# back to the national ID they were registered with.
		"SELECT tr.name AS name, COALESCE(e.employee_name, tr.payroll_number, tr.nationa_id) AS who, "
		"tr.test_package AS test_package, tr.member_type AS member_type, tr.scheduled_to AS scheduled_to, "
		"DATEDIFF(CURDATE(), tr.scheduled_to) AS days_late "
		"FROM `tabClinic Test Request` tr LEFT JOIN `tabEmployee` e ON e.name = tr.employee"
		+ _where(overdue_clauses)
		+ " ORDER BY days_late DESC LIMIT 20",
		params,
		as_dict=True,
	)
	for r in overdue_rows:
		r["scheduled_to"] = str(r["scheduled_to"]) if r.get("scheduled_to") else ""

	opt_years = frappe.db.sql(
		"SELECT DISTINCT YEAR(scheduled_from) AS yr FROM `tabClinic Test Request` "
		"WHERE scheduled_from IS NOT NULL ORDER BY yr DESC",
		as_dict=True,
	)
	opt_packages = frappe.db.sql(
		"SELECT DISTINCT test_package FROM `tabClinic Test Request` WHERE test_package IS NOT NULL "
		"AND test_package != '' ORDER BY test_package",
		as_dict=True,
	)

	resp = {
		"kpis": {
			"total": total,
			"pending": int(totals.get("pending") or 0),
			"completed": completed,
			"cancelled": int(totals.get("cancelled") or 0),
			"overdue": int(totals.get("overdue") or 0),
			"completion_rate": _pct(completed, total),
		},
		"by_package": by_package,
		"by_month": _month_series(by_month),
		"by_member_type": by_member_type,
		"overdue_rows": overdue_rows,
		"filter_options": {
			"years": [str(r.get("yr")) for r in opt_years if r.get("yr")],
			"months": list(MONTH_LABELS),
			"statuses": ["Pending", "Completed", "Cancelled"],
			"test_packages": [r.get("test_package") for r in opt_packages],
			"member_types": ["Active", "Pre Employment"],
		},
	}
	frappe.response["message"] = resp
	return resp


@frappe.whitelist()
def clinic_test_result_report():
	"""Clinic Test Result outcomes plus the per-condition risk grades that COVA
	returns on the child ``Test Result`` table. Results have no posting date of
	their own, so the period runs off ``creation``."""
	assert_health_report_access()
	data, year, month_num = _dashboard_request()

	clauses, params = _period_clauses("ctr.creation", year, month_num)
	range_clauses, range_params = _range_clauses("ctr.creation", data)
	clauses.extend(range_clauses)
	params.update(range_params)
	if data.get("test_package"):
		clauses.append("ctr.test_package = %(test_package)s")
		params["test_package"] = data["test_package"]
	if data.get("clinical_outcome"):
		clauses.append("ctr.clinical_outcome = %(clinical_outcome)s")
		params["clinical_outcome"] = data["clinical_outcome"]
	if data.get("member_type"):
		clauses.append("ctr.member_type = %(member_type)s")
		params["member_type"] = data["member_type"]
	where = _where(clauses)

	totals = frappe.db.sql(
		"SELECT COUNT(*) AS total, COUNT(DISTINCT ctr.employee) AS employees, "
		"SUM(CASE WHEN ctr.clinical_outcome = 'FitForWork' THEN 1 ELSE 0 END) AS fit, "
		"SUM(CASE WHEN ctr.clinical_outcome = 'FitWithRestrictions' THEN 1 ELSE 0 END) AS restricted, "
		"SUM(CASE WHEN ctr.clinical_outcome = 'UnfitForWork' THEN 1 ELSE 0 END) AS unfit, "
		"SUM(CASE WHEN ctr.clinical_outcome = 'InconclusiveRetestRequired' THEN 1 ELSE 0 END) AS retest "
		"FROM `tabClinic Test Result` ctr" + where,
		params,
		as_dict=True,
	)[0]

	total = int(totals.get("total") or 0)

	by_outcome = frappe.db.sql(
		"SELECT COALESCE(NULLIF(ctr.clinical_outcome, ''), 'Unspecified') AS clinical_outcome, COUNT(*) AS cnt "
		"FROM `tabClinic Test Result` ctr" + where + " GROUP BY ctr.clinical_outcome ORDER BY cnt DESC",
		params,
		as_dict=True,
	)
	for r in by_outcome:
		r["percent"] = _pct(int(r.get("cnt") or 0), total)

	by_package = frappe.db.sql(
		"SELECT COALESCE(NULLIF(ctr.test_package, ''), 'Unspecified') AS test_package, COUNT(*) AS cnt "
		"FROM `tabClinic Test Result` ctr" + where + " GROUP BY ctr.test_package ORDER BY cnt DESC",
		params,
		as_dict=True,
	)

	by_month = frappe.db.sql(
		"SELECT MONTH(ctr.creation) AS m, COUNT(*) AS cnt FROM `tabClinic Test Result` ctr"
		+ where
		+ " GROUP BY MONTH(ctr.creation)",
		params,
		as_dict=True,
	)

	# Risk grades live on the child rows; join back so the same filters apply.
	risk_rows = frappe.db.sql(
		"SELECT COALESCE(NULLIF(t.test, ''), 'Unspecified') AS medical_case, "
		"COALESCE(NULLIF(t.select_tezd, ''), 'No Risk') AS risk, COUNT(*) AS cnt "
		"FROM `tabTest Result` t INNER JOIN `tabClinic Test Result` ctr ON t.parent = ctr.name"
		+ where
		+ " GROUP BY t.test, t.select_tezd",
		params,
		as_dict=True,
	)

	risk_levels = ["High Risk", "Medium Risk", "Low Risk", "No Risk"]
	matrix = {}
	for r in risk_rows:
		case = r["medical_case"]
		matrix.setdefault(case, dict.fromkeys(risk_levels, 0))
		if r["risk"] in matrix[case]:
			matrix[case][r["risk"]] += int(r.get("cnt") or 0)

	risk_matrix = []
	for case, counts in matrix.items():
		row_total = sum(counts.values())
		risk_matrix.append(
			{
				"medical_case": case,
				"cells": [counts[level] for level in risk_levels],
				"total": row_total,
			}
		)
	risk_matrix.sort(key=lambda x: x["total"], reverse=True)

	risk_totals = [sum(row["cells"][i] for row in risk_matrix) for i in range(len(risk_levels))]

	opt_years = frappe.db.sql(
		"SELECT DISTINCT YEAR(creation) AS yr FROM `tabClinic Test Result` ORDER BY yr DESC",
		as_dict=True,
	)
	opt_packages = frappe.db.sql(
		"SELECT DISTINCT test_package FROM `tabClinic Test Result` WHERE test_package IS NOT NULL "
		"AND test_package != '' ORDER BY test_package",
		as_dict=True,
	)
	opt_outcomes = frappe.db.sql(
		"SELECT DISTINCT clinical_outcome FROM `tabClinic Test Result` WHERE clinical_outcome IS NOT NULL "
		"AND clinical_outcome != '' ORDER BY clinical_outcome",
		as_dict=True,
	)

	resp = {
		"kpis": {
			"total": total,
			"employees": int(totals.get("employees") or 0),
			"fit": int(totals.get("fit") or 0),
			"restricted": int(totals.get("restricted") or 0),
			"unfit": int(totals.get("unfit") or 0),
			"retest": int(totals.get("retest") or 0),
			"fit_rate": _pct(int(totals.get("fit") or 0), total),
		},
		"by_outcome": by_outcome,
		"by_package": by_package,
		"by_month": _month_series(by_month),
		"risk": {"levels": risk_levels, "rows": risk_matrix, "totals": risk_totals},
		"filter_options": {
			"years": [str(r.get("yr")) for r in opt_years if r.get("yr")],
			"months": list(MONTH_LABELS),
			"test_packages": [r.get("test_package") for r in opt_packages],
			"clinical_outcomes": [r.get("clinical_outcome") for r in opt_outcomes],
			"member_types": ["Active", "Pre Employment"],
		},
	}
	frappe.response["message"] = resp
	return resp


# ─── clinic data (sick-off) read/write endpoint ─────────────────────────────
# Public URL preserved via hooks.override_whitelisted_methods:
#   GET  /api/method/getClinicData?start_date=&end_date=  -> fetch by date range
#   POST /api/method/getClinicData  (JSON body)           -> create a record
# Writes to the local `Clinic Checkin` doctype (the live counterpart is
# `Clinic Data`; both carry the same employee / start_date / end_date / reason /
# time_in / time_out sick-off fields). Inserting a record triggers the Clinic
# Checkin controller's dedup (before_insert) and sick-leave creation (after_insert).


@frappe.whitelist()
def get_clinic_data():
	method = frappe.request.method

	# ── GET: fetch Clinic Checkin sick-off records by creation date range ──
	if method == "GET":
		try:
			start_date_raw = frappe.request.args.get("start_date")
			end_date_raw = frappe.request.args.get("end_date")

			if not start_date_raw or not end_date_raw:
				frappe.throw("Missing required query parameters: 'start_date' and 'end_date'")

			try:
				start_date = frappe.utils.getdate(start_date_raw)
				end_date = frappe.utils.getdate(end_date_raw)
				if end_date < start_date:
					frappe.throw(
						"End date ({0}) cannot be before start date ({1})".format(end_date, start_date)
					)
			except Exception as date_error:
				frappe.throw("Invalid date format: {0}".format(str(date_error)))

			records = frappe.db.sql(
				"""
				SELECT employee, start_date, end_date, reason, time_in, time_out
				FROM `tabClinic Checkin`
				WHERE DATE(creation) >= %(start_date)s
				AND DATE(creation) <= %(end_date)s
				ORDER BY creation ASC
			""",
				{"start_date": str(start_date), "end_date": str(end_date)},
				as_dict=True,
			)

			for record in records:
				record["start_date"] = str(record["start_date"])
				record["end_date"] = str(record["end_date"])
				record["time_in"] = str(record["time_in"]) if record.get("time_in") else None
				record["time_out"] = str(record["time_out"]) if record.get("time_out") else None

			frappe.response.pop("docs", None)
			frappe.response["status"] = "success"
			frappe.response["count"] = len(records)
			frappe.response["data"] = records
			frappe.response.http_status_code = 200

		except Exception as e:
			frappe.log_error(title="Clinic Data API GET Error", message=str(e))
			frappe.response.update({"status": "error", "message": "Failed to fetch records: {0}".format(str(e))})
			frappe.response.http_status_code = 500

	# ── POST: create a Clinic Checkin record (no dedup blockage) ──
	elif method == "POST":
		if not frappe.request.content_type or "application/json" not in frappe.request.content_type:
			frappe.throw("Request must be JSON with Content-Type: application/json")
		try:
			data = frappe.request.get_json() or {}

			employee_id = data.get("employee")
			if not employee_id:
				frappe.throw("Missing required field: 'employee'")
			if not frappe.db.exists("Employee", employee_id):
				frappe.throw(
					"Employee '{0}' does not exist. Please provide a valid Employee ID.".format(employee_id)
				)

			# start_date / end_date are OPTIONAL. A payload without both dates creates
			# a plain check-in record and does NOT trigger a Leave Application (the
			# controller's after_insert guards on both dates being present). Validate
			# the window only when both are supplied.
			start_date = data.get("start_date")
			end_date = data.get("end_date")
			if start_date and end_date:
				try:
					start_date = frappe.utils.getdate(start_date)
					end_date = frappe.utils.getdate(end_date)
					if end_date < start_date:
						frappe.throw(
							"End date ({0}) cannot be before start date ({1})".format(end_date, start_date)
						)
				except Exception as date_error:
					frappe.throw("Invalid date format: {0}".format(str(date_error)))

			# Always insert — inserts and saves are allowed without dedup blockage.
			doc = frappe.get_doc(
				{
					"doctype": "Clinic Checkin",
					"employee": employee_id,
					"start_date": start_date or None,
					"end_date": end_date or None,
					"reason": data.get("reason"),
					"time_in": data.get("time_in"),
					"time_out": data.get("time_out"),
				}
			).insert(ignore_permissions=True)

			frappe.response.pop("docs", None)
			frappe.db.commit()

			frappe.response.update(
				{
					"status": "success",
					"message": "Clinic data saved successfully",
					"data": {
						"id": doc.name,
						"employee": doc.employee,
						"leave_application": doc.leave_application,
					},
				}
			)
			frappe.response.http_status_code = 201

		except Exception as e:
			frappe.log_error(title="Clinic Data API POST Error", message=str(e))
			frappe.response.update({"status": "error", "message": "Failed to process request: {0}".format(str(e))})
			frappe.response.http_status_code = 500

	else:
		frappe.response.update(
			{"status": "error", "message": "Method '{0}' not allowed. Supported methods: GET, POST".format(method)}
		)
		frappe.response.http_status_code = 405


# ─── XLSX export for the Clinic Analytics portal page ──────────────────────
# The page builds the visible rows client-side (headers + body, already
# filtered), POSTs them here and gets a real .xlsx back. Same HR-only gate as
# the reports the rows came from.


@frappe.whitelist()
def clinic_report_xlsx():
	"""Turn a grid of rows POSTed by the Clinic Analytics page into an Excel
	download. Expects form fields `rows` (JSON list of lists) and `title`."""
	assert_health_report_access()

	from frappe.desk.utils import provide_binary_file
	from frappe.utils.xlsxutils import make_xlsx

	try:
		rows = json.loads(frappe.form_dict.get("rows") or "[]")
	except ValueError:
		frappe.throw(_("Could not read the rows to export."))

	if not isinstance(rows, list) or not rows:
		frappe.throw(_("There is nothing to export."))

	title = (frappe.form_dict.get("title") or "Clinic Report").strip() or "Clinic Report"
	# Excel sheet names cap at 31 chars and reject : \\ / ? * [ ] — a dashboard
	# title like "Visits: 2026" reaches make_xlsx otherwise and raises there.
	sheet_name = re.sub(r"[:\\/?*\[\]]", " ", title).strip()[:31] or "Clinic Report"

	# Numbers stay numeric so the sheet can be summed and charted; everything
	# else (labels, percentage strings, blanks) goes out as text. bool is
	# excluded because it is a subclass of int and reads as TRUE/FALSE.
	def cell(value):
		if value is None:
			return ""
		if isinstance(value, bool):
			return str(value)
		if isinstance(value, int | float):
			return value
		return str(value)

	data = [[cell(c) for c in row] for row in rows]

	provide_binary_file(title, "xlsx", make_xlsx(data, sheet_name).getvalue())

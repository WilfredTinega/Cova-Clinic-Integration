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

from cova_clinic_integration.cova_clinic_integration.doctype.cova_clinic_settings.cova_clinic_settings import (
	DASHBOARD_SECTIONS,
	DEFAULT_TEST_GROUPS,
	get_clinic_company,
	get_dashboard_viewers,
	get_test_groups,
)
from cova_clinic_integration.cova_clinic_integration.doctype.test_package.test_package import (
	cova_package_code,
)
from cova_clinic_integration.cova_trace import record_cova_message
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


def allowed_dashboard_sections(user: str | None = None) -> list[str]:
	"""Which Clinic Analytics sections this user may open — they carry
	employees' medical data.

	Once Cova Clinic Settings lists anyone under Dashboard Access, that list is
	the whole rule: a listed user sees exactly the sections ticked on their row,
	Administrator sees all, and nobody else sees any — whatever HR role they
	hold. No roles or permissions are involved. While the list is empty the
	HR-family rule applies to every section, so a site is not locked out of its
	dashboards before anyone is listed."""
	user = user or frappe.session.user
	if user == "Guest":
		return []
	if user == "Administrator":
		return list(DASHBOARD_SECTIONS)
	viewers = get_dashboard_viewers()
	if viewers:
		ticked = viewers.get(user, set())
		return [s for s in DASHBOARD_SECTIONS if s in ticked]
	return list(DASHBOARD_SECTIONS) if health_report_roles(user) else []


def has_health_report_access(user: str | None = None, section: str | None = None) -> bool:
	"""True when the user may open ``section`` — or, with no section given,
	at least one section of the dashboard."""
	sections = allowed_dashboard_sections(user)
	return section in sections if section else bool(sections)


def assert_health_report_access(*sections: str) -> None:
	"""Refuse unless the user may open one of ``sections`` (any section when
	none is named)."""
	allowed = allowed_dashboard_sections()
	if not (any(s in allowed for s in sections) if sections else allowed):
		frappe.throw(
			_("You are not permitted to view this part of the Clinic dashboard."),
			frappe.PermissionError,
		)


# ─── helpers ──────────────────────────────────────────────────────────────


def build_headers():
	api_key = frappe.db.get_single_value("Cova Clinic Settings", "api_key")
	headers = {}
	if api_key:
		headers["X-Api-Key"] = api_key
	return headers


def cova_post(url, headers, payload, trace=None):
	"""POST to COVA and always come back with a dict.

	``trace`` is an optional ``(doctype, name, action)`` naming the document the
	exchange belongs to. Given one, both halves of the conversation — what we
	sent and what came back — are appended to that record's **COVA Raw** field,
	which is the only place a user can see what actually went over the wire
	without Error Log access. It is done here rather than at each call site so a
	new endpoint cannot quietly skip it.

	``make_post_request`` calls ``raise_for_status()``, so any 4xx/5xx becomes an
	exception **and the response body is thrown away** — which is the one thing
	worth having, because COVA puts the reason in it. Every call site here checks
	``result.get("error")``, a contract that was therefore never met on failure:
	a single rejected row aborted the whole bulk run with a raw traceback instead
	of being counted and explained.

	Returns COVA's parsed body on success. On failure returns
	``{"error": ..., "status_code": ..., "cova_response": ...}`` — never raises,
	so a bulk sweep degrades row by row.
	"""
	if trace:
		record_cova_message(trace[0], trace[1], trace[2], "sent", payload)

	try:
		result = make_post_request(url, headers=headers, json=payload)
	except Exception as exc:
		response = getattr(exc, "response", None)
		body = None
		status_code = None
		if response is not None:
			status_code = response.status_code
			try:
				body = response.json()
			except Exception:
				body = (response.text or "")[:2000]

		# COVA states its own reason in the body; fall back to the exception text
		# (a timeout or DNS failure has no body at all).
		message = None
		if isinstance(body, dict):
			message = body.get("message") or body.get("error") or body.get("detail")
		if not message:
			message = str(exc)

		frappe.log_error(
			title=("COVA POST failed · " + str(status_code or "no response"))[:140],
			message=json.dumps(
				{"url": url, "request": payload, "status_code": status_code, "response": body},
				default=str,
				indent=2,
			),
		)
		failure = {"error": message, "status_code": status_code, "cova_response": body}
		if trace:
			record_cova_message(trace[0], trace[1], trace[2], "received", failure, status="error")
		return failure

	# A 200 is not automatically a success: COVA answers an already-enrolled
	# member with {"status": "duplicate", ...}. Hand that back as-is — callers
	# read `status` — but make sure we always return something dict-shaped, since
	# make_post_request returns None for an empty body and a str for text/plain.
	if not isinstance(result, dict):
		result = {"cova_response": result}

	if trace:
		record_cova_message(
			trace[0], trace[1], trace[2], "received", result, status=result.get("status") or "success"
		)

	return result


def cova_member_id_of(result):
	"""The member id COVA reported, whether it succeeded outright or not.

	COVA answers a partly-completed registration with **HTTP 400** and a body
	like ``{"message": "Member created but wallet provisioning failed: ",
	"covaMemberId": "CHSC00015646"}``. The member does exist at that point, so
	reading the id only from the success path loses the link: the Employee keeps
	no ``cova_member_id``, every later sweep re-registers them, and COVA answers
	"duplicate" forever. Look in the error body too.
	"""
	if not isinstance(result, dict):
		return ""
	member_id = result.get("covaMemberId")
	if not member_id:
		body = result.get("cova_response")
		if isinstance(body, dict):
			member_id = body.get("covaMemberId")
	return member_id or ""


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
	# A partly-completed registration comes back as an HTTP error whose body still
	# carries covaMemberId, walletId and the rest. Read through to it, or a member
	# COVA did create is stored here with none of its identifiers.
	source = result if isinstance(result, dict) else {}
	if not source.get("covaMemberId") and isinstance(source.get("cova_response"), dict):
		source = {**source["cova_response"], **{k: v for k, v in source.items() if k != "cova_response"}}

	# doc.save() writes only columns that physically exist, so this is safe before/after
	# the fields' columns are materialised by a deploy; never let capture break the flow.
	try:
		cm = frappe.get_doc("Cova Members", cm_name)
		for erp_field in mapping:
			v = source.get(mapping[erp_field])
			if v is not None:
				cm.set(erp_field, v)
		cm.save(ignore_permissions=True)
		# Appended after the save rather than set on the document: every COVA
		# exchange lands in cova_raw the same way, and the member keeps the
		# earlier ones instead of each registration overwriting the last.
		record_cova_message("Cova Members", cm.name, "register_member", "received", result)
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
	deactivation_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "deactivation_endpoint")
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
	result = cova_post(base_url + register_endpoint, headers, payload)
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
		"Active",
		emp.name,
		"",
		emp.employee_name,
		employee_payroll_id(emp),
		emp.cell_number or "",
		emp.gender or "",
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
	result = cova_post(base_url + deactivation_endpoint, headers, payload)
	if not result.get("error"):
		frappe.db.set_value("Employee", emp.name, "cova_deactivated", 1)
	return result


# ─── main dispatcher endpoint ───────────────────────────────────────────────


def mark_test_request_sent(request_name, result):
	"""Record on the request what COVA's reply to a submission means.

	``received_by_cova`` is the only proof on the document that the request
	reached the clinic. It is set on a success and **never cleared**: a re-send
	that fails does not un-deliver the copy COVA already accepted, and clearing
	it would send someone chasing a request that is in fact in.

	The member COVA accepted it for is not kept on the request — the request's
	``cova_member`` link already names it. The reply's ``covaMemberId`` only
	fills that Cova Members record while it has none.

	Written with db.set_value rather than a save: this runs inside the API call,
	the fields are read-only to users, and a save here would re-run validate on a
	document the caller may still be holding.
	"""
	if not request_name or not isinstance(result, dict) or result.get("error"):
		return

	frappe.db.set_value("Clinic Test Request", request_name, "received_by_cova", 1, update_modified=False)

	member_id = cova_member_id_of(result)
	member = frappe.db.get_value("Clinic Test Request", request_name, "cova_member")
	if member_id and member and not frappe.db.get_value("Cova Members", member, "cova_member_id"):
		frappe.db.set_value("Cova Members", member, "cova_member_id", member_id, update_modified=False)


def send_test_request(doc, action="submit_test_request"):
	"""Send one Clinic Test Request to COVA and record the outcome on it.

	Shared by the desk's *Send to Clinic* action and the Clinic Test Schedule.
	Returns COVA's result; a request with nothing to identify its member is not
	sent at all and comes back as ``{"error": ..., "refused": True}``.
	"""
	base_url = frappe.db.get_single_value("Cova Clinic Settings", "base_url")
	test_request_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "test_request_endpoint")
	headers = build_headers()

	if doc.member_type == "Active":
		# `payroll_number` is filled on validate, so a request that predates
		# that (or one imported straight into the table) still has it empty —
		# and COVA answers `memberIdentifier: ""` with a refusal. Fall back to
		# the same identifier the API route sends, rather than sending blank.
		member_identifier = doc.payroll_number
		if not member_identifier and doc.employee:
			member_identifier = employee_payroll_id(frappe.get_doc("Employee", doc.employee))
	else:
		member_identifier = doc.get("nationa_id") or ""

	if not member_identifier:
		return {
			"error": "No member identifier on %s: an Active request needs an Employee, "
			"a Pre Employment request needs a National ID." % doc.name,
			"refused": True,
		}

	payload = {
		"requestId": doc.name,
		"memberIdentifier": member_identifier,
		"memberType": doc.member_type.replace(" ", ""),
		"testPackage": cova_package_code(doc.test_package),
		"scheduledWindow": {
			"from": str(doc.scheduled_from) if doc.get("scheduled_from") else "",
			"to": str(doc.scheduled_to) if doc.get("scheduled_to") else "",
		},
		"notes": doc.get("notes") or "",
	}

	if doc.member_type == "Pre Employment":
		# `full_name` is not a field on the request — the pre-employment route
		# only ever set it in memory before insert. Reading it as an attribute
		# off a reloaded doc raises AttributeError, which is why re-sending a
		# candidate's request used to 500; the name lives on the Cova Member.
		full_name = doc.get("full_name") or ""
		if not full_name and doc.get("cova_member"):
			full_name = frappe.db.get_value("Cova Members", doc.cova_member, "full_name") or ""

		payload["candidateBiodata"] = {
			"fullName": full_name,
			"nationalId": doc.get("nationa_id") or "",
			"dateOfBirth": str(doc.date_of_birth) if doc.get("date_of_birth") else "",
			"gender": doc.gender or "",
			"phone": normalize_phone(doc.phone_number),
		}

	result = cova_post(
		base_url + test_request_endpoint,
		headers,
		payload,
		trace=("Clinic Test Request", doc.name, action),
	)
	mark_test_request_sent(doc.name, result)
	return result


def _test_request_error(data, message):
	"""Refuse a test submission the way the dispatcher answers everything else.

	The submission lives inside the action chain, so it cannot fall through to the
	tail of ``cova_clinic_api`` once it has to stop early. This keeps the two
	things that tail does — the Error Log entry and ``frappe.response["message"]``
	— so a refusal still reads as ``{"error": ...}`` on the desk buttons instead
	of arriving as a traceback page.
	"""
	resp = {"error": message}
	try:
		frappe.log_error(
			title="COVA submit_test_request · refused",
			message=json.dumps(
				{"action": "submit_test_request", "request": data, "response": resp},
				default=str,
				indent=2,
			),
		)
	except Exception:
		pass
	frappe.response["message"] = resp
	return resp


@frappe.whitelist()
def cova_clinic_api():
	"""Single POST entry point for every COVA action. The action is selected by
	the ``action`` key in the JSON body."""
	data = frappe.request.get_json() or {}
	action = data.get("action")
	resp = None

	# Document names are strings. A caller that sends a payroll-style id as a
	# JSON number ("employee": 101253) would otherwise reach the ORM as an int,
	# and `name = 101253` against a varchar column makes MySQL cast every row —
	# which errors outright on the ids that are not numeric.
	for key in ("employee", "job_applicant", "cova_member"):
		if isinstance(data.get(key), int | float):
			data[key] = str(data[key])

	# ─── 1. REGISTER MEMBER (single) ──────────────────────────────────
	if action == "register_member":
		base_url = frappe.db.get_single_value("Cova Clinic Settings", "base_url")
		register_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "register_endpoint")
		headers = build_headers()

		member_type = data.get("member_type")
		employee = data.get("employee")
		emp = None

		if member_type == "Active":
			if not employee:
				resp = {"error": "employee is required for Active member registration"}
			else:
				try:
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
				except frappe.DoesNotExistError:
					resp = {"error": f"Employee {employee} does not exist"}
		else:
			payload = {
				"schemeType": "PreEmployment",
				"nationalId": data.get("nationa_id"),
				"fullName": data.get("full_name"),
				"dateOfBirth": data.get("date_of_birth"),
				"gender": data.get("gender"),
				"phone": normalize_phone(data.get("phone_number")),
			}

		if not resp:
			result = cova_post(base_url + register_endpoint, headers, payload)

			# Written even when COVA returned an error status: a "wallet
			# provisioning failed" 400 still carries a real member id, and
			# dropping it would make every later run re-register the employee.
			member_id = cova_member_id_of(result)
			if member_type == "Active" and member_id:
				try:
					frappe.db.set_value("Employee", employee, "cova_member_id", member_id)
				except Exception:
					pass

			# Registering again clears the deactivation flag, whether or not Cova
			# returned a member id (a mocked or partial reply still means registered).
			if member_type == "Active" and employee and not result.get("error"):
				frappe.db.set_value("Employee", employee, "cova_deactivated", 0)

			# Create/refresh the Cova Member and store COVA's registration response on it.
			if member_type == "Active":
				cm_name = create_or_update_cova_member(
					"Active",
					employee,
					"",
					emp.employee_name,
					employee_payroll_id(emp),
					emp.cell_number or "",
					emp.gender or "",
					activate=not result.get("error"),
				)
			else:
				cm_name = create_or_update_cova_member(
					"Pre Employment",
					"",
					data.get("nationa_id") or "",
					data.get("full_name") or "",
					"",
					data.get("phone_number") or "",
					data.get("gender") or "",
					activate=not result.get("error"),
				)
			save_cova_response(cm_name, result)

			# COVA's own reply is the response. Dropping this line made the
			# endpoint return None for every registration, success or refusal.
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

		# Looked up before the call so the exchange can be traced onto the member
		# it is about; the status update below reuses it.
		cm_name = frappe.db.get_value("Cova Members", {"employee": employee}, "name")

		result = cova_post(
			base_url + deactivation_endpoint,
			headers,
			payload,
			trace=("Cova Members", cm_name, "deactivate_member") if cm_name else None,
		)

		# Update Cova Member to Inactive
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
		duplicates = 0
		register_failed = 0
		deactivated = 0
		deactivate_failed = 0
		# Why each row failed, so a sweep of 4000 employees is diagnosable
		# without going through the Error Log one entry at a time.
		failures = []

		to_register = frappe.db.get_all(
			"Employee",
			filters={"status": "Active", "cova_member_id": ["in", ["", None]]},
			# Only the name is used — every row is re-read with frappe.get_doc below.
			# Naming any other column here is a portability trap: `national_id` is
			# owned by csf_ke, so on a site without it this query dies with
			# "Unknown column 'national_id'" and sync_members never runs.
			pluck="name",
		)

		for name in to_register:
			emp = frappe.get_doc("Employee", name)
			try:
				result = register_one(base_url, register_endpoint, headers, emp)
				if result.get("error"):
					register_failed = register_failed + 1
					failures.append({"employee": name, "action": "register", "reason": result["error"]})
				elif result.get("status") == "duplicate":
					# COVA answers an already-enrolled member with a 200 and
					# status=duplicate. Counting it as a fresh registration
					# overstates what the sweep actually did.
					duplicates = duplicates + 1
				else:
					registered = registered + 1
			except Exception as exc:
				register_failed = register_failed + 1
				failures.append({"employee": name, "action": "register", "reason": str(exc)})
				frappe.log_error(title="COVA sync_members register", message=frappe.get_traceback())

		to_deactivate = frappe.db.get_all(
			"Employee",
			filters={
				"status": ["!=", "Active"],
				"cova_member_id": ["not in", ["", None]],
				"cova_deactivated": 0,
			},
			pluck="name",
		)

		for name in to_deactivate:
			emp = frappe.get_doc("Employee", name)
			try:
				result = deactivate_one(base_url, deactivation_endpoint, headers, emp)
				if result.get("error"):
					deactivate_failed = deactivate_failed + 1
					failures.append({"employee": name, "action": "deactivate", "reason": result["error"]})
				else:
					deactivated = deactivated + 1
			except Exception as exc:
				deactivate_failed = deactivate_failed + 1
				failures.append({"employee": name, "action": "deactivate", "reason": str(exc)})
				frappe.log_error(title="COVA sync_members deactivate", message=frappe.get_traceback())

		resp = {
			"status": "sync complete",
			"registered": registered,
			"duplicates": duplicates,
			"register_failed": register_failed,
			"deactivated": deactivated,
			"deactivate_failed": deactivate_failed,
			# Capped: a sweep where everything fails must not return 4000 rows.
			"failures": failures[:50],
			"failure_count": len(failures),
		}

	# ─── 4. SUBMIT TEST REQUEST ───────────────────────────────────────
	elif action == "submit_test_request":
		request_name = data.get("request_name")
		doc = frappe.get_doc("Clinic Test Request", request_name) if request_name else None

		if doc is None:
			# The desk buttons and the bulk re-send read `error` off the reply;
			# a raised DoesNotExistError would reach them as an HTML 404 page.
			return _test_request_error(data, "request_name is required")

		result = send_test_request(doc)
		if result.get("refused"):
			return _test_request_error(data, result["error"])

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

		national_id = offer.get("national_id") or ""
		full_name = offer.get("applicant_name") or ""
		date_of_birth = str(offer.get("date_of_birth")) if offer.get("date_of_birth") else ""
		gender = offer.get("gender") or ""
		# Falls back to the linked applicant for offers predating phone_number.
		phone = offer.get("phone_number") or ""
		if not phone and offer.get("job_applicant"):
			phone = frappe.db.get_value("Job Applicant", offer.job_applicant, "phone_number") or ""

		# Normalize phone to E.164 with Kenya country code (Phone field requires a country code)
		phone = normalize_phone(phone)

		if offer.get("cova_tested"):
			resp = {"error": "Candidate already tested. Pre-employment is locked to one test."}
		elif not national_id:
			resp = {"error": "National ID is required for pre-employment registration."}
		else:
			# Step 1: create the tracking Clinic Test Request FIRST (persists even if Cova calls fail)
			req_doc = frappe.new_doc("Clinic Test Request")
			req_doc.member_type = "Pre Employment"
			req_doc.nationa_id = national_id
			req_doc.full_name = full_name
			req_doc.date_of_birth = offer.get("date_of_birth")
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
			register_result = cova_post(base_url + register_endpoint, headers, register_payload)

			# Step 3: submit the single PreEmploymentWellness test (same test endpoint)
			test_payload = {
				"requestId": req_doc.name,
				"memberIdentifier": national_id,
				"memberType": "PreEmployment",
				"testPackage": cova_package_code("Pre Employment Wellness"),
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
			test_result = cova_post(
				base_url + test_request_endpoint,
				headers,
				test_payload,
				trace=("Clinic Test Request", req_doc.name, "submit_test_request"),
			)
			mark_test_request_sent(req_doc.name, test_result)

			# Step 4: create Cova Member and store COVA's registration response on it.
			#
			# COVA answers a partly-completed registration with an HTTP 400 whose
			# body still carries a covaMemberId — "member created but wallet
			# provisioning failed". The member exists at that point, so treat it
			# as registered: marking it Inactive would hide a real member, and the
			# next sweep would try to enrol them all over again.
			member_id = cova_member_id_of(register_result)
			registered = bool(member_id) or not register_result.get("error")
			cm_name = create_or_update_cova_member(
				"Pre Employment",
				"",
				national_id,
				full_name,
				"",
				phone,
				gender,
				activate=registered,
			)
			save_cova_response(cm_name, register_result)

			# Step 5: mark offer registered
			frappe.db.set_value("Job Offer", offer_name, "cova_registered", 1)

			resp = {
				"status": "success",
				"registration": register_result,
				"test_request": req_doc.name,
				"test_submission": test_result,
				"cova_member_id": member_id,
				"registered": registered,
			}
			# Registered, but something downstream of the member did not complete.
			# Worth reporting without calling the whole row a failure.
			if registered and register_result.get("error"):
				resp["warning"] = register_result["error"]

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

			total = 0
			for item in line_items:
				doc.append(
					"visit_line_item",
					{
						"purpose": item.get("type"),
						"cost": item.get("amount") or 0,
						"notes": item.get("notes") or "",
					},
				)
				total = total + (item.get("amount") or 0)

			doc.total_cost = total
			doc.insert(ignore_permissions=True)
			record_cova_message("Clinic Visit Cost", doc.name, "receive_visit", "received", data)

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
				"Clinic Visit Cost",
				{"payroll_number": req.payroll_number},
				"name",
				order_by="visit_date desc",
			)

			result_doc = frappe.new_doc("Clinic Test Result")
			result_doc.member_type = req.member_type
			result_doc.employee = req.employee
			result_doc.payroll_number = req.payroll_number
			result_doc.clinic_visit_reference = latest_visit
			result_doc.request_id = request_id
			result_doc.test_package = test_package
			result_doc.clinical_outcome = clinical_outcome
			result_doc.append("results", {"test": mc_name, "select_tezd": risk})
			result_doc.insert(ignore_permissions=True)
			record_cova_message(
				"Clinic Test Result", result_doc.name, "receive_test_result", "received", data
			)
			# Also on the request: it is the record someone opens to ask whether
			# the test ever came back, and the result is that answer.
			record_cova_message("Clinic Test Request", request_id, "receive_test_result", "received", data)

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
				offer_name = frappe.db.get_value("Job Offer", {"national_id": req.nationa_id}, "name")
				if offer_name:
					frappe.db.set_value("Job Offer", offer_name, "cova_tested", 1)
					frappe.db.set_value("Job Offer", offer_name, "linked_test_result", result_doc.name)

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
		# Sibling webhooks use camelCase (visitDateTime, clinicalOutcome), so
		# accept postingDate alongside the original date key rather than
		# silently filing every report with no posting date.
		posting_date = data.get("postingDate") or data.get("date")
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

				skipped = []
				for case in cases:
					# receive_test_result names the same thing medicalCase, so take
					# either spelling.
					condition = (case.get("condition") or case.get("medicalCase") or "").strip()
					count = case.get("count") or 0

					# A blank condition used to reach Medical Case as an empty
					# name, which threw "Case is required" and lost the entire
					# report — every other entry in it included. Skip the row and
					# report it instead.
					if not condition:
						skipped.append(case)
						continue

					if not frappe.db.exists("Medical Case", {"cases": condition}):
						mc = frappe.new_doc("Medical Case")
						mc.cases = condition
						mc.insert(ignore_permissions=True)

					mc_name = frappe.db.get_value("Medical Case", {"cases": condition}, "name")
					doc.append("medical_cases", {"medical_case": mc_name, "case_count": count})
					total = total + count

				if not doc.medical_cases:
					resp = {"error": "no case in the payload named a condition"}
				else:
					doc.total_cases = total
					doc.insert(ignore_permissions=True)
					record_cova_message(
						"Health Monthly Report", doc.name, "receive_health_report", "received", data
					)
					resp = {"status": "success", "name": doc.name, "total_cases": total}
					if skipped:
						resp["skipped"] = skipped

	# ─── 8. RUN STATUTORY TESTS ───────────────────────────────────────
	elif action == "run_statutory_tests":
		# Who is due comes from Cova Clinic Settings → Test Scheduling; the lists
		# that used to live here are only the fallback for a site that has not
		# set any groups up (and are what migrate seeds that table with).
		groups = get_test_groups()
		if not groups:
			groups = {
				name: {
					"test_package": spec["test_package"],
					"departments": [],
					"designations": spec["designations"],
					"employees_per_designation": 0,
				}
				for name, spec in DEFAULT_TEST_GROUPS.items()
			}

		COMPANY = get_clinic_company() or "Karen Roses"
		requested_count = int(data.get("pick_count") or 0)

		base_url = frappe.db.get_single_value("Cova Clinic Settings", "base_url")
		test_request_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "test_request_endpoint")
		headers = build_headers()
		current_year = frappe.utils.nowdate().split("-")[0]
		today = frappe.utils.nowdate()
		window_end = str(frappe.utils.add_days(today, 15))
		year_start = current_year + "-01-01"

		results = []

		for group_name, group in groups.items():
			package = group["test_package"]
			departments = group["departments"]
			# A group keyed on departments alone still runs, once, over all of them.
			desig_list = group["designations"] or [None]
			PICK_COUNT = requested_count or group["employees_per_designation"] or 10

			# Pick PICK_COUNT random employees PER DESIGNATION (not per package)
			for desig in desig_list:
				query = (
					"SELECT e.name, e.employee_number, e.employee_name, e.designation, e.department "
					"FROM `tabEmployee` e "
					"WHERE e.company = %(company)s "
					"AND e.status = 'Active' "
					+ ("AND e.designation = %(designation)s " if desig else "")
					+ ("AND e.department IN %(departments)s " if departments else "")
					+ "AND e.name NOT IN ("
					"  SELECT tr.employee FROM `tabClinic Test Request` tr "
					"  WHERE tr.test_package = %(package)s "
					"  AND tr.scheduled_from >= %(year_start)s "
					"  AND tr.employee IS NOT NULL "
					"  AND tr.employee != ''"
					") "
					"ORDER BY RAND() "
					"LIMIT %(limit)s"
				)
				params = {
					"company": COMPANY,
					"designation": desig,
					"departments": tuple(departments),
					"package": package,
					"year_start": year_start,
					"limit": PICK_COUNT,
				}
				candidates = frappe.db.sql(query, params, as_dict=True)

				created = 0
				sent = 0
				failed = 0
				send_failures = []

				for emp in candidates:
					req_doc = frappe.new_doc("Clinic Test Request")
					req_doc.member_type = "Active"
					req_doc.employee = emp.get("name")
					req_doc.payroll_number = employee_payroll_id(emp)
					req_doc.department = emp.get("department")
					req_doc.designation = emp.get("designation")
					req_doc.test_group = group_name
					req_doc.status = "Pending"
					req_doc.test_package = package
					req_doc.scheduled_from = today
					req_doc.scheduled_to = window_end
					req_doc.notes = "Statutory annual medical tests"
					req_doc.insert(ignore_permissions=True)
					created = created + 1

					if base_url and test_request_endpoint:
						# send_test_request never raises, so the outcome has to be
						# read off the result rather than inferred from "no exception".
						send_result = send_test_request(req_doc, action="run_statutory_tests")
						if send_result.get("error"):
							failed = failed + 1
							send_failures.append(
								{
									"employee": emp.get("name"),
									"request": req_doc.name,
									"reason": send_result["error"],
								}
							)
						else:
							sent = sent + 1

				results.append(
					{
						"group": group_name,
						"package": package,
						"designation": desig,
						"departments": departments,
						"eligible": len(candidates),
						"created": created,
						"sent": sent,
						"failed": failed,
						"failures": send_failures[:20],
						"employees": [
							{"name": e.get("name"), "employee_name": e.get("employee_name")}
							for e in candidates
						],
					}
				)

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


def _disease_where(data):
	"""WHERE clause + params for the Health Monthly Report (hmr) / Health Report
	(hr) join, shared by the report and its drill-down lists so a list can never
	disagree with the tile that opened it."""
	filters = {}
	clauses = []

	year = data.get("year")
	f_month = (data.get("month") or "").upper()
	f_posting_date = data.get("posting_date")
	f_medical_case = data.get("medical_case")

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

	# A set of months (the trend dialog's toggles); a single ``month`` above
	# still works for every other caller.
	f_months = data.get("months")
	if isinstance(f_months, str):
		f_months = [m for m in f_months.split(",") if m.strip()]
	if f_months:
		wanted = [str(m).upper() for m in f_months if str(m).upper() in MONTH_LABELS]
		if wanted:
			clauses.append("UPPER(hmr.month) IN %(months)s")
			filters["months"] = tuple(wanted)

	range_clauses, range_params = _range_clauses("hmr.posting_date", data)
	clauses.extend(range_clauses)
	filters.update(range_params)

	where_clause = ""
	if clauses:
		where_clause = " WHERE " + " AND ".join(clauses)
	return where_clause, filters


_DISEASE_FROM = "FROM `tabHealth Monthly Report` hmr INNER JOIN `tabHealth Report` hr ON hr.parent = hmr.name"


@frappe.whitelist()
def disease_report_detail():
	"""The records behind one Disease & Health KPI tile.

	``kind`` picks the tile: ``total`` lists every Health Report line (one
	condition in one monthly report), ``conditions`` one row per condition,
	``average`` and ``months`` one row per monthly report. Filters are applied
	exactly as the tiles compute them."""
	assert_health_report_access("health")
	data = frappe.request.get_json() or {}
	kind = (data.get("kind") or "total").strip()
	where, params = _disease_where(data)
	month_sort = (
		"FIELD(UPPER(hmr.month),'JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC')"
	)

	if kind == "conditions_by_month":
		# One row per condition per month, for a line per month in the trend.
		rows = frappe.db.sql(
			"SELECT COALESCE(NULLIF(hr.medical_case, ''), 'Unspecified') AS medical_case, "
			"UPPER(hmr.month) AS month, SUM(hr.case_count) AS total "
			+ _DISEASE_FROM
			+ where
			+ " GROUP BY hr.medical_case, UPPER(hmr.month) ORDER BY total DESC LIMIT 2000",
			params,
			as_dict=True,
		)
		for r in rows:
			r["total"] = int(r.get("total") or 0)
	elif kind == "conditions":
		rows = frappe.db.sql(
			"SELECT COALESCE(NULLIF(hr.medical_case, ''), 'Unspecified') AS medical_case, "
			"SUM(hr.case_count) AS total, COUNT(DISTINCT hmr.name) AS reports "
			+ _DISEASE_FROM
			+ where
			+ " GROUP BY hr.medical_case ORDER BY total DESC, medical_case ASC LIMIT 500",
			params,
			as_dict=True,
		)
		for r in rows:
			r["total"] = int(r.get("total") or 0)
			r["reports"] = int(r.get("reports") or 0)
			if r["medical_case"] != "Unspecified" and frappe.db.exists("Medical Case", r["medical_case"]):
				r["route"] = _desk_form_route("Medical Case", r["medical_case"])
	elif kind in ("average", "months"):
		rows = frappe.db.sql(
			"SELECT hmr.name, UPPER(hmr.month) AS month, hmr.posting_date, "
			"SUM(hr.case_count) AS total, COUNT(DISTINCT hr.medical_case) AS conditions "
			+ _DISEASE_FROM
			+ where
			+ " GROUP BY hmr.name ORDER BY hmr.posting_date DESC, "
			+ month_sort
			+ " DESC LIMIT 500",
			params,
			as_dict=True,
		)
		for r in rows:
			r["total"] = int(r.get("total") or 0)
			r["conditions"] = int(r.get("conditions") or 0)
			r["posting_date"] = str(r["posting_date"]) if r.get("posting_date") else ""
			r["route"] = _desk_form_route("Health Monthly Report", r["name"])
	else:
		rows = frappe.db.sql(
			"SELECT hmr.name, UPPER(hmr.month) AS month, hmr.posting_date, "
			"COALESCE(NULLIF(hr.medical_case, ''), 'Unspecified') AS medical_case, hr.case_count AS total "
			+ _DISEASE_FROM
			+ where
			+ " ORDER BY hmr.posting_date DESC, "
			+ month_sort
			+ " DESC, hr.case_count DESC LIMIT 500",
			params,
			as_dict=True,
		)
		# How many distinct report months each condition appears in, within
		# the same filters, so a line can say whether the case keeps recurring.
		recurrence = {
			r["medical_case"]: int(r["months"] or 0)
			for r in frappe.db.sql(
				"SELECT COALESCE(NULLIF(hr.medical_case, ''), 'Unspecified') AS medical_case, "
				"COUNT(DISTINCT CONCAT(YEAR(hmr.posting_date), '-', UPPER(hmr.month))) AS months "
				+ _DISEASE_FROM
				+ where
				+ " GROUP BY hr.medical_case",
				params,
				as_dict=True,
			)
		}
		for r in rows:
			r["total"] = int(r.get("total") or 0)
			r["posting_date"] = str(r["posting_date"]) if r.get("posting_date") else ""
			r["months_seen"] = recurrence.get(r["medical_case"], 0)
			r["route"] = _desk_form_route("Health Monthly Report", r["name"])

	out = {"kind": kind, "rows": rows, "total": sum(r["total"] for r in rows)}
	frappe.response["message"] = out
	return out


@frappe.whitelist()
def clinic_disease_report():
	"""Pivot the Health Monthly Report / Health Report data into a condition x
	month matrix plus a multi-year monthly trend and filter option lists.

	HR-only — the /health-report portal page gates on the same check, and this
	guard stops the endpoint being read directly by non-HR sessions."""
	assert_health_report_access("health")

	data = frappe.request.get_json() or {}

	month_order = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

	where_clause, filters = _disease_where(data)

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
		"SELECT DISTINCT YEAR(posting_date) AS yr FROM `tabHealth Monthly Report` ORDER BY yr DESC",
		as_dict=True,
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
		"SELECT DISTINCT posting_date FROM `tabHealth Monthly Report` ORDER BY posting_date DESC",
		as_dict=True,
	)
	all_dates = [str(r.get("posting_date")) for r in all_dates_rows if r.get("posting_date")]

	all_cases_rows = frappe.db.sql(
		"SELECT DISTINCT medical_case FROM `tabHealth Report` ORDER BY medical_case ASC", as_dict=True
	)
	all_cases = [r.get("medical_case") for r in all_cases_rows if r.get("medical_case")]

	# Per-month series for the tile sparklines: the year alone, month and
	# range filters dropped, every other filter kept.
	yr_where, yr_params = _disease_where({"year": data.get("year"), "medical_case": data.get("medical_case")})
	series_rows = frappe.db.sql(
		"SELECT UPPER(hmr.month) AS m, SUM(hr.case_count) AS total, COUNT(DISTINCT hr.medical_case) AS conditions "
		+ _DISEASE_FROM
		+ yr_where
		+ " GROUP BY UPPER(hmr.month)",
		yr_params,
		as_dict=True,
	)
	kpi_series = _kpi_series(series_rows, ["total", "conditions"])
	reported, running = [], 0
	for v in kpi_series["total"]:
		running += 1 if v else 0
		reported.append(running)
	kpi_series["months_reported"] = reported

	resp = {
		"months": months_present,
		"rows": table_sorted,
		"kpi_series": kpi_series,
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


def clinic_currency():
	"""The currency clinic costs are denominated in — the default currency of
	the Company the integration is configured for, falling back to the site
	default. Returned with the money-bearing dashboards so the page never has
	to assume one."""
	from cova_clinic_integration.cova_clinic_integration.doctype.cova_clinic_settings.cova_clinic_settings import (
		get_clinic_company,
	)

	company = get_clinic_company()
	if company:
		currency = frappe.get_cached_value("Company", company, "default_currency")
		if currency:
			return currency
	return frappe.db.get_default("currency") or ""


# A Clinic Visit Cost is "checked in" when a Clinic Checkin exists for the same
# employee on the same day. The doctype carries two unrelated payloads and
# either one counts: a door punch (time) or an API-tab record (time_in).
# Expects the visit table aliased as cv.
_NO_CHECKIN_CLAUSE = (
	"NOT EXISTS (SELECT 1 FROM `tabClinic Checkin` cc WHERE cc.employee = cv.employee "
	"AND (DATE(cc.time) = cv.visit_date OR DATE(cc.time_in) = cv.visit_date))"
)


# What makes a row a door punch rather than a sick note.
#
# The two payloads used to be told apart by which employee column was filled.
# That put the distinction in the wrong place: it is a property of the payload,
# not of who it belongs to, and a row loaded into the wrong column silently
# changed meaning. A punch is a row with a punch time; a sick note carries a
# start and end date instead.
_PUNCH_CLAUSE = "cc.time IS NOT NULL AND cc.employee IS NOT NULL AND cc.employee != ''"


def _desk_form_route(doctype, name):
	"""Site-relative desk path for a document. Derived from the framework's own
	helper rather than written out, because the desk prefix is /app before v17
	and /desk from v17 on — this app runs against both."""
	from urllib.parse import urlsplit

	return urlsplit(frappe.utils.get_url_to_form(doctype, name)).path


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
	registered = [
		"EXISTS (SELECT 1 FROM `tabCova Members` cm WHERE cm.employee = e.name AND cm.status = 'Active')"
	]
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


def _year_clauses(clauses):
	"""The same filter set with the month and from/to restrictions removed, so a
	per-month series can run over the whole year the tiles sit in."""
	return [c for c in clauses if "MONTH(" not in c and "from_date)s" not in c and "to_date)s" not in c]


def _kpi_series(rows, keys, month_key="m"):
	"""GROUP BY month rows -> {key: [12 values]} for the tile sparklines.
	``month_key`` is a 1-12 number or a MONTH_LABELS name."""
	out = {k: [0] * 12 for k in keys}
	for r in rows:
		m = r.get(month_key)
		if isinstance(m, str):
			m = MONTH_LABELS.index(m.upper()) + 1 if m.upper() in MONTH_LABELS else None
		if not m or not 1 <= int(m) <= 12:
			continue
		for k in keys:
			v = r.get(k)
			out[k][int(m) - 1] = round(float(v), 2) if v is not None else 0
	return out


def _ratio_series(num, den, digits=1):
	return [round(n / d, digits) if d else 0 for n, d in zip(num, den, strict=False)]


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

	* **biometric** punches (``log_type`` / ``time``) — the turnstile-style log
	  used to count how many employees visited the clinic;
	* **sick-off** records (``start_date`` + ``end_date``) — the ones whose
	  controller raises a Sick Leave Application.

	Both name their person in ``employee``; what tells them apart is the payload
	each carries, not which column the employee was written into.

	Both are summarised here so the dashboard can present them separately."""
	assert_health_report_access("biometric", "sickoff")
	data, year, month_num = _dashboard_request()

	# ── biometric punches ────────────────────────────────────────────
	# The visits view filters by an explicit date range (date pickers); the
	# sick-off view still uses year/month. Both sets are honoured here.
	bio_clauses, bio_params = _period_clauses("cc.time", year, month_num)
	range_clauses, range_params = _range_clauses("cc.time", data)
	bio_clauses.extend(range_clauses)
	bio_params.update(range_params)
	bio_clauses.insert(0, _PUNCH_CLAUSE)
	bio_where = _where(bio_clauses)

	bio_totals = frappe.db.sql(
		"SELECT COUNT(*) AS punches, COUNT(DISTINCT cc.employee) AS employees, "
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
		"SELECT cc.employee AS employee, COALESCE(e.employee_name, cc.employee) AS employee_name, "
		"MAX(cc.employee_payroll_number) AS payroll_number, COUNT(*) AS visits, "
		"SUM(CASE WHEN cc.log_type = 'OUT' THEN 0 ELSE 1 END) AS in_punches, "
		"SUM(CASE WHEN cc.log_type = 'OUT' THEN 1 ELSE 0 END) AS out_punches, "
		"MIN(CASE WHEN cc.log_type = 'OUT' THEN NULL ELSE cc.time END) AS first_in, "
		"MAX(CASE WHEN cc.log_type = 'OUT' THEN NULL ELSE cc.time END) AS last_in, "
		"MIN(CASE WHEN cc.log_type = 'OUT' THEN cc.time ELSE NULL END) AS first_out, "
		"MAX(CASE WHEN cc.log_type = 'OUT' THEN cc.time ELSE NULL END) AS last_out, "
		"MAX(cc.time) AS last_visit "
		"FROM `tabClinic Checkin` cc LEFT JOIN `tabEmployee` e ON e.name = cc.employee"
		+ bio_where
		+ " GROUP BY cc.employee, e.employee_name"
		" ORDER BY visits DESC LIMIT 15",
		bio_params,
		as_dict=True,
	)
	# Time in the clinic: each employee-day runs from its first punch to its
	# last (see _describe_stay); the period's figure is the sum of those days.
	stays = _clinic_stays(bio_where, bio_params)
	timed = [st["minutes"] for st in stays if st["minutes"] is not None]
	minutes_by_employee = {}
	for st in stays:
		if st["minutes"] is not None:
			minutes_by_employee[st["employee"]] = minutes_by_employee.get(st["employee"], 0) + st["minutes"]

	for r in bio_top:
		for k in ("visits", "in_punches", "out_punches"):
			r[k] = int(r.get(k) or 0)
		for k in ("last_visit", "first_in", "last_in", "first_out", "last_out"):
			r[k] = str(r[k])[:16] if r.get(k) else ""
		r["minutes"] = minutes_by_employee.get(r["employee"])

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

	bio_year = _where(_year_clauses(bio_clauses))
	bio_series_rows = frappe.db.sql(
		"SELECT MONTH(cc.time) AS m, COUNT(*) AS punches, COUNT(DISTINCT cc.employee) AS employees, "
		"SUM(CASE WHEN cc.log_type = 'IN' THEN 1 ELSE 0 END) AS in_punches, "
		"SUM(CASE WHEN cc.log_type = 'OUT' THEN 1 ELSE 0 END) AS out_punches, "
		"COUNT(DISTINCT DATE(cc.time)) AS days "
		"FROM `tabClinic Checkin` cc" + bio_year + " GROUP BY MONTH(cc.time)",
		bio_params,
		as_dict=True,
	)
	bio_series = _kpi_series(bio_series_rows, ["punches", "employees", "in_punches", "out_punches", "days"])
	bio_series["avg_per_day"] = _ratio_series(bio_series["punches"], bio_series["days"])

	so_year = _where(_year_clauses(so_clauses))
	so_series_rows = frappe.db.sql(
		"SELECT MONTH(cc.start_date) AS m, COUNT(*) AS records, COUNT(DISTINCT cc.employee) AS employees, "
		"SUM(DATEDIFF(cc.end_date, cc.start_date) + 1) AS days, "
		"SUM(CASE WHEN cc.leave_application IS NOT NULL AND cc.leave_application != '' THEN 1 ELSE 0 END) AS with_leave "
		"FROM `tabClinic Checkin` cc" + so_year + " GROUP BY MONTH(cc.start_date)",
		so_params,
		as_dict=True,
	)
	so_series = _kpi_series(so_series_rows, ["records", "employees", "days", "with_leave"])
	so_series["without_leave"] = [r - w for r, w in zip(so_series["records"], so_series["with_leave"], strict=False)]
	so_series["avg_days"] = _ratio_series(so_series["days"], so_series["records"])

	# ── seen at an external facility (Clinic Checkin.is_external) ─────
	ext_day = "COALESCE(cc.start_date, DATE(cc.time_in), DATE(cc.creation))"
	ext_clauses, ext_params = _period_clauses(ext_day, year, month_num)
	ext_range, ext_range_params = _range_clauses(ext_day, data, prefix="ext_")
	ext_clauses.extend(ext_range)
	ext_params.update(ext_range_params)
	ext_clauses.insert(0, "cc.is_external = 1")
	facilities = frappe.db.sql(
		"SELECT cc.facility, COUNT(*) AS visits, COUNT(DISTINCT cc.employee) AS employees, "
		"SUM(COALESCE(cc.sick_off_given, 0)) AS sick_days "
		"FROM `tabClinic Checkin` cc"
		+ _where(ext_clauses)
		+ " GROUP BY cc.facility ORDER BY visits DESC LIMIT 50",
		ext_params,
		as_dict=True,
	)
	for r in facilities:
		for k in ("visits", "employees", "sick_days"):
			r[k] = int(r.get(k) or 0)

	resp = {
		"biometric": {
			"kpi_series": bio_series,
			"kpis": {
				"punches": bio_punches,
				"employees": int(bio_totals.get("employees") or 0),
				"in_punches": int(bio_totals.get("in_punches") or 0),
				"out_punches": int(bio_totals.get("out_punches") or 0),
				"days": bio_days,
				"avg_per_day": round((bio_punches * 1.0) / bio_days, 1) if bio_days else 0,
				"stays": len(stays),
				"timed_stays": len(timed),
				"total_minutes": sum(timed),
				"avg_stay_minutes": round(sum(timed) / len(timed)) if timed else 0,
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
			"kpi_series": so_series,
			"kpis": {
				"records": so_records,
				"employees": int(so_totals.get("employees") or 0),
				"days": so_days,
				"with_leave": so_with_leave,
				"without_leave": so_records - so_with_leave,
				"leave_rate": _pct(so_with_leave, so_records),
				"avg_days": round((so_days * 1.0) / so_records, 1) if so_records else 0,
				"external_visits": sum(r["visits"] for r in facilities),
				"external_sick_days": sum(r["sick_days"] for r in facilities),
			},
			"by_month": _month_series(so_by_month),
			"durations": durations,
			"top_employees": so_top,
			"facilities": facilities,
		},
		"filter_options": {
			"years": [str(r.get("yr")) for r in year_rows if r.get("yr")],
			"months": list(MONTH_LABELS),
		},
	}
	# One endpoint feeds two sections; send only what this viewer is ticked for.
	allowed = allowed_dashboard_sections()
	if "biometric" not in allowed:
		resp.pop("biometric")
	if "sickoff" not in allowed:
		resp.pop("sick_off")
	frappe.response["message"] = resp
	return resp


def _describe_stay(stay):
	"""Turn one employee-day's punch extremes into a clinic stay.

	The employee arrived at the day's earliest punch and left at its latest,
	whichever log type each happened to be — so a stay is one of First In →
	Last Out, First In → Last In, First Out → Last Out or First Out → Last In.
	On a tie, an IN is taken as the arrival and an OUT as the departure. A day
	with a single punch has an arrival but no departure, so no time spent."""
	first_in, last_in = stay.get("first_in"), stay.get("last_in")
	first_out, last_out = stay.get("first_out"), stay.get("last_out")

	if first_in and (not first_out or first_in <= first_out):
		arrived, arrived_as = first_in, "First In"
	else:
		arrived, arrived_as = first_out, "First Out"
	if last_out and (not last_in or last_out >= last_in):
		left, left_as = last_out, "Last Out"
	else:
		left, left_as = last_in, "Last In"

	stay["arrived"], stay["arrived_as"] = arrived, arrived_as
	if int(stay.get("punches") or 0) < 2 or not (arrived and left) or left <= arrived:
		stay["left"], stay["left_as"], stay["minutes"] = None, "", None
		stay["interval"] = arrived_as + " only"
	else:
		stay["left"], stay["left_as"] = left, left_as
		stay["minutes"] = int((left - arrived).total_seconds() // 60)
		stay["interval"] = arrived_as + " \u2192 " + left_as
	return stay


_STAY_NAME = "COALESCE(NULLIF(e.employee_name, ''), NULLIF(cc.full_name, ''), cc.employee)"
_STAY_IDENT = "COALESCE(NULLIF(cc.employee_payroll_number, ''), NULLIF(cc.payroll_number, ''))"


def _clinic_stays(where, params, limit=None):
	"""One row per employee per day in the clinic, with that day's first and
	last IN and OUT punch and the time spent between arrival and departure.
	Anything that is not an OUT counts as an IN, as the hour chart does."""
	rows = frappe.db.sql(
		f"SELECT cc.employee, MAX({_STAY_NAME}) AS employee_name, MAX({_STAY_IDENT}) AS payroll_number, "
		"DATE(cc.time) AS day, COUNT(*) AS punches, "
		"MIN(CASE WHEN cc.log_type = 'OUT' THEN NULL ELSE cc.time END) AS first_in, "
		"MAX(CASE WHEN cc.log_type = 'OUT' THEN NULL ELSE cc.time END) AS last_in, "
		"MIN(CASE WHEN cc.log_type = 'OUT' THEN cc.time ELSE NULL END) AS first_out, "
		"MAX(CASE WHEN cc.log_type = 'OUT' THEN cc.time ELSE NULL END) AS last_out "
		"FROM `tabClinic Checkin` cc LEFT JOIN `tabEmployee` e ON e.name = cc.employee"
		+ where
		+ " GROUP BY cc.employee, DATE(cc.time) ORDER BY day DESC, employee_name ASC"
		+ (f" LIMIT {int(limit)}" if limit else ""),
		params,
		as_dict=True,
	)
	return [_describe_stay(r) for r in rows]


def _pair_punches(punches):
	"""Fold IN/OUT punches into visits: one row per employee per stay.

	``punches`` must be ordered by employee then time. An IN opens a visit; the
	next OUT for the same employee on the same day closes it. An OUT with no
	open IN, or an IN never followed by an OUT, is still a visit — with one side
	blank — so no punch disappears from the list."""
	visits = []
	open_visit = None
	for p in punches:
		t = p.get("time")
		day = str(t)[:10] if t else ""
		if open_visit is not None and (open_visit["employee"] != p["employee"] or open_visit["day"] != day):
			open_visit = None
		if (p.get("log_type") or "IN") != "OUT":
			open_visit = {
				"name": p["name"],
				"employee": p["employee"],
				"employee_name": p.get("employee_name"),
				"payroll_number": p.get("payroll_number"),
				"day": day,
				"time_in": t,
				"time_out": None,
				"out_name": None,
				"minutes": None,
			}
			visits.append(open_visit)
		elif open_visit is not None and open_visit["time_out"] is None:
			open_visit["time_out"] = t
			open_visit["out_name"] = p["name"]
			if open_visit["time_in"]:
				open_visit["minutes"] = int((t - open_visit["time_in"]).total_seconds() // 60)
			open_visit = None
		else:
			visits.append(
				{
					"name": p["name"],
					"employee": p["employee"],
					"employee_name": p.get("employee_name"),
					"payroll_number": p.get("payroll_number"),
					"day": day,
					"time_in": None,
					"time_out": t,
					"out_name": p["name"],
					"minutes": None,
				}
			)
			open_visit = None
	return visits


@frappe.whitelist()
def clinic_punch_people():
	"""The punches behind one Clinic Visits (biometric) KPI tile.

	``kind`` picks the tile: ``IN`` / ``OUT`` for one log type, ``employees``
	for one row per person, ``days`` for one row per visit day, ``stays`` for
	one row per employee per day with its first/last IN and OUT and the time
	spent in the clinic, or nothing for every punch in the period. The period is applied exactly as the tiles
	compute it, so the list can never disagree with the number clicked."""
	assert_health_report_access("biometric")
	data, year, month_num = _dashboard_request()
	kind = (data.get("kind") or "").strip()

	clauses, params = _period_clauses("cc.time", year, month_num)
	range_clauses, range_params = _range_clauses("cc.time", data)
	clauses.extend(range_clauses)
	params.update(range_params)
	clauses.insert(0, _PUNCH_CLAUSE)
	if kind in ("IN", "OUT"):
		# The Checked In tile counts log_type = 'IN' only, but the hour chart
		# treats anything that is not OUT as an arrival; follow the tile here.
		clauses.append("cc.log_type = %(log_type)s")
		params["log_type"] = kind
	where = _where(clauses)

	name = "COALESCE(NULLIF(e.employee_name, ''), NULLIF(cc.full_name, ''), cc.employee)"
	ident = "COALESCE(NULLIF(cc.employee_payroll_number, ''), NULLIF(cc.payroll_number, ''))"
	joins = " LEFT JOIN `tabEmployee` e ON e.name = cc.employee"
	grouped = kind in ("employees", "days", "stays")

	if kind == "stays":
		rows = _clinic_stays(where, params, limit=500)
		for r in rows:
			for k in ("first_in", "last_in", "first_out", "last_out", "arrived", "left"):
				r[k] = str(r[k])[:16] if r.get(k) else ""
			r["punches"] = int(r.get("punches") or 0)
			r["day"] = str(r["day"])
		out = {"kind": kind, "rows": rows, "grouped": grouped}
		frappe.response["message"] = out
		return out

	if kind == "employees":
		rows = frappe.db.sql(
			f"SELECT cc.employee, MAX({name}) AS employee_name, MAX({ident}) AS payroll_number, "
			"COUNT(*) AS punches, "
			"SUM(CASE WHEN cc.log_type = 'IN' THEN 1 ELSE 0 END) AS in_punches, "
			"SUM(CASE WHEN cc.log_type = 'OUT' THEN 1 ELSE 0 END) AS out_punches, "
			"MIN(cc.time) AS first_seen, MAX(cc.time) AS last_seen "
			"FROM `tabClinic Checkin` cc"
			+ joins
			+ where
			+ " GROUP BY cc.employee ORDER BY punches DESC, employee_name ASC LIMIT 500",
			params,
			as_dict=True,
		)
	elif kind == "days":
		rows = frappe.db.sql(
			"SELECT DATE(cc.time) AS day, COUNT(*) AS punches, COUNT(DISTINCT cc.employee) AS employees, "
			"SUM(CASE WHEN cc.log_type = 'IN' THEN 1 ELSE 0 END) AS in_punches, "
			"SUM(CASE WHEN cc.log_type = 'OUT' THEN 1 ELSE 0 END) AS out_punches "
			"FROM `tabClinic Checkin` cc" + where + " GROUP BY DATE(cc.time) ORDER BY day DESC LIMIT 500",
			params,
			as_dict=True,
		)
	else:
		punches = frappe.db.sql(
			f"SELECT cc.name, cc.employee, {name} AS employee_name, {ident} AS payroll_number, "
			"cc.log_type, cc.time "
			"FROM `tabClinic Checkin` cc" + joins + where + " ORDER BY cc.employee, cc.time ASC LIMIT 2000",
			params,
			as_dict=True,
		)
		rows = _pair_punches(punches)
		# The Checked In tile is IN punches, the Checked Out tile OUT punches:
		# their lists are the visits carrying that side. Newest visit first.
		rows.sort(key=lambda v: v["time_in"] or v["time_out"] or "", reverse=True)
		rows = rows[:500]

	for r in rows:
		for k in ("punches", "in_punches", "out_punches", "employees"):
			if r.get(k) is not None:
				r[k] = int(r[k])
		for k in ("time", "first_seen", "last_seen", "time_in", "time_out"):
			if k in r:
				r[k] = str(r[k])[:16] if r.get(k) else ""
		if r.get("day") is not None:
			r["day"] = str(r["day"])
		if r.get("name"):
			r["route"] = _desk_form_route("Clinic Checkin", r["name"])

	out = {"kind": kind, "rows": rows, "grouped": grouped}
	frappe.response["message"] = out
	return out


@frappe.whitelist()
def clinic_visit_cost_report():
	"""Employee visits and what they cost, from Clinic Visit Cost and its
	Visit Line Item rows.

	``benefit_*`` on a visit is the balance COVA reported as *remaining* after
	that visit, so the trend follows the latest visit in each month rather than
	summing across visits — a sum of balances would be meaningless."""
	assert_health_report_access("visits")
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
		+ _where([*list(clauses), "cv.visit_date IS NOT NULL"])
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

	# Cost billed on a day the employee never checked in. Worth its own table:
	# either the checkin never reached us, or the charge is sitting against the
	# wrong person or the wrong date.
	no_checkin_costs = frappe.db.sql(
		"SELECT cv.employee AS employee, "
		"COALESCE(e.employee_name, cv.full_name, cv.candidate_name, cv.employee) AS employee_name, "
		"MAX(cv.payroll_number) AS payroll_number, COUNT(*) AS visits, "
		"COALESCE(SUM(cv.total_cost), 0) AS cost, MAX(cv.visit_date) AS last_visit "
		"FROM `tabClinic Visit Cost` cv LEFT JOIN `tabEmployee` e ON e.name = cv.employee"
		+ _where([*list(clauses), "cv.visit_date IS NOT NULL", _NO_CHECKIN_CLAUSE])
		+ " GROUP BY cv.employee, e.employee_name, cv.full_name, cv.candidate_name"
		" ORDER BY cost DESC LIMIT 100",
		params,
		as_dict=True,
	)
	for r in no_checkin_costs:
		r["visits"] = int(r.get("visits") or 0)
		r["cost"] = round(float(r.get("cost") or 0), 2)
		r["last_visit"] = str(r["last_visit"]) if r.get("last_visit") else ""

	# Cost per employee per month — the spend grid plus the series behind it.
	matrix_rows = frappe.db.sql(
		"SELECT cv.employee AS employee, "
		"COALESCE(e.employee_name, cv.full_name, cv.candidate_name, cv.employee) AS employee_name, "
		"MONTH(cv.visit_date) AS m, COALESCE(SUM(cv.total_cost), 0) AS cost "
		"FROM `tabClinic Visit Cost` cv LEFT JOIN `tabEmployee` e ON e.name = cv.employee"
		+ _where([*list(clauses), "cv.visit_date IS NOT NULL"])
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

	vs_year = _where(_year_clauses(clauses))
	vs_series_rows = frappe.db.sql(
		"SELECT MONTH(cv.visit_date) AS m, COUNT(*) AS visits, COUNT(DISTINCT cv.employee) AS employees, "
		"COALESCE(SUM(cv.total_cost), 0) AS cost "
		"FROM `tabClinic Visit Cost` cv" + vs_year + " GROUP BY MONTH(cv.visit_date)",
		params,
		as_dict=True,
	)
	vs_series = _kpi_series(vs_series_rows, ["visits", "employees", "cost"])
	vs_series["avg_cost"] = _ratio_series(vs_series["cost"], vs_series["visits"], 0)
	vs_series["cost_per_employee"] = _ratio_series(vs_series["cost"], vs_series["employees"], 0)

	resp = {
		"currency": clinic_currency(),
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
		"kpi_series": vs_series,
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
		"no_checkin_costs": no_checkin_costs,
		"filter_options": {
			"years": [str(r.get("yr")) for r in opt_years if r.get("yr")],
			"months": list(MONTH_LABELS),
			"employees": registered_employee_options(),
		},
	}
	frappe.response["message"] = resp
	return resp


@frappe.whitelist()
def employee_visit_breakdown():
	"""Individual visit records for an employee, from Clinic Visit Cost.
	Used by the modal breakdown view on the health report dashboard."""
	assert_health_report_access("visits")
	employee = frappe.form_dict.get("employee")
	if not employee:
		frappe.throw("employee is required")

	data = frappe.request.get_json() or {}
	clauses = ["cv.employee = %(employee)s"]
	params = {"employee": employee}

	# The grid this modal opens from is year-scoped, so the breakdown is too.
	if data.get("year"):
		clauses.append("YEAR(cv.visit_date) = %(year)s")
		params["year"] = data["year"]

	# Opened from the no-checkin table: show only the visits that table counted.
	if frappe.utils.cint(data.get("no_checkin")):
		clauses.append("cv.visit_date IS NOT NULL")
		clauses.append(_NO_CHECKIN_CLAUSE)

	# Include date range filters if provided
	if data.get("from_date"):
		try:
			from_date = frappe.utils.getdate(data.get("from_date"))
			clauses.append("DATE(cv.visit_date) >= %(from_date)s")
			params["from_date"] = str(from_date)
		except Exception:
			pass
	if data.get("to_date"):
		try:
			to_date = frappe.utils.getdate(data.get("to_date"))
			clauses.append("DATE(cv.visit_date) <= %(to_date)s")
			params["to_date"] = str(to_date)
		except Exception:
			pass

	where = _where(clauses)

	# Fetch individual visit records with their line items. Clinic Visit Cost
	# carries full_name / candidate_name, never employee_name — the readable
	# name comes off Employee when the link resolves.
	visits = frappe.db.sql(
		"SELECT cv.name, cv.employee, "
		"COALESCE(e.employee_name, cv.full_name, cv.candidate_name, cv.employee) AS employee_name, "
		"cv.visit_date, cv.total_cost, cv.payroll_number "
		"FROM `tabClinic Visit Cost` cv LEFT JOIN `tabEmployee` e ON e.name = cv.employee"
		+ where
		+ " ORDER BY cv.visit_date DESC",
		params,
		as_dict=True,
	)

	# The line items in one pass rather than one query per visit — the notes on
	# a row carry the drug or service name, which is what the panel is for.
	by_parent = {}
	if visits:
		for it in frappe.db.sql(
			"SELECT li.parent, li.purpose, li.cost, li.notes FROM `tabVisit Line Item` li "
			"WHERE li.parent IN %(parents)s AND li.parenttype = 'Clinic Visit Cost' "
			"ORDER BY li.parent, li.idx ASC",
			{"parents": [v["name"] for v in visits]},
			as_dict=True,
		):
			it["cost"] = round(float(it.get("cost") or 0), 2)
			by_parent.setdefault(it.pop("parent"), []).append(it)

	for visit in visits:
		visit["visit_date"] = str(visit["visit_date"]) if visit.get("visit_date") else ""
		visit["total_cost"] = round(float(visit.get("total_cost") or 0), 2)
		visit["items"] = by_parent.get(visit["name"], [])
		visit["route"] = _desk_form_route("Clinic Visit Cost", visit["name"])

	out = {"visits": visits, "currency": clinic_currency()}
	frappe.response["message"] = out
	return out


@frappe.whitelist()
def clinic_test_request_report():
	"""Clinic Test Request pipeline: what was asked of COVA and what came back.
	Scheduling dates drive the period, so a request shows up in the month it was
	scheduled for rather than the month the row happened to be created."""
	assert_health_report_access("requests")
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

	overdue_clauses = [*list(clauses), "tr.status = 'Pending'", "tr.scheduled_to < CURDATE()"]
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

	rq_year = _where(_year_clauses(clauses))
	rq_series_rows = frappe.db.sql(
		"SELECT MONTH(tr.scheduled_from) AS m, COUNT(*) AS total, "
		"SUM(CASE WHEN tr.status = 'Pending' THEN 1 ELSE 0 END) AS pending, "
		"SUM(CASE WHEN tr.status = 'Completed' THEN 1 ELSE 0 END) AS completed, "
		"SUM(CASE WHEN tr.status = 'Cancelled' THEN 1 ELSE 0 END) AS cancelled, "
		"SUM(CASE WHEN tr.status = 'Pending' AND tr.scheduled_to < CURDATE() THEN 1 ELSE 0 END) AS overdue "
		"FROM `tabClinic Test Request` tr" + rq_year + " GROUP BY MONTH(tr.scheduled_from)",
		params,
		as_dict=True,
	)
	rq_series = _kpi_series(rq_series_rows, ["total", "pending", "completed", "cancelled", "overdue"])

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
		"kpi_series": rq_series,
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
def purpose_spend_people():
	"""Who the spend on one purpose went to: one row per employee with their
	line items, visits and total cost for that purpose, under the same period
	and employee filters as the Spend by Purpose table."""
	assert_health_report_access("visits")
	data, year, month_num = _dashboard_request()

	clauses, params = _period_clauses("cv.visit_date", year, month_num)
	range_clauses, range_params = _range_clauses("cv.visit_date", data)
	clauses.extend(range_clauses)
	params.update(range_params)
	if data.get("employee"):
		clauses.append("cv.employee = %(employee)s")
		params["employee"] = data["employee"]
	purpose = (data.get("purpose") or "").strip()
	if purpose == "Unspecified":
		clauses.append("(li.purpose IS NULL OR li.purpose = '')")
	elif purpose:
		clauses.append("li.purpose = %(purpose)s")
		params["purpose"] = purpose
	where = _where(clauses)

	rows = frappe.db.sql(
		"SELECT cv.employee, COALESCE(NULLIF(e.employee_name, ''), MAX(NULLIF(cv.full_name, '')), "
		"MAX(NULLIF(cv.candidate_name, '')), cv.employee) AS employee_name, "
		"MAX(cv.payroll_number) AS payroll_number, COUNT(*) AS items, COUNT(DISTINCT cv.name) AS visits, "
		"COALESCE(SUM(li.cost), 0) AS cost, MAX(cv.visit_date) AS last_visit "
		"FROM `tabVisit Line Item` li INNER JOIN `tabClinic Visit Cost` cv ON li.parent = cv.name "
		"LEFT JOIN `tabEmployee` e ON e.name = cv.employee"
		+ where
		+ " GROUP BY cv.employee, e.employee_name ORDER BY cost DESC, employee_name ASC LIMIT 500",
		params,
		as_dict=True,
	)
	total = 0.0
	for r in rows:
		r["items"] = int(r.get("items") or 0)
		r["visits"] = int(r.get("visits") or 0)
		r["cost"] = round(float(r.get("cost") or 0), 2)
		total += r["cost"]
		r["last_visit"] = str(r["last_visit"]) if r.get("last_visit") else ""
		if r.get("employee") and frappe.db.exists("Employee", r["employee"]):
			r["route"] = _desk_form_route("Employee", r["employee"])

	out = {"purpose": purpose, "rows": rows, "total": round(total, 2)}
	frappe.response["message"] = out
	return out


@frappe.whitelist()
def sick_off_people():
	"""The sick-off records behind one Sick-Off & Leave KPI tile.

	``kind`` picks the tile: ``with_leave`` / ``without_leave`` for the two
	leave tiles, ``employees`` for one row per person, or nothing for every
	record in the period. The period is the sick-off start date, exactly as
	the tiles compute it."""
	assert_health_report_access("sickoff")
	data, year, month_num = _dashboard_request()
	kind = (data.get("kind") or "").strip()

	clauses, params = _period_clauses("cc.start_date", year, month_num)
	range_clauses, range_params = _range_clauses("cc.start_date", data)
	clauses.extend(range_clauses)
	params.update(range_params)
	clauses.insert(0, "cc.employee IS NOT NULL AND cc.employee != ''")
	clauses.insert(1, "cc.start_date IS NOT NULL")
	clauses.insert(2, "cc.end_date IS NOT NULL")
	if kind == "with_leave":
		clauses.append("cc.leave_application IS NOT NULL AND cc.leave_application != ''")
	elif kind == "without_leave":
		clauses.append("(cc.leave_application IS NULL OR cc.leave_application = '')")
	where = _where(clauses)

	name = "COALESCE(NULLIF(e.employee_name, ''), NULLIF(cc.full_name, ''), cc.employee)"
	ident = "COALESCE(NULLIF(cc.payroll_number, ''), NULLIF(cc.employee_payroll_number, ''))"
	joins = " LEFT JOIN `tabEmployee` e ON e.name = cc.employee"

	if kind == "employees":
		rows = frappe.db.sql(
			f"SELECT cc.employee, MAX({name}) AS employee_name, MAX({ident}) AS payroll_number, "
			"COUNT(*) AS episodes, SUM(DATEDIFF(cc.end_date, cc.start_date) + 1) AS days, "
			"SUM(CASE WHEN cc.leave_application IS NOT NULL AND cc.leave_application != '' THEN 1 ELSE 0 END) AS with_leave, "
			"MIN(cc.start_date) AS first_off, MAX(cc.end_date) AS last_off "
			"FROM `tabClinic Checkin` cc"
			+ joins
			+ where
			+ " GROUP BY cc.employee ORDER BY days DESC, employee_name ASC LIMIT 500",
			params,
			as_dict=True,
		)
	else:
		rows = frappe.db.sql(
			f"SELECT cc.name, cc.employee, {name} AS employee_name, {ident} AS payroll_number, "
			"cc.start_date, cc.end_date, DATEDIFF(cc.end_date, cc.start_date) + 1 AS days, "
			"cc.reason, cc.leave_application "
			"FROM `tabClinic Checkin` cc"
			+ joins
			+ where
			+ " ORDER BY cc.start_date DESC, cc.name DESC LIMIT 500",
			params,
			as_dict=True,
		)

	for r in rows:
		for k in ("episodes", "days", "with_leave"):
			if r.get(k) is not None:
				r[k] = int(r[k])
		for k in ("start_date", "end_date", "first_off", "last_off"):
			if k in r:
				r[k] = str(r[k]) if r.get(k) else ""
		if r.get("name"):
			r["route"] = _desk_form_route("Clinic Checkin", r["name"])
		if r.get("leave_application"):
			r["leave_route"] = _desk_form_route("Leave Application", r["leave_application"])

	out = {"kind": kind, "rows": rows, "grouped": kind == "employees"}
	frappe.response["message"] = out
	return out


@frappe.whitelist()
def test_request_people():
	"""The requests behind one Test Requests KPI tile or Packages by Status cell.

	``kind`` picks the tile: a status value, ``overdue`` for pending requests
	past their window, or nothing for every request in the period. Any
	``status`` / ``test_package`` / ``member_type`` filter narrows further, so a
	cell of the package grid passes its package and status. The period is the
	scheduled-from date, exactly as the report computes it."""
	assert_health_report_access("requests")
	data, year, month_num = _dashboard_request()

	clauses, params = _period_clauses("tr.scheduled_from", year, month_num)
	range_clauses, range_params = _range_clauses("tr.scheduled_from", data)
	clauses.extend(range_clauses)
	params.update(range_params)
	for key in ("status", "test_package", "member_type"):
		if data.get(key):
			if data[key] == "Unspecified":
				clauses.append(f"(tr.{key} IS NULL OR tr.{key} = '')")
			else:
				clauses.append(f"tr.{key} = %({key})s")
				params[key] = data[key]

	kind = (data.get("kind") or "").strip()
	if kind == "overdue":
		clauses.append("tr.status = 'Pending'")
		clauses.append("tr.scheduled_to < CURDATE()")
	elif kind and not data.get("status"):
		clauses.append("tr.status = %(kind)s")
		params["kind"] = kind
	where = _where(clauses)

	rows = frappe.db.sql(
		"SELECT tr.name, tr.employee, "
		"COALESCE(NULLIF(e.employee_name, ''), NULLIF(jo.applicant_name, ''), "
		"NULLIF(tr.payroll_number, ''), NULLIF(tr.nationa_id, ''), tr.name) AS who, "
		"COALESCE(NULLIF(tr.payroll_number, ''), NULLIF(tr.nationa_id, '')) AS payroll_number, "
		"tr.member_type, tr.test_package, tr.status, tr.scheduled_from, tr.scheduled_to, "
		"tr.linked_test_result, "
		"CASE WHEN tr.status = 'Pending' AND tr.scheduled_to < CURDATE() "
		"THEN DATEDIFF(CURDATE(), tr.scheduled_to) ELSE 0 END AS days_late "
		"FROM `tabClinic Test Request` tr "
		"LEFT JOIN `tabEmployee` e ON e.name = tr.employee "
		"LEFT JOIN `tabJob Offer` jo ON tr.nationa_id IS NOT NULL AND tr.nationa_id != '' "
		"AND jo.national_id = tr.nationa_id"
		+ where
		+ " ORDER BY tr.scheduled_from DESC, tr.name DESC LIMIT 500",
		params,
		as_dict=True,
	)
	for r in rows:
		for k in ("scheduled_from", "scheduled_to"):
			r[k] = str(r[k]) if r.get(k) else ""
		r["days_late"] = int(r.get("days_late") or 0)
		r["route"] = _desk_form_route("Clinic Test Request", r["name"])
		if r.get("linked_test_result"):
			r["result_route"] = _desk_form_route("Clinic Test Result", r["linked_test_result"])

	out = {"kind": kind, "rows": rows}
	frappe.response["message"] = out
	return out


@frappe.whitelist()
def test_result_people():
	"""The people behind one Test Results KPI tile.

	``outcome`` picks the tile: a clinical outcome value, or "employees" for the
	distinct-employee count, or nothing at all for every result in the period.
	The period and view filters are applied exactly as the tiles compute them,
	so the list can never disagree with the number that was clicked."""
	assert_health_report_access("results")
	data, year, month_num = _dashboard_request()

	clauses, params = _period_clauses("ctr.creation", year, month_num)
	range_clauses, range_params = _range_clauses("ctr.creation", data)
	clauses.extend(range_clauses)
	params.update(range_params)
	for key in ("test_package", "member_type"):
		if data.get(key):
			if data[key] == "Unspecified":
				# The report labels a blank package "Unspecified"; clicking that
				# row must list the results with no package, not none at all.
				clauses.append(f"(ctr.{key} IS NULL OR ctr.{key} = '')")
			else:
				clauses.append(f"ctr.{key} = %({key})s")
				params[key] = data[key]

	# A cell of the Condition by Risk Grade grid: the grade lives on the child
	# Test Result rows, so filter through them without multiplying parents.
	medical_case = (data.get("medical_case") or "").strip()
	risk = (data.get("risk") or "").strip()
	if medical_case or risk:
		sub = ["t.parent = ctr.name"]
		if medical_case == "Unspecified":
			sub.append("(t.test IS NULL OR t.test = '')")
		elif medical_case:
			sub.append("t.test = %(medical_case)s")
			params["medical_case"] = medical_case
		if risk == "No Risk":
			sub.append("(t.select_tezd IS NULL OR t.select_tezd = '' OR t.select_tezd = 'No Risk')")
		elif risk:
			sub.append("t.select_tezd = %(risk)s")
			params["risk"] = risk
		clauses.append("EXISTS (SELECT 1 FROM `tabTest Result` t WHERE " + " AND ".join(sub) + ")")

	outcome = (data.get("outcome") or "").strip()
	group_by_employee = outcome == "employees"
	if outcome and not group_by_employee:
		clauses.append("ctr.clinical_outcome = %(outcome)s")
		params["outcome"] = outcome
	elif data.get("clinical_outcome"):
		clauses.append("ctr.clinical_outcome = %(clinical_outcome)s")
		params["clinical_outcome"] = data["clinical_outcome"]

	where = _where(clauses)

	# A pre-employment result belongs to a candidate who has no Employee record
	# yet — the test was raised from a Job Offer. Nothing on the result itself
	# names them, so walk out to the request for the National ID and pick the
	# name up from the member register or the offer it came from. Without this
	# the row renders with no name at all.
	joins = (
		" LEFT JOIN `tabEmployee` e ON e.name = ctr.employee"
		" LEFT JOIN `tabCova Members` cm ON cm.name = ctr.cova_member"
		" LEFT JOIN `tabClinic Test Request` tr ON tr.name = ctr.request_id"
		" LEFT JOIN `tabCova Members` cmn ON cmn.national_id = tr.nationa_id"
		" LEFT JOIN `tabJob Offer` jo ON jo.national_id = tr.nationa_id"
		# Last resort for rows whose National ID matches nothing: the request's
		# own note records where it came from. Both shapes are written by
		# register_preemployment_candidate, so the format is ours, not guesswork.
		" LEFT JOIN `tabJob Offer` jon ON tr.notes LIKE 'Pre-employment wellness for Job Offer %%'"
		" AND jon.name = TRIM(SUBSTRING_INDEX(tr.notes, 'Job Offer ', -1))"
		" LEFT JOIN `tabJob Applicant` ja ON tr.notes LIKE 'Pre-employment wellness for Job Applicant %%'"
		" AND ja.name = TRIM(SUBSTRING_INDEX(tr.notes, 'Job Applicant ', -1))"
	)
	name = (
		"COALESCE("
		"NULLIF(e.employee_name, ''), NULLIF(ctr.full_name, ''), NULLIF(cm.full_name, ''), "
		"NULLIF(cmn.full_name, ''), NULLIF(jo.applicant_name, ''), "
		"NULLIF(jon.applicant_name, ''), NULLIF(ja.applicant_name, ''), "
		"NULLIF(ctr.employee, ''), NULLIF(ctr.cova_member, ''), "
		# Nothing anywhere names them — show who it is by ID rather than the
		# record's own code, which reads as a bug to whoever opens the panel.
		"CONCAT('Candidate ', NULLIF(tr.nationa_id, '')), ctr.name)"
	)
	# Candidates have no payroll number; their National ID is the identifier.
	ident = "COALESCE(NULLIF(ctr.payroll_number, ''), NULLIF(tr.nationa_id, ''))"

	if group_by_employee:
		# The Employees tile is COUNT(DISTINCT ctr.employee), and COUNT(DISTINCT)
		# skips NULLs — so a result with no Employee link (a pre-employment one,
		# carrying only a name) is not in that number and must not be in this
		# list either, or the panel contradicts the tile that opened it.
		# Grouped on employee alone for the same reason.
		rows = frappe.db.sql(
			f"SELECT ctr.employee AS employee, MAX({name}) AS employee_name, "
			"MAX(ctr.payroll_number) AS payroll_number, COUNT(*) AS results, "
			"MAX(ctr.creation) AS received "
			"FROM `tabClinic Test Result` ctr"
			+ joins
			+ _where([*list(clauses), "ctr.employee IS NOT NULL", "ctr.employee != ''"])
			+ " GROUP BY ctr.employee ORDER BY employee_name ASC LIMIT 500",
			params,
			as_dict=True,
		)
	else:
		# The grade(s) on the child rows, so a list opened from the risk grid
		# shows the grade that put each result in that cell.
		grade_sub = ["tg.parent = ctr.name"]
		if medical_case and medical_case != "Unspecified":
			grade_sub.append("tg.test = %(medical_case)s")
		grade = (
			"(SELECT GROUP_CONCAT(DISTINCT COALESCE(NULLIF(tg.select_tezd, ''), 'No Risk') SEPARATOR ', ') "
			"FROM `tabTest Result` tg WHERE " + " AND ".join(grade_sub) + ")"
		)
		rows = frappe.db.sql(
			f"SELECT ctr.name, ctr.employee AS employee, {name} AS employee_name, "
			f"{ident} AS payroll_number, ctr.member_type, "
			f"ctr.clinical_outcome, ctr.test_package, {grade} AS risk_grade, ctr.creation AS received "
			"FROM `tabClinic Test Result` ctr" + joins + where + " ORDER BY ctr.creation DESC LIMIT 500",
			params,
			as_dict=True,
		)

	for r in rows:
		r["received"] = str(r["received"])[:10] if r.get("received") else ""
		if r.get("results") is not None:
			r["results"] = int(r["results"])
		if r.get("name"):
			r["route"] = _desk_form_route("Clinic Test Result", r["name"])

	out = {"rows": rows, "grouped": group_by_employee}
	frappe.response["message"] = out
	return out


@frappe.whitelist()
def clinic_test_result_report():
	"""Clinic Test Result outcomes plus the per-condition risk grades that COVA
	returns on the child ``Test Result`` table. Results have no posting date of
	their own, so the period runs off ``creation``."""
	assert_health_report_access("results")
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

	rs_year = _where(_year_clauses(clauses))
	rs_series_rows = frappe.db.sql(
		"SELECT MONTH(ctr.creation) AS m, COUNT(*) AS total, COUNT(DISTINCT ctr.employee) AS employees, "
		"SUM(CASE WHEN ctr.clinical_outcome = 'FitForWork' THEN 1 ELSE 0 END) AS fit, "
		"SUM(CASE WHEN ctr.clinical_outcome = 'FitWithRestrictions' THEN 1 ELSE 0 END) AS restricted, "
		"SUM(CASE WHEN ctr.clinical_outcome = 'UnfitForWork' THEN 1 ELSE 0 END) AS unfit, "
		"SUM(CASE WHEN ctr.clinical_outcome = 'InconclusiveRetestRequired' THEN 1 ELSE 0 END) AS retest "
		"FROM `tabClinic Test Result` ctr" + rs_year + " GROUP BY MONTH(ctr.creation)",
		params,
		as_dict=True,
	)
	rs_series = _kpi_series(rs_series_rows, ["total", "employees", "fit", "restricted", "unfit", "retest"])

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
		"kpi_series": rs_series,
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
#
# An employee seen outside the clinic is posted with `is_external` plus the
# `facility` that saw them and the `sick_off_given` (days) it gave. Sending a
# `facility` alone implies `is_external`.


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
				SELECT employee, start_date, end_date, reason, time_in, time_out,
					is_external, facility, sick_off_given
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
				record["is_external"] = int(record.get("is_external") or 0)
				record["sick_off_given"] = int(record.get("sick_off_given") or 0)

			frappe.response.pop("docs", None)
			frappe.response["status"] = "success"
			frappe.response["count"] = len(records)
			frappe.response["data"] = records
			frappe.response.http_status_code = 200

		except Exception as e:
			frappe.log_error(title="Clinic Data API GET Error", message=str(e))
			frappe.response.update(
				{"status": "error", "message": "Failed to fetch records: {0}".format(str(e))}
			)
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

			facility = (data.get("facility") or "").strip() or None
			is_external = 1 if (frappe.utils.cint(data.get("is_external")) or facility) else 0
			sick_off_given = data.get("sick_off_given")
			if is_external:
				if not facility:
					frappe.throw("Missing required field: 'facility' (required when 'is_external' is set)")
				if sick_off_given not in (None, ""):
					try:
						sick_off_given = int(sick_off_given)
					except (TypeError, ValueError):
						frappe.throw("'sick_off_given' must be a whole number of days")
					if sick_off_given < 0:
						frappe.throw("'sick_off_given' cannot be negative")

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
					"is_external": is_external,
					"facility": facility if is_external else None,
					"sick_off_given": (sick_off_given or 0) if is_external else 0,
				}
			).insert(ignore_permissions=True)

			record_cova_message("Clinic Checkin", doc.name, "clinic_data_post", "received", data)

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
						"is_external": doc.is_external,
						"facility": doc.facility,
						"sick_off_given": doc.sick_off_given,
					},
				}
			)
			frappe.response.http_status_code = 201

		except Exception as e:
			frappe.log_error(title="Clinic Data API POST Error", message=str(e))
			frappe.response.update(
				{"status": "error", "message": "Failed to process request: {0}".format(str(e))}
			)
			frappe.response.http_status_code = 500

	else:
		frappe.response.update(
			{
				"status": "error",
				"message": "Method '{0}' not allowed. Supported methods: GET, POST".format(method),
			}
		)
		frappe.response.http_status_code = 405


# ─── clinic tickets / test scheduling dashboards ────────────────────────────


def _option_values(sql):
	return [r[0] for r in frappe.db.sql(sql) if r[0]]


@frappe.whitelist()
def clinic_overview_report():
	"""Overview: the headline totals from every other section — cases,
	tickets, medical gate passes, visits, sick-offs and tests — for one year
	(and month). Each figure is included only when the viewer may open the
	section it comes from, so the overview never shows more than they could
	see section by section."""
	assert_health_report_access("overview")
	data, year, month_num = _dashboard_request()
	year = year or frappe.utils.getdate().year
	allowed = set(allowed_dashboard_sections())

	def period(field):
		clauses, params = _period_clauses(field, year, month_num)
		return _where(clauses), params

	def by_month(sql, field, params):
		rows = frappe.db.sql(sql + f" GROUP BY MONTH({field})", params, as_dict=True)
		return _month_series(rows)["values"]

	kpis, series, top_cases = {}, {}, []

	if "health" in allowed:
		clauses = ["YEAR(hmr.posting_date) = %(year)s"]
		params = {"year": year}
		if month_num:
			clauses.append("UPPER(hmr.month) = %(month)s")
			params["month"] = MONTH_LABELS[month_num - 1]
		where = _where(clauses)
		kpis["cases"] = int(
			frappe.db.sql("SELECT COALESCE(SUM(hr.case_count), 0) " + _DISEASE_FROM + where, params)[0][0]
			or 0
		)
		top_cases = frappe.db.sql(
			"SELECT hr.medical_case AS condition_name, SUM(hr.case_count) AS cases "
			+ _DISEASE_FROM
			+ where
			+ " GROUP BY hr.medical_case ORDER BY cases DESC LIMIT 10",
			params,
			as_dict=True,
		)
		for r in top_cases:
			r["cases"] = int(r["cases"] or 0)
		rows = frappe.db.sql(
			"SELECT MONTH(hmr.posting_date) AS m, SUM(hr.case_count) AS cnt "
			+ _DISEASE_FROM
			+ " WHERE YEAR(hmr.posting_date) = %(year)s GROUP BY MONTH(hmr.posting_date)",
			{"year": year},
			as_dict=True,
		)
		series["cases"] = _month_series(rows)["values"]

	if "tickets" in allowed:
		where, params = period("ticket_date")
		t = frappe.db.sql(
			"SELECT COUNT(*) AS total, SUM(status = 'Visited') AS visited FROM `tabClinic Ticket`" + where,
			params,
			as_dict=True,
		)[0]
		kpis["tickets"] = int(t.total or 0)
		kpis["tickets_visited"] = int(t.visited or 0)
		series["tickets"] = by_month(
			"SELECT MONTH(ticket_date) AS m, COUNT(*) AS cnt FROM `tabClinic Ticket`"
			" WHERE YEAR(ticket_date) = %(year)s",
			"ticket_date",
			{"year": year},
		)
		if frappe.db.exists("DocType", "Gate Pass"):
			where, params = period("date")
			kpis["medical_gate_passes"] = int(
				frappe.db.sql(
					"SELECT COUNT(*) FROM `tabGate Pass`"
					+ (where + " AND" if where else " WHERE")
					+ " pass_type = 'Medical' AND docstatus < 2",
					params,
				)[0][0]
				or 0
			)

	if "biometric" in allowed:
		where, params = period("cc.time")
		kpis["clinic_visits"] = int(
			frappe.db.sql(
				"SELECT COUNT(DISTINCT cc.employee, DATE(cc.time)) FROM `tabClinic Checkin` cc"
				+ (where + " AND " if where else " WHERE ")
				+ _PUNCH_CLAUSE,
				params,
			)[0][0]
			or 0
		)

	if "sickoff" in allowed:
		where, params = period("start_date")
		s = frappe.db.sql(
			"SELECT COUNT(*) AS records, SUM(DATEDIFF(end_date, start_date) + 1) AS days FROM `tabClinic Checkin`"
			+ (where + " AND" if where else " WHERE")
			+ " start_date IS NOT NULL AND end_date IS NOT NULL",
			params,
			as_dict=True,
		)[0]
		kpis["sick_offs"] = int(s.records or 0)
		kpis["sick_days"] = int(s.days or 0)

	if allowed & {"requests", "schedules"}:
		where, params = period("scheduled_from")
		r = frappe.db.sql(
			"SELECT COUNT(*) AS total, SUM(status = 'Pending') AS pending, SUM(status = 'Completed') AS completed "
			"FROM `tabClinic Test Request`" + where,
			params,
			as_dict=True,
		)[0]
		kpis["tests"] = int(r.total or 0)
		kpis["tests_pending"] = int(r.pending or 0)
		kpis["tests_completed"] = int(r.completed or 0)

	if "accidents" in allowed:
		where, params = period("accident_date")
		a = frappe.db.sql(
			"SELECT COUNT(*) AS total, SUM(lost_time_injury = 1) AS lost_time, COALESCE(SUM(days_lost), 0) AS days_lost "
			"FROM `tabWork Accident`" + where,
			params,
			as_dict=True,
		)[0]
		kpis["accidents"] = int(a.total or 0)
		kpis["accidents_lost_time"] = int(a.lost_time or 0)
		kpis["accidents_days_lost"] = int(a.days_lost or 0)

	years = sorted(
		{
			str(y)
			for y in _option_values("SELECT DISTINCT YEAR(posting_date) FROM `tabHealth Monthly Report`")
			+ _option_values("SELECT DISTINCT YEAR(ticket_date) FROM `tabClinic Ticket`")
			+ _option_values("SELECT DISTINCT YEAR(`time`) FROM `tabClinic Checkin`")
		}
		| {str(year)},
		reverse=True,
	)

	resp = {
		"year": str(year),
		"kpis": kpis,
		"by_month": {"months": list(MONTH_LABELS), **series},
		"top_cases": top_cases,
		"filter_options": {"years": years, "months": list(MONTH_LABELS)},
	}
	frappe.response["message"] = resp
	return resp


@frappe.whitelist()
def clinic_ticket_report():
	"""Clinic Tickets: who was allowed to go to the clinic, and whether they went."""
	assert_health_report_access("tickets")
	data, year, month_num = _dashboard_request()

	clauses, params = _period_clauses("ct.ticket_date", year, month_num)
	range_clauses, range_params = _range_clauses("ct.ticket_date", data)
	clauses.extend(range_clauses)
	params.update(range_params)
	for key in ("department", "designation", "status"):
		if data.get(key):
			clauses.append(f"ct.{key} = %({key})s")
			params[key] = data.get(key)
	where = _where(clauses)

	counts = (
		"COUNT(*) AS total, COUNT(DISTINCT ct.employee) AS employees, "
		"SUM(ct.status = 'Issued') AS issued, SUM(ct.status = 'Visited') AS visited, "
		"SUM(ct.status = 'Expired') AS expired, SUM(ct.status = 'Cancelled') AS cancelled, "
		"ROUND(AVG(CASE WHEN ct.arrived = 1 THEN ct.minutes_to_reach END)) AS avg_minutes_to_reach, "
		"SUM(ct.urgency = 'Emergency') AS emergencies "
	)
	totals = frappe.db.sql("SELECT " + counts + "FROM `tabClinic Ticket` ct" + where, params, as_dict=True)[0]
	kpis = {
		k: int(totals.get(k) or 0)
		for k in (
			"total",
			"employees",
			"issued",
			"visited",
			"expired",
			"cancelled",
			"avg_minutes_to_reach",
			"emergencies",
		)
	}
	kpis["visit_rate"] = _pct(kpis["visited"], kpis["total"] - kpis["cancelled"])
	has_gate_pass = frappe.db.has_column("Clinic Ticket", "gate_pass")
	gp_col = "ct.gate_pass" if has_gate_pass else "NULL"

	series_rows = frappe.db.sql(
		"SELECT MONTH(ct.ticket_date) AS m, "
		+ counts
		+ "FROM `tabClinic Ticket` ct"
		+ _where(_year_clauses(clauses))
		+ " GROUP BY MONTH(ct.ticket_date)",
		params,
		as_dict=True,
	)
	by_department = frappe.db.sql(
		"SELECT COALESCE(NULLIF(ct.department, ''), 'Unassigned') AS department, "
		+ counts
		+ "FROM `tabClinic Ticket` ct"
		+ where
		+ " GROUP BY department ORDER BY total DESC LIMIT 30",
		params,
		as_dict=True,
	)
	rows = frappe.db.sql(
		"SELECT ct.name, ct.employee, ct.employee_name, ct.department, ct.ticket_date, ct.valid_until, "
		f"ct.appointment_time, ct.urgency, ct.status, ct.visited_at, "
		f"CASE WHEN ct.arrived = 1 THEN ct.minutes_to_reach END AS minutes_to_reach, {gp_col} AS gate_pass, "
		"ct.reason FROM `tabClinic Ticket` ct"
		+ where
		+ " ORDER BY ct.ticket_date DESC, ct.creation DESC LIMIT 200",
		params,
		as_dict=True,
	)
	by_urgency = frappe.db.sql(
		"SELECT COALESCE(NULLIF(ct.urgency, ''), 'Routine') AS urgency, "
		+ counts
		+ "FROM `tabClinic Ticket` ct"
		+ where
		+ " GROUP BY urgency",
		params,
		as_dict=True,
	)
	# The clinic desk: every ticket still waiting for its employee to arrive,
	# regardless of the period filters — the desk works on today, not a report.
	open_rows = frappe.db.sql(
		f"SELECT ct.name, ct.employee, ct.employee_name, ct.department, ct.ticket_date, ct.valid_until, "
		f"ct.appointment_time, ct.urgency, ct.reason, {gp_col} AS gate_pass "
		"FROM `tabClinic Ticket` ct WHERE ct.status = 'Issued' "
		"ORDER BY FIELD(ct.urgency, 'Emergency', 'Urgent', 'Routine'), ct.ticket_date, ct.appointment_time LIMIT 200",
		as_dict=True,
	)
	for r in open_rows:
		for k in ("ticket_date", "valid_until"):
			r[k] = str(r[k]) if r.get(k) else ""
		r["appointment_time"] = str(r["appointment_time"])[:5] if r.get("appointment_time") else ""
		r["route"] = _desk_form_route("Clinic Ticket", r["name"])
		if r.get("gate_pass"):
			r["gate_pass_route"] = _desk_form_route("Gate Pass", r["gate_pass"])
	for r in by_department + series_rows + by_urgency:
		for k in (
			"total",
			"employees",
			"issued",
			"visited",
			"expired",
			"cancelled",
			"avg_minutes_to_reach",
			"emergencies",
		):
			r[k] = int(r.get(k) or 0)
	for r in rows:
		for k in ("ticket_date", "valid_until"):
			r[k] = str(r[k]) if r.get(k) else ""
		r["visited_at"] = str(r["visited_at"])[:16] if r.get("visited_at") else ""
		r["appointment_time"] = str(r["appointment_time"])[:5] if r.get("appointment_time") else ""
		r["route"] = _desk_form_route("Clinic Ticket", r["name"])

	resp = {
		"kpis": kpis,
		"kpi_series": _kpi_series(series_rows, ["total", "visited", "issued", "expired"]),
		"by_month": {
			"months": list(MONTH_LABELS),
			"visited": _kpi_series(series_rows, ["visited"])["visited"],
			"total": _kpi_series(series_rows, ["total"])["total"],
		},
		"by_department": by_department,
		"by_urgency": by_urgency,
		"rows": rows,
		"open_tickets": open_rows,
		"gate_pass_enabled": has_gate_pass and frappe.db.exists("DocType", "Gate Pass") is not None,
		"filter_options": {
			"years": [
				str(y)
				for y in _option_values(
					"SELECT DISTINCT YEAR(ticket_date) FROM `tabClinic Ticket` ORDER BY 1 DESC"
				)
			],
			"months": list(MONTH_LABELS),
			"departments": _option_values(
				"SELECT DISTINCT department FROM `tabClinic Ticket` WHERE department IS NOT NULL ORDER BY 1"
			),
			"statuses": ["Issued", "Visited", "Expired", "Cancelled"],
		},
	}
	frappe.response["message"] = resp
	return resp


@frappe.whitelist()
def clinic_schedule_report():
	"""Test Scheduling: the annual tests broken down by the test group,
	department and designation each employee was scheduled under."""
	assert_health_report_access("schedules")
	data, year, month_num = _dashboard_request()

	clauses, params = _period_clauses("tr.scheduled_from", year, month_num)
	range_clauses, range_params = _range_clauses("tr.scheduled_from", data)
	clauses.extend(range_clauses)
	params.update(range_params)
	clauses.insert(0, "tr.member_type = 'Active'")
	for key in ("test_group", "test_package", "department", "designation"):
		if data.get(key):
			clauses.append(f"tr.{key} = %({key})s")
			params[key] = data.get(key)
	where = _where(clauses)
	params["today"] = frappe.utils.nowdate()

	counts = (
		"COUNT(*) AS total, SUM(tr.status = 'Completed') AS completed, SUM(tr.status = 'Pending') AS pending, "
		"SUM(tr.status = 'Pending' AND tr.scheduled_to < %(today)s) AS overdue, "
		"SUM(tr.received_by_cova = 1) AS received "
	)
	keys = ("total", "completed", "pending", "overdue", "received")

	def grouped(column, label):
		rows = frappe.db.sql(
			f"SELECT COALESCE(NULLIF(tr.{column}, ''), 'Unassigned') AS {label}, "
			+ counts
			+ "FROM `tabClinic Test Request` tr"
			+ where
			+ f" GROUP BY {label} ORDER BY total DESC LIMIT 40",
			params,
			as_dict=True,
		)
		for r in rows:
			for k in keys:
				r[k] = int(r.get(k) or 0)
			r["completion_rate"] = _pct(r["completed"], r["total"])
		return rows

	totals = frappe.db.sql(
		"SELECT " + counts + "FROM `tabClinic Test Request` tr" + where, params, as_dict=True
	)[0]
	kpis = {k: int(totals.get(k) or 0) for k in keys}
	kpis["completion_rate"] = _pct(kpis["completed"], kpis["total"])

	sched_clauses, sched_params = _period_clauses("cts.scheduled_from", year, month_num)
	schedules = frappe.db.sql(
		"SELECT cts.name, cts.title, cts.test_group, cts.test_package, cts.scheduled_from, cts.scheduled_to, "
		"cts.total_employees, cts.requests_created, cts.status FROM `tabClinic Test Schedule` cts"
		+ _where(sched_clauses)
		+ " ORDER BY cts.scheduled_from DESC LIMIT 100",
		sched_params,
		as_dict=True,
	)
	for r in schedules:
		for k in ("scheduled_from", "scheduled_to"):
			r[k] = str(r[k]) if r.get(k) else ""
		r["route"] = _desk_form_route("Clinic Test Schedule", r["name"])
	kpis["schedules"] = len(schedules)

	series_rows = frappe.db.sql(
		"SELECT MONTH(tr.scheduled_from) AS m, "
		+ counts
		+ "FROM `tabClinic Test Request` tr"
		+ _where(_year_clauses(clauses))
		+ " GROUP BY MONTH(tr.scheduled_from)",
		params,
		as_dict=True,
	)

	resp = {
		"kpis": kpis,
		"kpi_series": _kpi_series(series_rows, list(keys)),
		"by_group": grouped("test_group", "test_group"),
		"by_department": grouped("department", "department"),
		"by_designation": grouped("designation", "designation"),
		"schedules": schedules,
		"plans": _test_plans(),
		"filter_options": {
			"years": [
				str(y)
				for y in _option_values(
					"SELECT DISTINCT YEAR(scheduled_from) FROM `tabClinic Test Request` ORDER BY 1 DESC"
				)
			],
			"months": list(MONTH_LABELS),
			"test_groups": sorted(
				set(get_test_groups())
				| set(
					_option_values(
						"SELECT DISTINCT test_group FROM `tabClinic Test Request` WHERE test_group IS NOT NULL"
					)
				)
			),
			"test_packages": _option_values(
				"SELECT name FROM `tabTest Package` WHERE disabled = 0 ORDER BY name"
			),
			"departments": _option_values(
				"SELECT DISTINCT department FROM `tabClinic Test Request` WHERE department IS NOT NULL ORDER BY 1"
			),
		},
	}
	frappe.response["message"] = resp
	return resp


# ─── work accidents ─────────────────────────────────────────────────────────


@frappe.whitelist()
def work_accident_report():
	"""Work Accidents: every recorded workplace accident, and what they add up to."""
	assert_health_report_access("accidents")
	data, year, month_num = _dashboard_request()

	clauses, params = _period_clauses("wa.accident_date", year, month_num)
	range_clauses, range_params = _range_clauses("wa.accident_date", data)
	clauses.extend(range_clauses)
	params.update(range_params)
	for key in ("department", "designation", "severity", "accident_type", "status"):
		if data.get(key):
			clauses.append(f"wa.{key} = %({key})s")
			params[key] = data.get(key)
	where = _where(clauses)

	counts = (
		"COUNT(*) AS total, COUNT(DISTINCT wa.employee) AS employees, "
		"SUM(wa.lost_time_injury = 1) AS lost_time, COALESCE(SUM(wa.days_lost), 0) AS days_lost, "
		"SUM(wa.severity IN ('Serious', 'Fatal')) AS serious, SUM(wa.severity = 'Fatal') AS fatal, "
		"SUM(wa.first_aid_given = 1) AS first_aid, SUM(wa.status != 'Closed') AS open_cases "
	)
	keys = ("total", "employees", "lost_time", "days_lost", "serious", "fatal", "first_aid", "open_cases")
	base = "FROM `tabWork Accident` wa"

	def ints(rows):
		for r in rows:
			for k in keys:
				r[k] = int(r.get(k) or 0)
		return rows

	totals = ints(frappe.db.sql("SELECT " + counts + base + where, params, as_dict=True))[0]
	last_lti = frappe.db.sql("SELECT MAX(accident_date) FROM `tabWork Accident` WHERE lost_time_injury = 1")[
		0
	][0]
	totals["days_since_lost_time"] = (
		frappe.utils.date_diff(frappe.utils.nowdate(), last_lti) if last_lti else None
	)

	def grouped(column, label, order="total DESC"):
		return ints(
			frappe.db.sql(
				f"SELECT COALESCE(NULLIF(wa.{column}, ''), 'Not recorded') AS {label}, "
				+ counts
				+ base
				+ where
				+ f" GROUP BY {label} ORDER BY {order} LIMIT 40",
				params,
				as_dict=True,
			)
		)

	series_rows = ints(
		frappe.db.sql(
			"SELECT MONTH(wa.accident_date) AS m, "
			+ counts
			+ base
			+ _where(_year_clauses(clauses))
			+ " GROUP BY MONTH(wa.accident_date)",
			params,
			as_dict=True,
		)
	)
	rows = frappe.db.sql(
		"SELECT wa.name, wa.employee, wa.employee_name, wa.department, wa.accident_date, wa.accident_time, "
		"wa.location, wa.accident_type, wa.severity, wa.body_part, wa.days_lost, wa.status, wa.clinic_ticket, "
		"wa.first_aider, (SELECT fa.employee_name FROM `tabEmployee` fa WHERE fa.name = wa.first_aider) AS first_aider_name "
		+ base
		+ where
		+ " ORDER BY wa.accident_date DESC, wa.creation DESC LIMIT 200",
		params,
		as_dict=True,
	)
	for r in rows:
		r["accident_date"] = str(r["accident_date"]) if r.get("accident_date") else ""
		r["accident_time"] = str(r["accident_time"])[:5] if r.get("accident_time") else ""
		r["days_lost"] = int(r.get("days_lost") or 0)
		r["route"] = _desk_form_route("Work Accident", r["name"])
		if r.get("clinic_ticket"):
			r["ticket_route"] = _desk_form_route("Clinic Ticket", r["clinic_ticket"])

	meta = frappe.get_meta("Work Accident")
	resp = {
		"kpis": totals,
		"kpi_series": _kpi_series(series_rows, list(keys)),
		"by_month": {
			"months": list(MONTH_LABELS),
			"total": _kpi_series(series_rows, ["total"])["total"],
			"lost_time": _kpi_series(series_rows, ["lost_time"])["lost_time"],
		},
		"by_type": grouped("accident_type", "accident_type"),
		"by_department": grouped("department", "department"),
		"by_body_part": grouped("body_part", "body_part"),
		"by_severity": grouped(
			"severity", "severity", "FIELD(severity, 'Fatal', 'Serious', 'Moderate', 'Minor')"
		),
		"rows": rows,
		"first_aiders": _first_aiders(),
		"filter_options": {
			"years": [
				str(y)
				for y in _option_values(
					"SELECT DISTINCT YEAR(accident_date) FROM `tabWork Accident` ORDER BY 1 DESC"
				)
			],
			"months": list(MONTH_LABELS),
			"severities": meta.get_field("severity").options.split("\n"),
			"accident_types": meta.get_field("accident_type").options.split("\n"),
			"body_parts": [o for o in meta.get_field("body_part").options.split("\n") if o],
			"statuses": meta.get_field("status").options.split("\n"),
		},
	}
	frappe.response["message"] = resp
	return resp


def _farm_column():
	"""The Employee column that says which farm someone works on: the site's own
	Farm field where it has one, otherwise the standard Branch."""
	return "custom_farm" if frappe.db.has_column("Employee", "custom_farm") else "branch"


def _first_aiders():
	"""Active employees marked First Aider, by farm, with their phone, how many
	accidents they have attended and whether their certificate is still good."""
	farm = _farm_column()
	fallback = "e.branch" if farm == "custom_farm" else "NULL"
	rows = frappe.db.sql(
		f"SELECT e.name, e.name AS employee, e.employee_name, e.cell_number AS phone, "
		f"COALESCE(NULLIF(e.{farm}, ''), {fallback}) AS farm, e.department, "
		"e.first_aid_certified_until AS certified_until, "
		"(SELECT COUNT(*) FROM `tabWork Accident` wa WHERE wa.first_aider = e.name) AS attended "
		"FROM `tabEmployee` e WHERE e.status = 'Active' AND e.is_first_aider = 1 "
		"ORDER BY farm, e.employee_name",
		as_dict=True,
	)
	today = frappe.utils.getdate()
	for r in rows:
		until = r.certified_until
		if not until:
			r.certificate = "Not recorded"
		elif until < today:
			r.certificate = "Expired"
		elif frappe.utils.date_diff(until, today) <= 60:
			r.certificate = "Expiring"
		else:
			r.certificate = "Valid"
		r.certified_until = str(until) if until else ""
		r.attended = int(r.attended or 0)
		r.route = _desk_form_route("Employee", r.name)
	return rows


@frappe.whitelist()
def add_first_aider():
	"""Mark an employee as a first aider from the dashboard. Phone and farm are
	the employee's own; a phone entered here is saved onto their record."""
	assert_health_report_access("accidents")
	data = frappe.request.get_json() or {}
	employee = data.get("employee")
	if not employee or not frappe.db.exists("Employee", employee):
		frappe.throw(_("Pick the employee."))
	values = {"is_first_aider": 1}
	if data.get("certified_until"):
		values["first_aid_certified_until"] = data["certified_until"]
	phone = (data.get("phone") or "").strip()
	if phone:
		values["cell_number"] = phone
	elif not frappe.db.get_value("Employee", employee, "cell_number"):
		frappe.throw(
			_("{0} has no mobile number on their employee record; enter one.").format(
				frappe.db.get_value("Employee", employee, "employee_name")
			)
		)
	frappe.db.set_value("Employee", employee, values)
	row = frappe.db.get_value(
		"Employee", employee, ["employee_name", "cell_number", _farm_column()], as_dict=True
	)
	out = {
		"name": employee,
		"employee_name": row.employee_name,
		"phone": row.cell_number,
		"farm": row.get(_farm_column()),
	}
	frappe.response["message"] = out
	return out


@frappe.whitelist()
def record_work_accident():
	"""Record a Work Accident from the dashboard, optionally sending the
	employee to the clinic with a ticket."""
	assert_health_report_access("accidents")
	data = frappe.request.get_json() or {}
	employee = data.get("employee")
	if not employee or not frappe.db.exists("Employee", employee):
		frappe.throw(_("Pick the employee who was hurt."))
	doc = frappe.get_doc(
		{
			"doctype": "Work Accident",
			"employee": employee,
			"accident_date": data.get("accident_date") or frappe.utils.nowdate(),
			"accident_time": data.get("accident_time") or None,
			"location": data.get("location") or None,
			"accident_type": data.get("accident_type"),
			"severity": data.get("severity") or "Minor",
			"body_part": data.get("body_part") or "",
			"description": (data.get("description") or "").strip(),
			"witness": data.get("witness") or None,
			"first_aid_given": 1 if data.get("first_aid_given") else 0,
			"first_aid_details": data.get("first_aid_details") or None,
			"first_aider": data.get("first_aider") or None,
			"days_lost": int(data.get("days_lost") or 0),
		}
	).insert(ignore_permissions=True)
	out = {
		"name": doc.name,
		"employee_name": doc.employee_name,
		"route": _desk_form_route("Work Accident", doc.name),
	}
	if data.get("send_to_clinic"):
		out["clinic_ticket"] = doc.send_to_clinic(ignore_permissions=True)
	frappe.response["message"] = out
	return out


# ─── actions from the dashboard ─────────────────────────────────────────────
# Opened by the viewer's section ticks in Cova Clinic Settings, not by roles:
# whoever may open Clinic Tickets may issue one, whoever may open Test
# Scheduling may schedule. The documents are written without role checks for
# that reason.


LINK_DOCTYPES = ("Employee", "Department", "Designation")


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def dashboard_link_query(
	doctype: str, txt: str, searchfield: str, start: int, page_len: int, filters: dict | list | None
):
	"""Search behind the dashboard's Employee / Department / Designation link
	fields. Opened by the viewer's section ticks rather than read permission on
	those doctypes, so a dashboard viewer without an HR role can still search."""
	if not allowed_dashboard_sections():
		frappe.throw(_("You are not permitted to view the Clinic dashboard."), frappe.PermissionError)
	if doctype not in LINK_DOCTYPES:
		frappe.throw(_("Cannot search {0} here.").format(doctype))
	params = {"txt": f"%{txt or ''}%", "start": int(start or 0), "page_len": int(page_len or 20)}
	if doctype == "Employee":
		# {"is_first_aider": 1} narrows the list to first aiders, shown with
		# their farm and phone so the nearest one is easy to pick.
		if isinstance(filters, dict) and filters.get("is_first_aider"):
			farm = _farm_column()
			return frappe.db.sql(
				f"SELECT name, employee_name, CONCAT_WS(' · ', NULLIF({farm}, ''), NULLIF(cell_number, '')) "
				"FROM `tabEmployee` WHERE status = 'Active' AND is_first_aider = 1 AND (name LIKE %(txt)s "
				"OR employee_name LIKE %(txt)s OR employee_number LIKE %(txt)s) "
				"ORDER BY employee_name LIMIT %(start)s, %(page_len)s",
				params,
			)
		return frappe.db.sql(
			"SELECT name, employee_name, department "
			"FROM `tabEmployee` WHERE status = 'Active' AND (name LIKE %(txt)s OR employee_name LIKE %(txt)s "
			"OR employee_number LIKE %(txt)s) ORDER BY employee_name LIMIT %(start)s, %(page_len)s",
			params,
		)
	if doctype == "Department":
		return frappe.db.sql(
			"SELECT name, department_name FROM `tabDepartment` WHERE COALESCE(disabled, 0) = 0 "
			"AND (name LIKE %(txt)s OR department_name LIKE %(txt)s) ORDER BY name LIMIT %(start)s, %(page_len)s",
			params,
		)
	return frappe.db.sql(
		"SELECT name FROM `tabDesignation` WHERE name LIKE %(txt)s ORDER BY name LIMIT %(start)s, %(page_len)s",
		params,
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def first_aider_link_query(
	doctype: str, txt: str, searchfield: str, start: int, page_len: int, filters: dict | list | None
):
	"""The dashboard's Attended By picker: any active employee, searchable by
	name, ID or payroll number, with those marked First Aider listed first and
	labelled with their farm and phone. Its own query rather than a filtered
	Employee link: a filtered link makes Frappe load Employee's desk scripts,
	which a web page cannot run."""
	if not allowed_dashboard_sections():
		frappe.throw(_("You are not permitted to view the Clinic dashboard."), frappe.PermissionError)
	farm = _farm_column()
	return frappe.db.sql(
		f"SELECT name, employee_name, CONCAT_WS(' \u00b7 ', IF(is_first_aider = 1, 'First Aider', NULL), "
		f"NULLIF({farm}, ''), IF(is_first_aider = 1, NULLIF(cell_number, ''), NULLIF(department, ''))) "
		"FROM `tabEmployee` WHERE status = 'Active' AND (name LIKE %(txt)s OR employee_name LIKE %(txt)s "
		"OR employee_number LIKE %(txt)s) ORDER BY is_first_aider DESC, employee_name "
		"LIMIT %(start)s, %(page_len)s",
		{"txt": f"%{txt or ''}%", "start": int(start or 0), "page_len": int(page_len or 20)},
	)


@frappe.whitelist()
def request_medical_attention():
	"""Issue a Clinic Ticket from the dashboard: the employee is to be seen by
	the clinic on ``ticket_date`` at ``appointment_time``."""
	assert_health_report_access("tickets")
	data = frappe.request.get_json() or {}
	employee = data.get("employee")
	if not employee or not frappe.db.exists("Employee", {"name": employee, "status": "Active"}):
		frappe.throw(_("Pick an active employee."))
	if not (data.get("reason") or "").strip():
		frappe.throw(_("Say what the employee needs to be seen for."))
	urgency = data.get("urgency") or "Routine"
	if urgency not in ("Routine", "Urgent", "Emergency"):
		frappe.throw(_("Unknown urgency {0}.").format(urgency))

	ticket = frappe.get_doc(
		{
			"doctype": "Clinic Ticket",
			"employee": employee,
			"ticket_date": data.get("ticket_date") or frappe.utils.nowdate(),
			"valid_until": data.get("valid_until") or None,
			"appointment_time": data.get("appointment_time") or None,
			"time_issued": frappe.utils.nowtime(),
			"urgency": urgency,
			"reason": data["reason"].strip(),
		}
	).insert(ignore_permissions=True)
	out = {
		"name": ticket.name,
		"employee_name": ticket.employee_name,
		"ticket_date": str(ticket.ticket_date),
		"appointment_time": str(ticket.appointment_time or "")[:5],
		"route": _desk_form_route("Clinic Ticket", ticket.name),
	}
	if data.get("raise_gate_pass"):
		out.update(_raise_medical_gate_pass(ticket, data))
	frappe.response["message"] = out
	return out


def _raise_medical_gate_pass(ticket, data):
	"""A Medical Gate Pass for the ticket, raised in the same step. The pass
	still goes through its own approval workflow; if it cannot be raised (no
	supervisor set, already out on another pass, on leave…) the ticket stands
	and the reason comes back instead."""
	if not frappe.db.exists("DocType", "Gate Pass"):
		return {"gate_pass_error": _("Gate Pass is not installed on this site.")}
	frappe.db.savepoint("clinic_gate_pass")
	try:
		gp = frappe.get_doc(
			{
				"doctype": "Gate Pass",
				"employee": ticket.employee,
				"date": ticket.ticket_date,
				"pass_type": "Medical",
				"time_out": data.get("appointment_time") or frappe.utils.nowtime(),
				"returning_same_day": 1,
				"reason": ticket.reason,
				"clinic_ticket": ticket.name,
			}
		)
		gp.insert(ignore_permissions=True)
	except Exception as e:
		frappe.db.rollback(save_point="clinic_gate_pass")
		frappe.clear_messages()
		return {"gate_pass_error": frappe.utils.strip_html(str(e))}
	return {"gate_pass": gp.name, "gate_pass_route": _desk_form_route("Gate Pass", gp.name)}


@frappe.whitelist()
def mark_ticket_arrived():
	"""The clinic desk: the employee on ``ticket`` has arrived. Records their
	Clinic Checkin (an IN at ``arrived_at``, now by default), which flags the
	ticket as arrived and works out how long they took to get there."""
	assert_health_report_access("tickets")
	data = frappe.request.get_json() or {}
	name = data.get("ticket")
	ticket = (
		frappe.db.get_value("Clinic Ticket", name, ["name", "employee", "status"], as_dict=True)
		if name
		else None
	)
	if not ticket:
		frappe.throw(_("Clinic Ticket {0} not found.").format(name))
	if ticket.status != "Issued":
		frappe.throw(_("Clinic Ticket {0} is already {1}.").format(name, ticket.status))

	arrived_at = (
		frappe.utils.get_datetime(data.get("arrived_at"))
		if data.get("arrived_at")
		else frappe.utils.now_datetime()
	)
	checkin = frappe.get_doc(
		{"doctype": "Clinic Checkin", "employee": ticket.employee, "log_type": "IN", "time": arrived_at}
	)
	checkin.flags.clinic_ticket = ticket.name
	checkin.insert(ignore_permissions=True)

	out = frappe.db.get_value(
		"Clinic Ticket",
		name,
		["name", "employee_name", "status", "visited_at", "minutes_to_reach"],
		as_dict=True,
	)
	out["visited_at"] = str(out.visited_at)[:16] if out.visited_at else ""
	frappe.response["message"] = out
	return out


def _test_plans():
	"""Every Clinic Test Plan with its progress and next round, newest period first."""
	plans = frappe.get_all(
		"Clinic Test Plan",
		fields=[
			"name",
			"test_group",
			"test_package",
			"period_from",
			"period_to",
			"number_of_rounds",
			"total_employees",
			"employees_per_round",
			"scheduled_employees",
			"tested_employees",
			"remaining_employees",
			"progress",
			"status",
		],
		order_by="period_from desc",
		limit=50,
	)
	rounds = frappe.get_all(
		"Clinic Test Plan Round",
		filters={"parenttype": "Clinic Test Plan", "parent": ["in", [p.name for p in plans] or [""]]},
		fields=["parent", "round_no", "scheduled_from", "scheduled_to", "status", "skipped_on_leave"],
		order_by="round_no asc",
	)
	for p in plans:
		mine = [r for r in rounds if r.parent == p.name]
		nxt = next((r for r in mine if r.status == "Planned"), None)
		p.rounds_done = sum(1 for r in mine if r.status == "Scheduled")
		p.rounds = len(mine)
		p.on_leave_carried = sum(int(r.skipped_on_leave or 0) for r in mine)
		p.next_round = nxt.round_no if nxt else None
		p.next_window = f"{nxt.scheduled_from} \u2013 {nxt.scheduled_to}" if nxt else ""
		p.period = f"{p.period_from} \u2013 {p.period_to}"
		p.period_from = str(p.period_from)
		p.period_to = str(p.period_to)
		p.route = _desk_form_route("Clinic Test Plan", p.name)
	return plans


@frappe.whitelist()
def create_test_plan():
	"""New Clinic Test Plan from the dashboard, with its rounds laid out."""
	assert_health_report_access("schedules")
	data = frappe.request.get_json() or {}
	plan = frappe.get_doc(
		{
			"doctype": "Clinic Test Plan",
			"test_group": data.get("test_group"),
			"company": get_clinic_company(),
			"period_from": data.get("period_from"),
			"period_to": data.get("period_to"),
			"number_of_rounds": int(data.get("number_of_rounds") or 1),
			"send_to_cova": 1 if data.get("send_to_cova", True) else 0,
			"notes": data.get("notes") or None,
		}
	)
	plan.flags.ignore_permissions = True
	plan.insert()
	plan.generate_rounds()
	out = {
		"name": plan.name,
		"total_employees": plan.total_employees,
		"employees_per_round": plan.employees_per_round,
		"rounds": len(plan.rounds),
		"route": _desk_form_route("Clinic Test Plan", plan.name),
	}
	frappe.response["message"] = out
	return out


@frappe.whitelist()
def schedule_plan_round():
	"""Schedule the next round of a Clinic Test Plan from the dashboard."""
	assert_health_report_access("schedules")
	data = frappe.request.get_json() or {}
	plan = frappe.get_doc("Clinic Test Plan", data.get("plan"))
	plan.flags.ignore_permissions = True
	out = plan.schedule_next_round()
	out["route"] = _desk_form_route("Clinic Test Schedule", out["schedule"])
	frappe.response["message"] = out
	return out


def _dashboard_schedule(data):
	"""An unsaved Clinic Test Schedule from the dashboard's Schedule Tests form."""
	from cova_clinic_integration.cova_clinic_integration.doctype.clinic_test_schedule.clinic_test_schedule import (
		test_group_filters,
	)

	group_name = data.get("test_group")
	if not group_name:
		frappe.throw(_("Pick a test group."))
	group = test_group_filters(group_name)
	if not (data.get("scheduled_from") and data.get("scheduled_to")):
		frappe.throw(_("Pick the dates the tests are to be done."))

	doc = frappe.new_doc("Clinic Test Schedule")
	doc.update(
		{
			"title": f"{str(data['scheduled_from'])[:4]} {group_name}",
			"test_group": group_name,
			"test_package": group["test_package"],
			"company": get_clinic_company(),
			"scheduled_from": data["scheduled_from"],
			"scheduled_to": data["scheduled_to"],
			"employees_per_designation": int(
				data.get("employees_per_designation") or group["employees_per_designation"] or 0
			),
			"skip_already_scheduled": 1,
			"skip_employees_on_leave": 1,
			"send_to_cova": 1 if data.get("send_to_cova", True) else 0,
			"notes": data.get("notes") or None,
		}
	)
	for dept in group["departments"]:
		doc.append("departments", {"department": dept})
	for desig in group["designations"]:
		doc.append("designations", {"designation": desig})
	doc.flags.ignore_permissions = True
	return doc


@frappe.whitelist()
def preview_test_schedule():
	"""Who the Schedule Tests form would pick up, before anything is created."""
	assert_health_report_access("schedules")
	doc = _dashboard_schedule(frappe.request.get_json() or {})
	if not doc.company:
		frappe.throw(_("Set the Company in Cova Clinic Settings first."))
	employees = doc.matching_employees()
	by_designation = {}
	for e in employees:
		by_designation[e.designation or "Unassigned"] = (
			by_designation.get(e.designation or "Unassigned", 0) + 1
		)
	out = {
		"count": len(employees),
		"test_package": doc.test_package,
		"employees_per_designation": doc.employees_per_designation,
		"by_designation": [{"designation": k, "count": v} for k, v in sorted(by_designation.items())],
		# On leave during the window: not scheduled now, picked up next time.
		"on_leave": [{"employee": e.name, "employee_name": e.employee_name} for e in doc.on_leave],
		"employees": [
			{
				"employee": e.name,
				"employee_name": e.employee_name,
				"designation": e.designation,
				"department": e.department,
			}
			for e in employees[:100]
		],
	}
	frappe.response["message"] = out
	return out


@frappe.whitelist()
def schedule_tests():
	"""Schedule Tests from the dashboard: save a Clinic Test Schedule for the
	group, pull in who is due, raise their test requests and send them to Cova."""
	assert_health_report_access("schedules")
	doc = _dashboard_schedule(frappe.request.get_json() or {})
	doc.insert()
	doc.get_employees()
	if not doc.employees:
		out = {
			"schedule": doc.name,
			"route": _desk_form_route("Clinic Test Schedule", doc.name),
			"created": 0,
			"sent": 0,
			"failed": 0,
			"failures": [],
			"send_skipped": False,
			"employees": 0,
		}
	else:
		out = doc.create_test_requests()
		out.update(
			{
				"schedule": doc.name,
				"employees": len(doc.employees),
				"route": _desk_form_route("Clinic Test Schedule", doc.name),
			}
		)
	frappe.response["message"] = out
	return out


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

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

import frappe

# ─── helpers ──────────────────────────────────────────────────────────────


def build_headers():
	api_key = frappe.db.get_single_value("Cova Clinic Settings", "api_key")
	headers = {}
	if api_key:
		headers["X-Api-Key"] = api_key
	return headers


def normalize_phone(phone):
	"""Normalize a phone number to E.164 with the Kenya country code, as the
	Frappe Phone field requires a country code."""
	phone_clean = (phone or "").replace(" ", "")
	if phone_clean and not phone_clean.startswith("+"):
		if phone_clean.startswith("0"):
			phone_clean = "+254" + phone_clean[1:]
		elif phone_clean.startswith("254"):
			phone_clean = "+" + phone_clean
		else:
			phone_clean = "+254" + phone_clean
	return phone_clean


def create_or_update_cova_member(
	member_type, employee, national_id, full_name, payroll_number, phone, gender
):
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
		return existing

	doc = frappe.new_doc("Cova Members")
	doc.member_type = member_type
	doc.status = "Active"
	doc.employee = employee or ""
	doc.national_id = national_id or ""
	doc.full_name = full_name or ""
	doc.payroll_number = payroll_number or ""
	doc.phone_number = normalize_phone(phone) or ""
	doc.gender = gender or ""
	doc.insert(ignore_permissions=True)
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
		"payrollNumber": emp.employee_number,
		"fullName": emp.employee_name,
		"nationalId": emp.get("national_id") or "",
		"dateOfBirth": str(emp.date_of_birth) if emp.date_of_birth else "",
		"gender": emp.gender or "",
		"phone": emp.cell_number or "",
	}
	result = frappe.make_post_request(base_url + register_endpoint, headers=headers, json=payload)
	if result.get("covaMemberId"):
		try:
			frappe.db.set_value("Employee", emp.name, "cova_member_id", result.get("covaMemberId"))
		except Exception:
			pass
	cm_name = create_or_update_cova_member(
		"Active", emp.name, "", emp.employee_name, emp.employee_number, emp.cell_number or "", emp.gender or ""
	)
	save_cova_response(cm_name, result)
	return result


def deactivate_one(base_url, deactivation_endpoint, headers, emp):
	payload = {
		"payrollNumber": emp.employee_number,
		"covaMemberId": emp.get("cova_member_id") or "",
		"exitDate": str(emp.relieving_date) if emp.get("relieving_date") else "",
		"requiresExitMedical": False,
	}
	result = frappe.make_post_request(
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
				"payrollNumber": emp.employee_number,
				"fullName": emp.employee_name,
				"nationalId": emp.get("national_id") or "",
				"dateOfBirth": str(emp.date_of_birth) if emp.date_of_birth else "",
				"gender": emp.gender or "",
				"phone": emp.cell_number or "",
			}
		else:
			payload = {
				"schemeType": "PreEmployment",
				"nationalId": data.get("nationa_id"),
				"fullName": data.get("full_name"),
				"dateOfBirth": data.get("date_of_birth"),
				"gender": data.get("gender"),
				"phone": data.get("phone_number"),
			}

		result = frappe.make_post_request(base_url + register_endpoint, headers=headers, json=payload)

		if member_type == "Active" and result.get("covaMemberId"):
			try:
				frappe.db.set_value("Employee", employee, "cova_member_id", result.get("covaMemberId"))
			except Exception:
				pass

		# Create/refresh the Cova Member and store COVA's registration response on it.
		if member_type == "Active":
			cm_name = create_or_update_cova_member(
				"Active", employee, "", emp.employee_name, emp.employee_number, emp.cell_number or "", emp.gender or ""
			)
		else:
			cm_name = create_or_update_cova_member(
				"Pre Employment", "", data.get("nationa_id") or "", data.get("full_name") or "", "", data.get("phone_number") or "", data.get("gender") or ""
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
			"payrollNumber": emp.employee_number,
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
			req_doc.payroll_number = emp.employee_number
			req_doc.status = "Pending"
			req_doc.test_package = "Exit Medical"
			req_doc.scheduled_from = frappe.utils.nowdate()
			req_doc.scheduled_to = frappe.utils.add_days(frappe.utils.nowdate(), 15)
			req_doc.notes = "Auto-created on Cova deactivation"
			req_doc.insert(ignore_permissions=True)
			exit_request = req_doc.name

		result = frappe.make_post_request(base_url + deactivation_endpoint, headers=headers, json=payload)

		# Update Cova Member to Inactive
		cm_name = frappe.db.get_value("Cova Members", {"employee": employee}, "name")
		if cm_name:
			frappe.db.set_value("Cova Members", cm_name, "status", "Inactive")

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
				"phone": doc.phone_number or "",
			}

		result = frappe.make_post_request(base_url + test_request_endpoint, headers=headers, json=payload)

		# Status stays Pending until the test result webhook comes back from Cova
		resp = result

	# ─── REGISTER PRE-EMPLOYMENT CANDIDATE (Job Applicant) ────────────
	elif action == "register_preemployment_candidate":
		base_url = frappe.db.get_single_value("Cova Clinic Settings", "base_url")
		register_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "register_endpoint")
		test_request_endpoint = frappe.db.get_single_value("Cova Clinic Settings", "test_request_endpoint")
		headers = build_headers()

		applicant_name = data.get("applicant")
		applicant = frappe.get_doc("Job Applicant", applicant_name)

		national_id = applicant.get("custom_national_id") or ""
		full_name = applicant.get("applicant_name") or ""
		date_of_birth = str(applicant.get("custom_date_of_birth")) if applicant.get("custom_date_of_birth") else ""
		gender = applicant.get("custom_gender") or ""
		phone = applicant.get("phone_number") or ""

		# Normalize phone to E.164 with Kenya country code (Phone field requires a country code)
		phone = normalize_phone(phone)

		if applicant.get("custom_cova_tested"):
			resp = {"error": "Candidate already tested. Pre-employment is locked to one test."}
		elif not national_id:
			resp = {"error": "National ID is required for pre-employment registration."}
		else:
			# Step 1: create the tracking Clinic Test Request FIRST (persists even if Cova calls fail)
			req_doc = frappe.new_doc("Clinic Test Request")
			req_doc.member_type = "Pre Employment"
			req_doc.nationa_id = national_id
			req_doc.full_name = full_name
			req_doc.date_of_birth = applicant.get("custom_date_of_birth")
			req_doc.gender = gender
			req_doc.phone_number = phone
			req_doc.status = "Pending"
			req_doc.test_package = "Pre Employment Wellness"
			req_doc.scheduled_from = frappe.utils.nowdate()
			req_doc.scheduled_to = frappe.utils.add_days(frappe.utils.nowdate(), 15)
			req_doc.notes = "Pre-employment wellness for Job Applicant %s" % applicant_name
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
			register_result = frappe.make_post_request(base_url + register_endpoint, headers=headers, json=register_payload)

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
			test_result = frappe.make_post_request(base_url + test_request_endpoint, headers=headers, json=test_payload)

			# Step 4: create Cova Member and store COVA's registration response on it
			cm_name = create_or_update_cova_member("Pre Employment", "", national_id, full_name, "", phone, gender)
			save_cova_response(cm_name, register_result)

			# Step 5: mark applicant registered
			frappe.db.set_value("Job Applicant", applicant_name, "custom_cova_registered", 1)

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

		already_exists = frappe.db.exists("Clinic Visit", {"payroll_number": payroll_number, "visit_date": visit_date})
		employee = frappe.db.get_value("Employee", {"employee_number": payroll_number}, "name")

		if already_exists:
			resp = {"status": "duplicate"}
		elif not employee:
			resp = {"error": "Employee not found for payroll %s" % payroll_number}
		else:
			doc = frappe.new_doc("Clinic Visit")
			doc.employee = employee
			doc.payroll_number = payroll_number
			doc.visit_date = visit_date
			doc.visit_datetime = visit_date
			# v2: benefitBalanceAfter is now an OBJECT keyed by benefit category
			# (was a single number). Capture each category; a missing key = zero.
			b_consult = b_pharm = b_lab = b_diag = b_spec = 0
			b_total = 0
			if isinstance(bba, dict):
				b_consult = bba.get("Consultation") or 0
				b_pharm = bba.get("Pharmacy") or 0
				b_lab = bba.get("Laboratory") or 0
				b_diag = bba.get("Diagnostic") or 0
				b_spec = bba.get("Specialist") or 0
				for bv in [b_consult, b_pharm, b_lab, b_diag, b_spec]:
					b_total = b_total + (bv or 0)
			else:
				b_total = bba or 0
			doc.benefit_consultation = b_consult
			doc.benefit_pharmacy = b_pharm
			doc.benefit_laboratory = b_lab
			doc.benefit_diagnostic = b_diag
			doc.benefit_specialist = b_spec
			doc.benefit_balance_after = b_total
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
				frappe.db.set_value("Cova Members", cm_name, "last_visit", visit_date)
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
				"Clinic Visit", {"payroll_number": req.payroll_number}, "name", order_by="visit_date desc"
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

			# If this is a pre-employment candidate, lock the Job Applicant (one test only)
			if req.member_type == "Pre Employment" and req.get("nationa_id"):
				applicant_name = frappe.db.get_value("Job Applicant", {"custom_national_id": req.nationa_id}, "name")
				if applicant_name:
					frappe.db.set_value("Job Applicant", applicant_name, "custom_cova_tested", 1)
					frappe.db.set_value("Job Applicant", applicant_name, "custom_linked_test_result", result_doc.name)

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
					req_doc.payroll_number = emp.get("employee_number")
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
							"memberIdentifier": emp.get("employee_number"),
							"memberType": "Active",
							"testPackage": package.replace(" ", ""),
							"scheduledWindow": {"from": today, "to": window_end},
							"notes": req_doc.notes,
						}
						try:
							frappe.make_post_request(base_url + test_request_endpoint, headers=headers, json=payload)
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
	month matrix plus a multi-year monthly trend and filter option lists."""
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
		cnt = r.get("cnt") or 0
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
		cnt = tr.get("cnt") or 0
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

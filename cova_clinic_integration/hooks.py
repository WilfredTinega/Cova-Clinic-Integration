app_name = "cova_clinic_integration"
app_title = "Cova Clinic Integration"
app_publisher = "Upande LTD"
app_description = "Cova Clinic Integration"
app_email = "wilfred@upande.com"
app_license = "mit"

# Send non-GET requests for this app's endpoints as native `application/json`
# bodies instead of form-encoded, per-key JSON-stringified values.
use_json_request_body = True

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "cova_clinic_integration",
# 		"logo": "/assets/cova_clinic_integration/logo.png",
# 		"title": "Cova Clinic Integration",
# 		"route": "/cova_clinic_integration",
# 		"has_permission": "cova_clinic_integration.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/cova_clinic_integration/css/cova_clinic_integration.css"
# app_include_js = "/assets/cova_clinic_integration/js/cova_clinic_integration.js"

# include js, css files in header of web template
# web_include_css = "/assets/cova_clinic_integration/css/cova_clinic_integration.css"
# web_include_js = "/assets/cova_clinic_integration/js/cova_clinic_integration.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "cova_clinic_integration/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "cova_clinic_integration/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "cova_clinic_integration.utils.jinja_methods",
# 	"filters": "cova_clinic_integration.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "cova_clinic_integration.install.before_install"
# after_install = "cova_clinic_integration.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "cova_clinic_integration.uninstall.before_uninstall"
# after_uninstall = "cova_clinic_integration.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "cova_clinic_integration.utils.before_app_install"
# after_app_install = "cova_clinic_integration.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "cova_clinic_integration.utils.before_app_uninstall"
# after_app_uninstall = "cova_clinic_integration.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "cova_clinic_integration.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "cova_clinic_integration.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"cova_clinic_integration.tasks.all"
# 	],
# 	"daily": [
# 		"cova_clinic_integration.tasks.daily"
# 	],
# 	"hourly": [
# 		"cova_clinic_integration.tasks.hourly"
# 	],
# 	"weekly": [
# 		"cova_clinic_integration.tasks.weekly"
# 	],
# 	"monthly": [
# 		"cova_clinic_integration.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "cova_clinic_integration.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "cova_clinic_integration.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "cova_clinic_integration.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "cova_clinic_integration.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["cova_clinic_integration.utils.before_request"]
# after_request = ["cova_clinic_integration.utils.after_request"]

# Job Events
# ----------
# before_job = ["cova_clinic_integration.utils.before_job"]
# after_job = ["cova_clinic_integration.utils.after_job"]

# after_file_upload = ["cova_clinic_integration.utils.after_file_upload"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"cova_clinic_integration.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# Require all whitelisted methods to have type annotations
require_type_annotated_api_methods = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []


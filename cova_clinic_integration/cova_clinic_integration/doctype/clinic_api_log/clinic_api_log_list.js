frappe.listview_settings["Clinic API Log"] = {
	get_indicator(doc) {
		const color = { Success: "green", Warning: "orange", Error: "red" }[doc.status];
		return [__(doc.status), color, "status,=," + doc.status];
	},
};

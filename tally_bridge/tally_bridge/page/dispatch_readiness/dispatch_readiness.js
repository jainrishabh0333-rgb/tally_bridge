// Desk page for the dispatch-readiness report.
//
// The report is a self-contained page with its own stylesheet and behaviour,
// written to be opened straight from disk. Injecting it into the desk would
// put class names like .row, .card and .note next to Bootstrap's, so it goes
// into a frame instead and the two never see each other's CSS.
//
// srcdoc rather than a URL: no route has to serve raw HTML, and the frame
// carries `sandbox="allow-scripts"` WITHOUT allow-same-origin, so the report
// runs its own JavaScript from an opaque origin with no access to the desk's
// session, cookies or storage. The report needs none of that.

frappe.pages["dispatch-readiness"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Dispatch Readiness"),
		single_column: true,
	});

	const $body = $(page.body).css("padding", "0");
	const $status = $('<div style="padding:12px 15px;font-size:13px"></div>')
		.appendTo($body);
	const $frame = $(
		'<iframe sandbox="allow-scripts allow-popups allow-modals"' +
		' title="Dispatch readiness report"' +
		' style="width:100%;border:0;display:none"></iframe>'
	).appendTo($body);

	// The report scrolls internally, so the frame is sized to the window
	// rather than to its content — a frame cannot be measured across an
	// opaque origin anyway.
	function fit() {
		const top = $frame.offset() ? $frame.offset().top : 0;
		$frame.css("height", Math.max(420, $(window).height() - top - 24) + "px");
	}
	$(window).on("resize", frappe.utils.debounce(fit, 120));

	const picker = page.add_field({
		fieldname: "snapshot",
		label: __("Report date"),
		fieldtype: "Select",
		options: [],
		change: () => load(picker.get_value()),
	});

	page.set_secondary_action(__("Refresh"), () => {
		list();
		load(picker.get_value());
	});

	function load(name) {
		$status.html('<span class="text-muted">' + __("Loading…") + "</span>");
		$frame.hide();
		frappe.call({
			method: "tally_bridge.dispatch_readiness.get_snapshot",
			args: { name: name || "" },
			callback: (r) => {
				const s = r.message;
				if (!s) {
					$status.html(
						'<span class="text-muted">' +
						__("No report has been uploaded yet. Run the sync agent with --html and push it.") +
						"</span>"
					);
					return;
				}
				$status.html(
					'<span class="text-muted">' +
					__("As at") + " <b>" + frappe.datetime.str_to_user(s.as_of) + "</b> · " +
					frappe.utils.escape_html(String(s.order_count || 0)) + " " + __("pending orders") +
					" · " + frappe.utils.escape_html(String(s.coverage_pct || 0)) + "% " + __("dispatchable") +
					(s.generated_on
						? " · " + __("generated") + " " + frappe.datetime.str_to_user(s.generated_on)
						: "") +
					"</span>"
				);
				$frame.attr("srcdoc", s.page_html || "").show();
				fit();
			},
		});
	}

	// Populating the picker fires its change handler, which is what loads the
	// report — so this is the only entry point.
	function list() {
		frappe.call({
			method: "tally_bridge.dispatch_readiness.snapshots",
			callback: (r) => {
				const rows = r.message || [];
				picker.df.options = rows.map((x) => x.name);
				picker.refresh();
				if (rows.length) picker.set_value(rows[0].name);
				else load(null);
			},
		});
	}

	list();
};

"""Part 10 evidence: EXPLAIN / timing of the Monthly Value query, before and after the index.

Builds a throwaway copy of `tabReno Order` with N synthetic rows (default 100,000), runs
the report query without and with `monthly_value_idx`, prints EXPLAIN + ANALYZE + timings,
and drops the copy. The site's real data is never touched.

	cd ~/16bench/sites
	../env/bin/python ../apps/reno_order/scripts/explain_monthly_value.py reno.localhost [rows]
"""

import statistics
import sys
import time

import frappe

TABLE = "_explain_reno_order"
INDEX_COLUMNS = "transaction_date, docstatus, status, grand_total"
STATUSES = (
	"Draft",
	"Confirmed",
	"In Production",
	"Ready for Installation",
	"Installed",
	"Closed",
	"Cancelled",
)
QUERY = f"""
	select date_format(transaction_date, '%%Y-%%m') as month, status, count(*) as orders, sum(grand_total) as value
	from `{TABLE}`
	where transaction_date between %(from_date)s and %(to_date)s and docstatus < 2
	group by month, status
	order by month, status
"""


def main(site: str, rows: int):
	frappe.init(site, sites_path=".")
	frappe.connect()
	db = frappe.db
	try:
		build_table(db, rows)
		params = {
			"from_date": frappe.utils.get_first_day(frappe.utils.add_months(frappe.utils.today(), -11)),
			"to_date": frappe.utils.get_last_day(frappe.utils.today()),
		}
		print(f"# {rows:,} rows, MariaDB {db.sql('select version()')[0][0]}\n")
		report(db, "Before: no index on transaction_date", params)
		db.sql(f"alter table `{TABLE}` add index monthly_value_idx ({INDEX_COLUMNS})")
		db.sql(f"analyze table `{TABLE}`")
		report(db, f"After: monthly_value_idx ({INDEX_COLUMNS})", params)
	finally:
		db.sql(f"drop table if exists `{TABLE}`")
		frappe.destroy()


def build_table(db, rows):
	db.sql(f"drop table if exists `{TABLE}`")
	db.sql(f"create table `{TABLE}` like `tabReno Order`")
	# The copy inherits every index; start from "before": drop the report index if present.
	indexes = {r[2] for r in db.sql(f"show index from `{TABLE}`")}
	if "monthly_value_idx" in indexes:
		db.sql(f"alter table `{TABLE}` drop index monthly_value_idx")

	status_case = " ".join(f"when {i} then '{s}'" for i, s in enumerate(STATUSES))
	# 3 years of orders; roughly a third fall inside the report window, like a real history.
	db.sql(
		f"""
		insert into `{TABLE}` (name, creation, modified, owner, modified_by, docstatus, customer,
			company, transaction_date, expected_installation_date, order_type, status, grand_total,
			total_amount, naming_series)
		select concat('XRO-', lpad(seq, 7, '0')), now(), now(), 'Administrator', 'Administrator',
			case seq % 7 when 0 then 0 when 6 then 2 else 1 end,
			concat('Customer ', seq % 5000), 'Reno Kitchens Pvt Ltd',
			date_sub(curdate(), interval (seq % 1095) day),
			date_add(date_sub(curdate(), interval (seq % 1095) day), interval 21 day),
			'Standard', case seq % 7 {status_case} end,
			round(20000 + (seq * 7919) % 480000, 2), round(20000 + (seq * 7919) % 480000, 2), 'RO-.#####'
		from seq_1_to_{int(rows)}
		"""
	)
	db.sql(f"analyze table `{TABLE}`")
	db.commit()


def report(db, title, params):
	print(f"## {title}\n")
	print("EXPLAIN:")
	for row in db.sql("explain " + QUERY, params, as_dict=True):
		print(
			f"  type={row.type} key={row.key} rows={row.rows} "
			f"filtered={row.get('filtered')} Extra={row.Extra}"
		)
	analyzed = db.sql("analyze " + QUERY, params, as_dict=True)
	for row in analyzed:
		print(
			f"ANALYZE: r_rows={row.r_rows} r_filtered={row.r_filtered} r_total_time_ms={row.get('r_total_time_ms')}"
		)

	timings = []
	for _ in range(7):
		start = time.perf_counter()
		db.sql(QUERY, params)
		timings.append((time.perf_counter() - start) * 1000)
	print(f"median of 7 runs: {statistics.median(timings):.1f} ms\n")


if __name__ == "__main__":
	main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 100_000)

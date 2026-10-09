# DECISIONS.md

I use a unique index on `employees.emp_code` so an employee code cannot be created twice. A unique compound index on `attendance_logs.emp_code` and `date` is the final protection against duplicate daily attendance records, including simultaneous requests. The date-first attendance index supports the listing sort, while employee/date and department indexes support common filters and lookups. I would review index usage with production-shaped data before adding more indexes, because every extra index increases write cost.

For simultaneous punch-ins, both requests may initially find no record, but the unique compound index allows only one insert to succeed. The other request receives MongoDB's duplicate-key error, which the API translates to HTTP 409. This relies on the index being present; an application-level pre-check alone would not be sufficient.

The leaderboard sorts by late minutes descending, then employee code ascending, and assigns competition ranks so ties share a rank. The department summary starts from employees and looks up their logs, rather than starting from attendance, so employees with no logs still contribute to headcount.

At 100x the data volume, I would measure the actual query plans and latency first, then consider bounded date partitions or pre-aggregated analytics if the measured workload justified the extra complexity.

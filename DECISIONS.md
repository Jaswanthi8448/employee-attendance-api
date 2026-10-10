# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, race-safe create), `(department, emp_code)` (filtered list in
   sort order, department lookups), and `joined_on` (department summary without a department filter).
   `attendance_logs`: unique `(emp_code, date)` (one record per day, race-safe punch-in, PATCH lookup, monthly and trend
   queries) and `(date desc, emp_code)` (the list's exact sort, and month ranges for the leaderboard). I rejected an
   index on `status`: with only five values it is not selective, so the date index plus a filter is cheaper.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. There is no prior
   `find`. The unique index lets exactly one insert succeed (201). The other gets `DuplicateKeyError`, which I return
   as 409.

3. **Ties.** `$setWindowFields` with `$rank` gives competition ranks (1, 2, 2, 4). I filter on `rank <= limit`, not on
   row count, so every employee tied at the cutoff is returned. Ties are ordered by `emp_code`.

4. **Headcount.** The summary pipeline starts from `employees` filtered by `joined_on <= month end`, then uses
   `$lookup` for each employee's logs with `preserveNullAndEmptyArrays`. Employees with zero logs still reach the
   `$group`.

5. **At 100x.** I would precompute daily per-department rollups when records are written, so the summary and trend
   read about 30 small documents instead of scanning raw logs.

# DECISIONS.md

1. **Indexes.** Unique `employees.emp_code`; unique `attendance_logs(emp_code, date)` (the punch-in race guard and monthly scans); `(date desc, emp_code)` for the list sort and month ranges; `(status, date desc, emp_code)` for status filters; `(emp_code, punch_in desc)` for punch-out; `employees(department, joined_on)` and `(joined_on)` for headcount. I rejected an index on `late_minutes`: the leaderboard must sum every record of the month anyway.
2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets one insert succeed (201); the other raises `DuplicateKeyError`, which I turn into 409. No read happens first.
3. **Ties.** `$rank` gives ties the same rank and skips the next one, and `limit` filters on `rank <= limit`, so everyone tied at the cutoff is returned, even if that exceeds `limit`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and left-joins logs, so someone with no logs still counts one.
5. **At 100x the data** I would pre-aggregate daily per-employee totals into a summary collection and run analytics on that instead of raw logs.

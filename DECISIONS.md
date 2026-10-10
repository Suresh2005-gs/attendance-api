# DECISIONS.md

1. **Indexes.** Unique `emp_code` on employees. Unique `(emp_code, date)` on attendance, which blocks double punch-ins and serves monthly lookups. `(date desc, emp_code)` gives the list its sort and the month scans. `(status, date desc, emp_code)` serves status filters, and `(emp_code, punch_in desc)` finds the record to punch out. `(department, joined_on)` and `(joined_on)` serve headcount. I rejected a `late_minutes` index: the leaderboard adds up every record of the month anyway.

2. **Punch-in race.** Both requests work out the same employee and date and call `insert_one` straight away, with no check first. The unique index lets one insert through. The other gets a duplicate-key error and I return 409.

3. **Ties.** I rank with `$rank`, so tied people share a rank and the next one is skipped. I then keep everyone with `rank <= limit`, so both people tied at the cutoff are returned.

4. **Headcount.** The summary starts from employees who joined by month end and then looks up their logs. Someone with no logs still has a row, so they are counted.

5. **At 100x the data**, I would keep daily totals per employee in a summary collection, updated on punch and correction, and run analytics on that.
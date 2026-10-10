# DECISIONS.md

1. **Indexes.** I made `emp_code` unique on employees, and `(emp_code, date)` unique on attendance, which stops double punch-ins and speeds up monthly lookups. `(date desc, emp_code)` gives the list its sort order and serves the month scans. `(status, date desc, emp_code)` handles status filters, `(emp_code, punch_in desc)` finds the record to punch out, and `(department, joined_on)` plus `(joined_on)` serve headcount. I rejected an index on `late_minutes`, because the leaderboard has to add up every record of the month anyway.

2. **Punch-in race.** Both requests work out the same employee and date and call `insert_one` straight away, with no check first. The unique index lets only one insert through. The other fails with a duplicate-key error and I return 409.

3. **Ties.** I rank with `$rank`, so tied people share a rank and the next rank is skipped. Then I keep everyone with `rank <= limit`. If two people tie at the cutoff, both are returned, so the list can be longer than `limit`.

4. **Headcount.** The department summary starts from the employees collection, keeps those who joined by the end of the month, and then looks up their logs. Someone with no logs still has one row, so they are still counted.

5. **At 100x the data**, I would store daily totals per employee in a separate summary collection, updated on punch and correction, and run the analytics on that.

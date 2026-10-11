# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date)` enforces one record per employee per day and serves per-employee ranges and the `$lookup`s. `(date desc, emp_code)` serves the sorted list and the leaderboard's month scan. `(status, date desc, emp_code)` serves the status filter. On employees: unique `emp_code`, and `(department, emp_code)`. I rejected a covering index on `late_minutes` for the leaderboard: one month of dates is already cheap and it would slow every write.
2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique index lets one insert win (201); the other gets `DuplicateKeyError`, which I turn into 409. There is no read-then-write step.
3. **Ties.** `$rank` gives competition ranks (1,2,2,4) and I filter `rank <= limit` after ranking. Every tied employee at the cutoff is returned, so there can be more than `limit` rows, ordered by `emp_code`.
4. **Headcount.** The pipeline starts from `employees`, `$lookup`s each person's logs, and keeps people with none (`preserveNullAndEmptyArrays`). Headcount counts `joined_on <= month end`.
5. **100x data.** Maintain pre-aggregated monthly rollups per employee, updated on punch and correction, so analytics stop scanning raw logs.

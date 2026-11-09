# Limitations and next steps

## What works
- Schema with all 10 required entities plus conflict, assignment-bridge and event tables; foreign keys and enum/range CHECKs enforced by SQLite.
- Deterministic generator at two scales (full: 10,000 submissions, 1,000 flags, 300 cases).
- Guarded workflow: no automatic guilt, conflict-of-interest guard, mandatory rationale, appeal window, independent appeal review, closure rules, append-only history.
- Four labelled negative scenarios, kept in a separate ground-truth file and all detected (no misses, no false alarms).
- 101 operational checks (keys, IDs, enums, formats, date order, lifecycle, status replay, reconciliation), metrics, and a double-generation reproducibility check.
- 23 automated tests, an end-to-end demo script and SQL analysis queries.

## Known limitations
- **Simulated people.** Reviewer decisions and appeal outcomes come from a simple scoring rule plus noise, not real reviewer behaviour. The metrics describe the simulation, not a real institution.
- **Case selection skews to the priority band.** Taking the top 300 of 1,000 flags means every case at full scale is priority band. Low and standard flags are counted but never reviewed. A real policy might sample from every band.
- **One report per submission.** Re-runs of the similarity check (`-02` reports) are not modelled.
- **No user interface.** The brief marks a dashboard as optional. Inspection is through the demo script, the SQL queries and the SQLite file (any SQLite viewer works).
- **No authentication or roles.** Anyone with the database file can write to it. The guards live in the Python workflow layer, so raw SQL can bypass them. That is exactly how the negative scenarios are planted, and validation exists to catch such writes.
- **Single appeal round in the generated data.** The schema allows several rounds, but the generator never appeals a revised decision.
- **SQLite only.** Moving to PostgreSQL would need small dialect changes (`GLOB`, `julianday`).

## Next steps
1. Streamlit dashboard reading the SQLite database: case queue with filters (status, band, assessment, reviewer, appeal outcome), case detail with evidence timeline and audit history.
2. Stratified case selection (a quota per band) so low and standard flags are also reviewed, which measures false positives across the full score range.
3. Database triggers that reject writes the workflow would refuse (for example a decision with no assigned reviewer), as a second line of defence.
4. Re-run reports, multiple appeal rounds and reviewer unavailability or reassignment.
5. SLA tracking (decision deadline per priority) and reporting on overdue cases.

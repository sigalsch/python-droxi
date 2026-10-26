# CHANGES

Rationale and trade-offs for grouping `PatientRequest` objects by **patient + department**. Organized by topic.

| Section | What it answers |
|---|---|
| [Goal](#goal) | What changed, where it lives |
| [Algorithm](#algorithm) | How we group (chose Option A; B and C rejected) |
| [Schema](#schema) | Whether to add tables/fields (chose none) |
| [Correctness](#correctness) | Department moves, leftover-close, assumptions |
| [Tests](#tests) | What is covered, what is not, fixture isolation |
| [Observability](#observability) | Logs, correlation ids, PHI rule |
| [Deferred](#deferred) | Perf, transactions, idempotency, TinyDB indexes |

---

## Goal

Extend ingestion so `PatientRequest` objects are grouped by both `patient_id` and `assigned_to` (`Dermatology`, `Radiology`, `Primary`), without changing the original per-patient service.

Implementation: `services/patient_department_request_service.py::DepartmentPatientRequestService`, wired in by `tests/test_clinic_manager_with_departments.py`.

---

## Algorithm

**Chose Option A** — flat composite key, rebuild-and-upsert per batch.

### Options

- **A (chosen)** — Group incoming tasks by `(patient_id, assigned_to)`, snapshot Open requests for affected patients, upsert per group, close leftovers.
- **B** — Deltas only: reverse-lookup the previous Open request via `task_ids` and migrate. More surgical, more round-trips and edge cases. `ClinicManager` already passes the full Open-task snapshot, so we do not pay that cost yet.
- **C** — Same as A with a nested `{patient_id: {department: [tasks]}}` shape (the stub's type aliases). Rejected on style: A's flat key is a smaller diff and reads in one pass. Easy to switch later if more grouping dimensions appear.

### Why A

- Smallest leap from `PerPatientRequestService`: grouping key `patient_id` → `(patient_id, assigned_to)`, plus one reconciliation step.
- Reuses `to_patient_request` as-is. In a department-scoped group every task shares `assigned_to`, so `newest_task.assigned_to` is unambiguous.
- Handles the new scenarios without special cases:
  - **Split** — patient2 in `Primary` and `Dermatology` → two groups, two Open requests.
  - **Move** — patient3's `task3` goes `Radiology → Primary` → no `(patient3, Radiology)` group → leftover Open request is closed.
  - **Emptied department** — same mechanism: no group for that key → leftover is closed.

Removed the stub aliases `DepartmentToPatientTasks` / `PatientToGroupedDepartmentTasks`. They only match Option C; keeping them next to A's flat key would be misleading.

### Invariant

`ClinicManager.process_tasks_update` calls `update_requests(open_tasks + newly_closed_tasks)` where `open_tasks` is **every** currently-Open task, not just this batch's deltas. The leftover-close step depends on that. If the contract becomes deltas-only, switch to Option B.

---

## Schema

README line 59 allows new models/tables. Evaluated three options and took **none**. The current `PatientRequest.assigned_to` already carries the department.

### Options not taken

**1. Sidecar `OpenRequestIndex`:** `(patient_id, assigned_to) → request_id` for Open rows.

- Buy: O(1) lookup vs the current `patient_id in [...] AND status == Open` scan.
- Why not: TinyDB workaround. Mongo/Postgres give the same thing with a compound index on `(patient_id, assigned_to, status)` and one collection. Dual writes on every upsert/close add a consistency failure mode we do not have today.

**2. Split `open_patient_requests` / `closed_patient_requests`.**

- Buy: Open table stays O(thousands) while closed history grows. Matches "millions of patients, thousands of open requests" + retain closed as medical history.
- Why not: an index on `(status, patient_id)` already partitions logically. Two physical tables complicate "fetch this request id regardless of status." Revisit only if a real DB with that index still shows Open latency growing with the archive.

**3. Dedicated `department` field** next to `assigned_to`.

- Buy: `assigned_to` looks like it meant "doctor" (README: thousands of doctors). A separate field would allow department + individual.
- Why not: values are already the department names, so the overload is unambiguous for this exercise. A model change without a scale/correctness win makes review harder. Revisit when a real assigned-doctor requirement lands.

None of these improve correctness. Option 1 adds a dual-write hazard. 1 and 2 work around a placeholder DB. Smallest correct diff wins; harder changes stay documented.

---

## Correctness

### Leftover-close and the partial-batch guard

Step 4 of `update_requests` closes any Open request whose `(patient_id, assigned_to)` is still in `existing_open_by_key` after upserts. That is only safe when the caller passed every Open task for every affected patient (today's `ClinicManager` contract).

A partial batch (only the tasks that changed for one department) would make the patient's *other* Open departments look like leftovers and wrongly close them.

Before closing, we load live `(patient_id, assigned_to)` keys from the `tasks` table (scoped to `affected_patients`) and skip any leftover that still has an Open task:

```python
if existing_open_by_key:
    live_open_task_keys = self._get_open_task_keys_for_patients(affected_patients)
    for key, stale in existing_open_by_key.items():
        if key in live_open_task_keys:
            continue
        db.patient_requests.update({'status': 'Closed'}, where('id') == stale.id)
```

Under `ClinicManager` this is a no-op: leftovers have no live Open task. Cost is one extra query, only when leftovers exist.

Why keep an unreachable check:

- Future callers (webhook per task, department-scoped job, deltas-only) would otherwise silently corrupt data.
- The signature `update_requests(tasks)` does not encode "must be a complete snapshot."
- Cost is bounded; we already pay a similar query in step 2.

This is not Option B. The guard refuses a wrong close; it does not migrate tasks between requests.

### Snapshots when a department empties

Two code paths, on purpose:

| Event | Path | Closed-row snapshot |
|---|---|---|
| Last Open task **moves** away | leftover-close (status flip only) | Previous Open snapshot kept (`task_ids` / messages as they were) |
| Last Open task is **Closed** in place | step 3 upserts the existing row to `Closed` | Rebuilt from the closed tasks in the group |

Keeping the move snapshot matches README medical history: "Radiology held these tasks when it emptied" is more useful than an empty historical record. Easy to revisit if a downstream reader wants scrubbed rows.

### Assumptions

- **`patient_id` does not change on a task.** `affected_patients` comes from the incoming batch. A rewritten patient id would leave the previous patient's Open request stranded. A different patient is a different task id. If that changes, include the previous `patient_id` from the tasks table before upsert.
- **At most one Open request per `(patient, dept)`.** A second Open row is silently dropped when building `existing_open_by_key`. We do not heal corruption from a bug or concurrent writer.
- **No dropped or out-of-order events** (README). Idempotency beyond task-id upsert is deferred.

---

## Tests

### Isolation

The suite used a process-wide file-backed TinyDB. `drop_tables()` did not reliably clear `SerializationMiddleware` + JSON caches between files, so the department test leaked `task7` / patient3 into `test_clinic_manager.py`.

`tests/conftest.py` now rebinds the module globals to a fresh `TinyDB(storage=MemoryStorage)` per test. Services already do `import db.db_tinydb as db`, so they pick up the new handle. Duplicated `init_db` fixtures were removed from both test files.

MemoryStorage over "delete the json file": hermetic, no fsync, already hinted in `db/db_tinydb.py`. `main.py` still writes `tinydb/db.json` for manual inspection. Trade-off: tests leave nothing on disk; use a `--file-db` opt-in if you need to peek.

### What we cover

Fixture `test_task_processing_with_department_support`: split, full empty via move, close-everything, new Open after close (count only).

Direct `PatientTask` tests in `tests/test_clinic_manager_with_departments.py`:

| Test | Locks in |
|---|---|
| `test_department_reassignment_closes_only_the_emptied_department` | Last task leaves Radiology → only Radiology closes; Dermatology keeps the same `id`; closed Radiology keeps `task_ids == {"t-b"}` |
| `test_partial_move_keeps_source_department_open` | Two Radiology tasks, one moves → Radiology stays Open with the remaining task |
| `test_partial_close_removes_closed_task_data_from_open_request` | One of two Open tasks Closed → request stays Open; closed task data dropped (README rule) |
| `test_move_merges_into_existing_destination_request` | Radiology's last task joins an already-Open Primary → Primary keeps its `id` |
| `test_new_open_task_does_not_reopen_closed_request` | Later Open task for the same key is a **new** request; Closed history keeps its `id` |

### What we do not test, and why

- **Last-task-close vs last-task-move snapshot shape** — both paths already run (fixtures vs leftover-close). Extra asserts would freeze scrub-vs-preserve and make a later change look like a regression. Documented above under Snapshots.
- **Two Open requests for the same key / duplicate `task.id` in one batch / Closed-only batch for a never-seen patient** — malformed or corrupt input, not the grouping requirement. We do not invent a healer or a history-synthesis policy.
- **Closed and reassigned in one update** — README treats those as separate events.
- **`created_date` jumping after the oldest Open task closes** — follows "remove closed-task data," not grouping.
- **`pharmacy_id` from the newest task** — original `to_patient_request`; we did not change aggregation.
- **`assigned_to` outside the three departments** — exercise lists those three; an unknown `str` is just another grouping key.
- **Empty `process_tasks_update`** — early return.

---

## Observability

Implemented in `observability.py`, wired into `ClinicManager` and both services. No new dependencies (`logging` + `contextvars` + `dataclasses`).

### INFO / WARNING (always on once configured)

- One **correlation id per batch** (`correlation_scope` + `_CorrelationIdFilter`). Services never pass the id.
- **`UpdateStats`** returned from `update_requests`: `requests_created`, `requests_updated`, `requests_closed_by_leftover_sweep`, `leftover_close_skipped_live_key`. One summary log per batch.
- Start + end INFO on `process_tasks_update`. Start is deliberate: a mid-batch crash still shows the id and inputs.

  ```
  INFO [a6b8a354cb86] clinic_manager: process_tasks_update started batch_size=5 affected_patients=3 service=DepartmentPatientRequestService
  INFO [a6b8a354cb86] clinic_manager: process_tasks_update completed duration_ms=0.2 batch_size=5 newly_closed_tasks=3 open_tasks_scanned=4 ... leftover_close_skipped_live_key=0
  ```

- **WARNING** if the partial-batch guard skips a close (unreachable under `ClinicManager`; nonzero = contract violation). Alert on `leftover_close_skipped_live_key > 0`.
- `configure_logging()` from `main.py` only. Tests stay silent.

No metrics backend yet. The same counters are the right shape for Prometheus/StatsD later, next to the summary log.

### DEBUG (off by default)

Flip on for an investigation:

```python
import logging
from observability import configure_logging
configure_logging(logging.DEBUG)
```

- `clinic_manager`: per-task `task_id` / `patient_id` / `department` / `status` on entry.
- Department service: groups after step 1; insert/update per upsert; `closed request <id>` on leftover-close.
- Per-patient service: insert/update per patient.

String-building is behind `log.isEnabledFor(logging.DEBUG)`.

### PHI rule

Every log line — INFO, WARNING, DEBUG — logs **ids and counts only**. Never `message`, `medications`, names, or free-form content. Rule of thumb: "is this an id or a count?" If no, don't log it. Written at the top of `observability.py`.

---

## Deferred

Not required for the exercise. Flagged so a reviewer sees the choice.

**Performance (`clinic_manager.py` Question).** `get_open_tasks()` returns every Open task in the DB each batch, including untouched patients, then re-upserts their requests. Fine at "thousands of open requests"; hurts at 10×. Fix: scope to affected `patient_id`s, same as `_get_open_requests_for_patients`. Touches the shared path; tests do not exercise that scale.

**Transactions (`task_service.py` Question).** TinyDB has no batch upsert and no transactions. One `process_tasks_update` does a per-row task upsert loop, then request insert/update, then leftover-close. A crash leaves half-updated state. Real DB: bulk upsert inside a session transaction. Fixing this on TinyDB would be a lie.

**Idempotency / replay.** Task upsert-by-id is a safe no-op. Request writes have no `updated_date` guard, so a stale replay can overwrite newer request state. README says no out-of-order events. Defense-in-depth later: `update` only if `existing.updated_date <= new_req.updated_date`.

**Compound index.** TinyDB `where('patient_id').one_of([...])` is a scan. Production should index `(patient_id, assigned_to, status)`. Cheap here because we only pull Open rows (thousands at most).

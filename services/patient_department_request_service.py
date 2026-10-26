import logging
from collections import defaultdict

from tinydb import where

import db.db_tinydb as db
from models.patient_request import PatientRequest
from models.patient_task import PatientTask
from observability import UpdateStats, format_kv
from .abstract_patient_request_service import PatientRequestService


log = logging.getLogger(__name__)


class DepartmentPatientRequestService(PatientRequestService):
    """Groups tasks into PatientRequests by (patient_id, assigned_to department).

    Each (patient, department) pair has at most one Open PatientRequest.
    When its last Open task leaves (closed or reassigned), the request is
    marked Closed. See CHANGES.md for the full rationale.
    """

    def update_requests(self, tasks: list[PatientTask]) -> UpdateStats:
        stats = UpdateStats()

        # 1. Group tasks by (patient_id, department) -- one group per PatientRequest.
        grouped_by_patient_and_dept: dict[tuple[str, str], list[PatientTask]] = defaultdict(list)
        for task in tasks:
            grouped_by_patient_and_dept[(task.patient_id, task.assigned_to)].append(task)

        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "grouped batch into %d (patient, department) groups: %s",
                len(grouped_by_patient_and_dept),
                "; ".join(
                    format_kv(patient_id=p, department=d, tasks=len(group_tasks))
                    for (p, d), group_tasks in grouped_by_patient_and_dept.items()
                ),
            )

        # 2. Snapshot Open PatientRequests for affected patients, keyed by
        #    (patient_id, department). We pop matches as we upsert; whatever
        #    remains has no Open tasks in this batch and gets closed in step 4.
        affected_patients = {patient_id for patient_id, _ in grouped_by_patient_and_dept}
        existing_open_by_key: dict[tuple[str, str], PatientRequest] = {
            (req.patient_id, req.assigned_to): req
            for req in self._get_open_requests_for_patients(affected_patients)
        }

        # 3. Upsert one PatientRequest per (patient, department) group.
        for (patient_id, department), group_tasks in grouped_by_patient_and_dept.items():
            new_req = self.to_patient_request(patient_id, group_tasks)
            existing = existing_open_by_key.pop(
                (new_req.patient_id, new_req.assigned_to), None)

            if existing is None:
                db.patient_requests.insert(new_req.model_dump())
                stats.requests_created += 1
                log.debug(
                    "inserted request %s",
                    format_kv(
                        request_id=new_req.id,
                        patient_id=patient_id,
                        department=department,
                        tasks=len(group_tasks),
                    ),
                )
            else:
                new_req.id = existing.id
                db.patient_requests.update(
                    new_req.model_dump(), where('id') == existing.id)
                stats.requests_updated += 1
                log.debug(
                    "updated request %s",
                    format_kv(
                        request_id=existing.id,
                        patient_id=patient_id,
                        department=department,
                        tasks=len(group_tasks),
                    ),
                )

        # 4. Close leftover Open requests (keys with no Open task in the batch).
        #    live-key check = partial-batch guard; see CHANGES.md.
        if existing_open_by_key:
            live_open_task_keys = self._get_open_task_keys_for_patients(
                affected_patients)
            for key, stale in existing_open_by_key.items():
                patient_id, department = key
                if key in live_open_task_keys:
                    # Partial batch: this (patient, dept) looked leftover but
                    # still has a live Open task not in this input. Refusing
                    # the close prevents silent data loss. Unreachable under
                    # the current ClinicManager contract; nonzero count = an
                    # upstream contract violation, worth alerting on.
                    log.warning(
                        "leftover-close skipped: live Open task still exists for key %s",
                        format_kv(
                            patient_id=patient_id,
                            department=department,
                            request_id=stale.id,
                        ),
                    )
                    stats.leftover_close_skipped_live_key += 1
                    continue
                db.patient_requests.update(
                    {'status': 'Closed'}, where('id') == stale.id)
                stats.requests_closed_by_leftover_sweep += 1
                log.debug(
                    "closed request %s -- no Open tasks remain for this key",
                    format_kv(
                        request_id=stale.id,
                        patient_id=patient_id,
                        department=department,
                    ),
                )

        return stats

    def _get_open_requests_for_patients(
            self, patient_ids: set[str]) -> list[PatientRequest]:
        if not patient_ids:
            return []

        matches = db.patient_requests.search(
            (where('status') == 'Open')
            & (where('patient_id').one_of(list(patient_ids))))
        return [PatientRequest(**doc) for doc in matches]

    def _get_open_task_keys_for_patients(
            self, patient_ids: set[str]) -> set[tuple[str, str]]:
        """Return every ``(patient_id, assigned_to)`` pair that still has at
        least one Open task in the DB for the given patients."""
        if not patient_ids:
            return set()

        matches = db.tasks.search(
            (where('status') == 'Open')
            & (where('patient_id').one_of(list(patient_ids))))
        return {(doc['patient_id'], doc['assigned_to']) for doc in matches}

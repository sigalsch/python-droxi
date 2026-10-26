import logging
from collections import defaultdict

from tinydb import where

import db.db_tinydb as db
from models.patient_task import PatientTask
from models.patient_request import PatientRequest
from observability import UpdateStats, format_kv
from .abstract_patient_request_service import PatientRequestService


log = logging.getLogger(__name__)


class PerPatientRequestService(PatientRequestService):

    def get_open_patient_request(self, patient_id) -> PatientRequest | None:
        result_dict = db.patient_requests.get(
            (where('patient_id') == patient_id) & (where('status') == 'Open'))

        if not result_dict:
            return None

        return PatientRequest(**result_dict)

    def update_requests(self, tasks: list[PatientTask]) -> UpdateStats:
        stats = UpdateStats()

        grouped_by_patient: dict[str, list[PatientTask]] = defaultdict(list)

        for task in tasks:
            grouped_by_patient[task.patient_id].append(task)

        for patient_id, patient_tasks in grouped_by_patient.items():
            existing_request = self.get_open_patient_request(patient_id)
            new_request = self.to_patient_request(patient_id, patient_tasks)

            if existing_request is None:
                db.patient_requests.insert(new_request.model_dump())
                stats.requests_created += 1
                log.debug(
                    "inserted request %s",
                    format_kv(
                        request_id=new_request.id,
                        patient_id=patient_id,
                        tasks=len(patient_tasks),
                    ),
                )
            else:
                new_request.id = existing_request.id
                db.patient_requests.update(
                    new_request.model_dump(),
                    where('id') == existing_request.id,
                )
                stats.requests_updated += 1
                log.debug(
                    "updated request %s",
                    format_kv(
                        request_id=existing_request.id,
                        patient_id=patient_id,
                        tasks=len(patient_tasks),
                    ),
                )

        return stats

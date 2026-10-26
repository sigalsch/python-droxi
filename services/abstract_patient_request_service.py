from abc import ABC, abstractmethod
from models.patient_task import PatientTask
from uuid import uuid4

from models.patient_request import PatientRequest
from observability import UpdateStats
from operator import attrgetter

task_date_getter = attrgetter('updated_date')


class PatientRequestService(ABC):

    @abstractmethod
    def update_requests(self, tasks: list[PatientTask]) -> UpdateStats:
        """Update the matching ``PatientRequest`` rows for the given open
        and modified tasks. Returns :class:`UpdateStats` with per-batch
        counters so the caller can emit one summary log for the batch.
        """
        raise NotImplementedError

    def to_patient_request(self, patient_id, patient_tasks):
        open_tasks: list[PatientTask] = [
            t for t in patient_tasks if t.status == 'Open']

        request_status = 'Open' if open_tasks else 'Closed'

        # Closed tasks only matter when the whole request is being closed.
        request_tasks = open_tasks or patient_tasks

        tasks_by_updated_asc: list[PatientTask] = sorted(
            request_tasks, key=task_date_getter)

        newest_task = tasks_by_updated_asc[-1]
        oldest_created_date = min(task.created_date for task in request_tasks)

        return PatientRequest(
            id=str(uuid4()),
            assigned_to=newest_task.assigned_to,
            created_date=oldest_created_date,
            updated_date=newest_task.updated_date,
            patient_id=patient_id,
            pharmacy_id=newest_task.pharmacy_id,
            task_ids={t.id for t in request_tasks},
            status=request_status,
            messages=[t.message for t in tasks_by_updated_asc],
            medications={m for t in request_tasks for m in t.medications},
        )

import logging
import time

from models import TaskInput
from observability import correlation_scope, format_kv
from services.patient_request_service import PerPatientRequestService
from services.abstract_patient_request_service import PatientRequestService
from services.task_service import TaskService


log = logging.getLogger(__name__)


class ClinicManager:

    def __init__(self, patientRequestService: PatientRequestService = None):
        self.patient_request_service = patientRequestService or PerPatientRequestService()
        self.task_service = TaskService()

    def process_tasks_update(self, task_input: TaskInput):
        """Accepts a task_input object that contains all the tasks that were modified since
        the last time this method was called. The method process the changes to the tasks, and updates the patient requests appropriately. """

        tasks = task_input.tasks
        if not tasks:
            return

        # One correlation id per batch; services pick it up via the
        # ContextVar in observability.py.
        with correlation_scope() as cid:
            affected_patients = {t.patient_id for t in tasks}
            log.info(
                "process_tasks_update started %s",
                format_kv(
                    batch_size=len(tasks),
                    affected_patients=len(affected_patients),
                    service=type(self.patient_request_service).__name__,
                ),
            )

            # DEBUG trace of batch tasks (ids only, see PHI rule in
            # observability.py). isEnabledFor skips the join when DEBUG is off.
            if log.isEnabledFor(logging.DEBUG):
                log.debug(
                    "batch tasks: %s",
                    "; ".join(
                        format_kv(
                            task_id=t.id,
                            patient_id=t.patient_id,
                            department=t.assigned_to,
                            status=t.status,
                        )
                        for t in tasks
                    ),
                )

            start = time.perf_counter()

            self.task_service.updates_tasks(tasks)

            newly_closed_tasks = [t for t in tasks if t.status == 'Closed']

            # Question: What is a potential performance issue with this code ?
            # Answered in CHANGES.md -> "Deferred" (Performance):
            # returns every Open task every batch, including untouched
            # patients. Fine at the stated scale; fix is to scope
            # get_open_tasks to affected patient_ids. Deferred.
            open_tasks = list(self.task_service.get_open_tasks())

            stats = self.patient_request_service.update_requests(
                open_tasks + newly_closed_tasks)

            duration_ms = (time.perf_counter() - start) * 1000.0

            log.info(
                "process_tasks_update completed %s",
                format_kv(
                    duration_ms=f"{duration_ms:.1f}",
                    batch_size=len(tasks),
                    newly_closed_tasks=len(newly_closed_tasks),
                    open_tasks_scanned=len(open_tasks),
                    correlation_id=cid,
                    **stats.as_log_fields(),
                ),
            )

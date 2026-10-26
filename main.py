"""Entry point: drives the four sample batches through ``ClinicManager``
so a reviewer sees the real log output and (with ``generate_requests``
in ``tests/config.py``) can inspect ``tinydb/db.json`` after the run.

Defaults to :class:`DepartmentPatientRequestService`; swap in
``PerPatientRequestService`` for the original per-patient grouping.
"""

from os import PathLike
from pathlib import Path

import db.db_tinydb as db
from clinic_manager import ClinicManager
from models import TaskInput
from observability import configure_logging
from services.patient_department_request_service import DepartmentPatientRequestService


cur_dir = Path(__file__).parent

files = [
    './data/input_task_1.json',
    './data/input_task_2.json',
    './data/input_task_3.json',
    './data/input_task_4.json',
]


def load_input(file: PathLike):
    with open(file) as f:
        input_json = f.read()
    return TaskInput.model_validate_json(input_json)


def load_all_inputs():
    return [load_input(cur_dir / f) for f in files]


def main():
    configure_logging()
    db.init_db()

    manager = ClinicManager(DepartmentPatientRequestService())
    for task_input in load_all_inputs():
        manager.process_tasks_update(task_input)


if __name__ == '__main__':
    main()

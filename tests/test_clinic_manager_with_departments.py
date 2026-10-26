from datetime import datetime, timedelta

from tinydb import where
from main import load_all_inputs
import db.db_tinydb as db
from clinic_manager import ClinicManager
from models.patient_task import Medication, PatientTask
from models.task_input import TaskInput
from services.patient_department_request_service import DepartmentPatientRequestService

BASE = datetime(2026, 1, 1, 9, 0, 0)
ACET = Medication(code="ACET001", name="Acetaminophen")
IBU = Medication(code="IBU001", name="Ibuprofen")


def make_task(
        task_id: str,
        *,
        assigned_to: str,
        status: str = "Open",
        message: str | None = None,
        medications: list[Medication] | None = None,
        hours: int = 0,
        patient_id: str = "patientX") -> PatientTask:
    ts = BASE + timedelta(hours=hours)
    return PatientTask(
        id=task_id,
        patient_id=patient_id,
        status=status,
        assigned_to=assigned_to,
        created_date=ts,
        updated_date=ts,
        message=message or task_id,
        medications=medications or [],
        pharmacy_id=None,
    )


def department_manager() -> ClinicManager:
    return ClinicManager(DepartmentPatientRequestService())


# The autouse ``init_db`` fixture lives in tests/conftest.py and gives every
# test a fresh in-memory TinyDB; see CHANGES.md.


def test_task_processing_with_department_support():

    inputs = load_all_inputs()

    patient_request_manger = ClinicManager(DepartmentPatientRequestService())

    first_update(inputs, patient_request_manger)

    second_update(inputs, patient_request_manger)

    third_update(inputs, patient_request_manger)

    fourth_update(inputs, patient_request_manger)


def first_update(inputs, patient_request_manger):
    patient_request_manger.process_tasks_update(inputs[0])

    patient1_open = get_open_patient_requests("patient1")

    assert len(
        patient1_open) == 1, "Error after input 1, incorrect number of open requests"

    pat1_req = patient1_open[0]

    assert len(
        pat1_req['messages']) == 2, "Error after input 1, messages length on the open request is incorrect "
    assert set(map(lambda m: m['code'], pat1_req['medications'])) == {"ACET001", "IBU001", "LISI001"
                                                                      }, "Error after input 1,  the medication set is incorrect"

    patient2_open = get_open_patient_requests("patient2")

    assert len(
        patient2_open) == 2, "Error after input 1, incorrect number of open requests.\
            Patient 2 tasks should have been split to two requests due to department grouping"

    pat2_req = next(
        (req for req in patient2_open if req['assigned_to'] == 'Dermatology'), None)

    assert len(
        pat2_req['messages']) == 1, "Error after input 1, messages length on the open request is incorrect "
    assert set(map(lambda m: m['code'], pat2_req['medications'])) == {"METR001", "IBU001"
                                                                      }, "Error after input 1,  the medication set is incorrect for patient2"

    assert len(
        get_open_patient_requests("patient3")) == 2, "Error after input 1, incorrect number of open requests.\
            Patient 3 tasks should have been split to two requests due to department grouping"


def second_update(inputs, patient_request_manger):
    patient_request_manger.process_tasks_update(inputs[1])
    assert len(get_open_patient_requests(
        "patient1")) == 0, "Error after input 2, the patient1 request should be closed"

    patient2_open = get_open_patient_requests("patient2")
    assert len(patient2_open) == 2, "Error after input 2"

    pat2_req = next(
        (req for req in patient2_open if req['assigned_to'] == 'Primary'), None)

    assert pat2_req['messages'] == [
        "The results of the patient's blood tests are in. Please review them ASAP."
    ], "Error after input 2, "

    assert len(
        get_open_patient_requests("patient3")) == 1, "Error after input 1, incorrect number of open requests.\
            Patient 3 tasks should have been merged to a single department grouping"


def third_update(inputs, patient_request_manger):
    patient_request_manger.process_tasks_update(inputs[2])

    assert count_open_patient_requests(
    ) == 0, "Error after input 3, there should be no open requests left"


def fourth_update(inputs, patient_request_manger):
    patient_request_manger.process_tasks_update(inputs[3])
    assert count_open_patient_requests(
    ) == 1, "Error after input 4, there should be one open request"


def test_department_reassignment_closes_only_the_emptied_department():
    """Regression: an Open task that moves B -> C (with nothing else in B)
    must close *only* the (patient, B) PatientRequest and leave (patient, A)
    Open, even though both A and B started as Open for the same patient.

    This directly exercises the leftover-close branch in
    ``DepartmentPatientRequestService.update_requests`` and locks in the
    partial-batch guard that cross-checks live Open tasks before closing.
    """

    manager = ClinicManager(DepartmentPatientRequestService())

    base = datetime(2026, 1, 1, 9, 0, 0)

    task_a = PatientTask(
        id="t-a",
        patient_id="patientX",
        status="Open",
        assigned_to="Dermatology",
        created_date=base,
        updated_date=base,
        message="A: skin consult",
        medications=[],
        pharmacy_id=None,
    )
    task_b = PatientTask(
        id="t-b",
        patient_id="patientX",
        status="Open",
        assigned_to="Radiology",
        created_date=base,
        updated_date=base,
        message="B: imaging",
        medications=[],
        pharmacy_id=None,
    )

    # First update: patient has one Open task in Dermatology and one in Radiology.
    manager.process_tasks_update(TaskInput(tasks=[task_a, task_b]))

    open_requests = get_open_patient_requests("patientX")
    open_by_dept = {req['assigned_to']: req for req in open_requests}
    assert set(open_by_dept) == {"Dermatology", "Radiology"}, \
        "Setup failed: expected one Open PatientRequest per department"

    dermatology_req_id = open_by_dept["Dermatology"]['id']
    radiology_req_id = open_by_dept["Radiology"]['id']

    # Second update: the Radiology task is reassigned to Primary. The
    # Dermatology task is unchanged -- it is NOT re-sent in the input, which
    # is the common case where only the modified task hits the pipeline.
    task_b_moved = task_b.model_copy(update={
        "assigned_to": "Primary",
        "updated_date": base + timedelta(hours=1),
    })

    manager.process_tasks_update(TaskInput(tasks=[task_b_moved]))

    open_after = get_open_patient_requests("patientX")
    open_by_dept_after = {req['assigned_to']: req for req in open_after}

    assert set(open_by_dept_after) == {"Dermatology", "Primary"}, \
        "After reassignment: Dermatology must stay Open and Primary must be the new Open department"

    # The Dermatology request is the *same* record, still Open -- it must not
    # have been swept by the leftover-close branch.
    assert open_by_dept_after["Dermatology"]['id'] == dermatology_req_id, \
        "Dermatology request should be preserved, not recreated"

    # The old Radiology request must now be Closed, with its snapshot
    # (task_ids / messages) kept intact per the design in CHANGES.md.
    radiology_doc = db.patient_requests.get(where('id') == radiology_req_id)
    assert radiology_doc is not None, "Radiology request should still exist"
    assert radiology_doc['status'] == 'Closed', \
        "Radiology request should be Closed after its only task moved to Primary"
    assert radiology_doc['task_ids'] == {"t-b"}, \
        "Closed Radiology request should keep its task_ids snapshot"


def test_partial_move_keeps_source_department_open():
    """Only some Open tasks leave a department: the source stays Open."""
    manager = department_manager()
    t1 = make_task("t1", assigned_to="Radiology", message="stay")
    t2 = make_task("t2", assigned_to="Radiology", message="move", hours=1)
    manager.process_tasks_update(TaskInput(tasks=[t1, t2]))

    radiology_id = get_open_by_dept("patientX")["Radiology"]["id"]

    t2_moved = t2.model_copy(update={
        "assigned_to": "Primary",
        "updated_date": BASE + timedelta(hours=2),
    })
    manager.process_tasks_update(TaskInput(tasks=[t2_moved]))

    open_by_dept = get_open_by_dept("patientX")
    assert set(open_by_dept) == {"Radiology", "Primary"}
    assert open_by_dept["Radiology"]["id"] == radiology_id
    assert open_by_dept["Radiology"]["task_ids"] == {"t1"}
    assert open_by_dept["Radiology"]["messages"] == ["stay"]
    assert open_by_dept["Primary"]["task_ids"] == {"t2"}
    assert open_by_dept["Primary"]["messages"] == ["move"]


def test_partial_close_removes_closed_task_data_from_open_request():
    """Closing one of several Open tasks drops its data; the request stays Open."""
    manager = department_manager()
    t1 = make_task(
        "t1", assigned_to="Dermatology", message="first", medications=[ACET])
    t2 = make_task(
        "t2", assigned_to="Dermatology", message="second",
        medications=[IBU], hours=1)
    manager.process_tasks_update(TaskInput(tasks=[t1, t2]))

    dermatology_id = get_open_by_dept("patientX")["Dermatology"]["id"]

    t1_closed = t1.model_copy(update={
        "status": "Closed",
        "updated_date": BASE + timedelta(hours=2),
    })
    manager.process_tasks_update(TaskInput(tasks=[t1_closed]))

    open_reqs = get_open_patient_requests("patientX")
    assert len(open_reqs) == 1
    remaining = open_reqs[0]
    assert remaining["id"] == dermatology_id
    assert remaining["status"] == "Open"
    assert remaining["task_ids"] == {"t2"}
    assert remaining["messages"] == ["second"]
    assert {m["code"] for m in remaining["medications"]} == {"IBU001"}


def test_move_merges_into_existing_destination_request():
    """Last task leaving a department joins an already-Open destination."""
    manager = department_manager()
    t_primary = make_task("t-primary", assigned_to="Primary", message="primary")
    t_rad = make_task("t-rad", assigned_to="Radiology", message="radiology", hours=1)
    manager.process_tasks_update(TaskInput(tasks=[t_primary, t_rad]))

    open_by_dept = get_open_by_dept("patientX")
    primary_id = open_by_dept["Primary"]["id"]
    radiology_id = open_by_dept["Radiology"]["id"]

    t_rad_moved = t_rad.model_copy(update={
        "assigned_to": "Primary",
        "updated_date": BASE + timedelta(hours=2),
    })
    manager.process_tasks_update(TaskInput(tasks=[t_rad_moved]))

    open_after = get_open_by_dept("patientX")
    assert set(open_after) == {"Primary"}
    assert open_after["Primary"]["id"] == primary_id
    assert open_after["Primary"]["task_ids"] == {"t-primary", "t-rad"}
    assert open_after["Primary"]["messages"] == ["primary", "radiology"]

    radiology_doc = db.patient_requests.get(where("id") == radiology_id)
    assert radiology_doc["status"] == "Closed"
    assert radiology_doc["task_ids"] == {"t-rad"}


def test_new_open_task_does_not_reopen_closed_request():
    """A later Open task for the same (patient, dept) is a new request."""
    manager = department_manager()
    t1 = make_task("t1", assigned_to="Primary", message="first visit")
    manager.process_tasks_update(TaskInput(tasks=[t1]))
    first_id = get_open_by_dept("patientX")["Primary"]["id"]

    t1_closed = t1.model_copy(update={
        "status": "Closed",
        "updated_date": BASE + timedelta(hours=1),
    })
    manager.process_tasks_update(TaskInput(tasks=[t1_closed]))
    assert get_open_patient_requests("patientX") == []

    t2 = make_task("t2", assigned_to="Primary", message="second visit", hours=2)
    manager.process_tasks_update(TaskInput(tasks=[t2]))

    open_after = get_open_by_dept("patientX")
    assert set(open_after) == {"Primary"}
    assert open_after["Primary"]["id"] != first_id
    assert open_after["Primary"]["task_ids"] == {"t2"}

    historical = db.patient_requests.get(where("id") == first_id)
    assert historical is not None
    assert historical["status"] == "Closed"
    assert historical["task_ids"] == {"t1"}


def count_open_patient_requests():
    return len(db.patient_requests.search(where('status') == "Open"))


def get_open_patient_requests(patient_id) -> list[dict] | None:
    return db.patient_requests.search(
        (where('patient_id') == patient_id) & (where('status') == 'Open'))


def get_open_by_dept(patient_id) -> dict[str, dict]:
    return {req["assigned_to"]: req for req in get_open_patient_requests(patient_id)}

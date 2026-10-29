import hashlib
import gzip
import hmac
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import current_user
from app.core.config import get_settings
from app.db import get_db
from app.models import AnalysisResult, Case, MRIUpload, PatientVisibility, User, Visit
from app.schemas import (
    AnalysisResultCreateRequest,
    CaseCreateRequest,
    CaseUpdateRequest,
    PatientIdRequest,
    ProfileUpdateRequest,
    VisibilityUpdateRequest,
    VisitCreateRequest,
    VisitUpdateRequest,
)

router = APIRouter(tags=["cases and reports"])
SEQUENCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
ALLOWED_SEQUENCES = {
    "T1",
    "T1c",
    "T2",
    "FLAIR",
    "DWI",
    "ADC",
    "SWI",
    "ASL",
    "DTI_FA",
    "DTI_MD",
    "DTI_L1",
    "DTI_L2",
    "DTI_L3",
    "SEG",
}


def tc_digest(value: str) -> str:
    secret = get_settings().jwt_secret.encode()
    return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()


def valid_turkish_id(value: str) -> bool:
    if not re.fullmatch(r"[1-9]\d{10}", value):
        return False
    digits = [int(char) for char in value]
    odd = sum(digits[0:9:2])
    even = sum(digits[1:8:2])
    return (odd * 7 - even) % 10 == digits[9] and sum(digits[:10]) % 10 == digits[10]


def doctor_required(user: User) -> User:
    if user.role != "doctor":
        raise HTTPException(status_code=403, detail="Doctor role required")
    if get_settings().doctor_approval_required and not user.doctor_verified:
        raise HTTPException(
            status_code=403, detail="Doctor account approval is required"
        )
    return user


def case_for_user(case_id: str, user: User, db: Session) -> Case:
    query = select(Case).where(Case.id == case_id)
    if user.role == "doctor":
        query = query.where(Case.doctor_id == user.id)
    else:
        query = query.where(Case.patient_id == user.id)
    case = db.scalar(query)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


def visit_for_user(visit_id: int, user: User, db: Session) -> Visit:
    visit = db.get(Visit, visit_id)
    if not visit:
        raise HTTPException(status_code=404, detail="Visit not found")
    case_for_user(visit.case_id, user, db)
    return visit


def visibility_for(case: Case, db: Session) -> PatientVisibility:
    row = db.get(PatientVisibility, case.id)
    if row is None:
        row = PatientVisibility(case_id=case.id, doctor_id=case.doctor_id)
        db.add(row)
        db.flush()
    return row


def serialize_case(case: Case, user: User):
    return {
        "id": case.id,
        "patient_name": case.patient_name,
        "age": case.age,
        "sex": case.sex,
        "scan_date": case.scan_date,
        "condition": case.condition,
        "status": case.status,
        "risk": case.risk,
        "doctor_id": case.doctor_id if user.role == "doctor" else None,
        "patient_id": case.patient_id,
        "created_at": case.created_at,
    }


@router.get("/profile")
def get_profile(user: User = Depends(current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "city": user.city,
        "hospital": user.hospital,
        "doctor_verified": user.doctor_verified,
        "has_tc": bool(user.patient_tc_hash),
    }


@router.patch("/profile")
def update_profile(
    payload: ProfileUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    user.full_name = payload.full_name.strip()
    if user.role == "doctor":
        user.city, user.hospital = payload.city, payload.hospital
    db.commit()
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "city": user.city,
        "hospital": user.hospital,
        "doctor_verified": user.doctor_verified,
        "has_tc": bool(user.patient_tc_hash),
    }


@router.put("/profile/patient-id")
def set_patient_id(
    payload: PatientIdRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if user.role != "patient":
        raise HTTPException(status_code=403, detail="Patient role required")
    patient_tc = payload.patient_tc
    if not valid_turkish_id(patient_tc):
        raise HTTPException(
            status_code=422, detail="Invalid patient identification number"
        )
    digest = tc_digest(patient_tc)
    existing = db.scalar(
        select(User).where(User.patient_tc_hash == digest, User.id != user.id)
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="This patient ID is already linked to another account",
        )
    user.patient_tc_hash = digest
    linked = db.scalars(
        select(Case).where(Case.patient_tc_hash == digest, Case.patient_id.is_(None))
    ).all()
    for case in linked:
        case.patient_id = user.id
    db.commit()
    return {"has_tc": True, "linked": len(linked)}


@router.post("/cases", status_code=status.HTTP_201_CREATED)
def create_case(
    payload: CaseCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    doctor_required(user)
    if not valid_turkish_id(payload.patient_tc):
        raise HTTPException(
            status_code=422, detail="Invalid patient identification number"
        )
    digest = tc_digest(payload.patient_tc)
    patient = db.scalar(
        select(User).where(User.patient_tc_hash == digest, User.role == "patient")
    )
    if db.get(Case, payload.id):
        raise HTTPException(status_code=409, detail="Case ID already exists")
    case = Case(
        id=payload.id,
        patient_name=payload.patient_name.strip(),
        age=payload.age,
        sex=payload.sex,
        scan_date=payload.scan_date.date(),
        condition=payload.condition,
        status="Pending",
        doctor_id=user.id,
        patient_id=patient.id if patient else None,
        patient_tc_hash=digest,
    )
    db.add(case)
    db.add(PatientVisibility(case_id=case.id, doctor_id=user.id))
    db.commit()
    db.refresh(case)
    return serialize_case(case, user)


@router.get("/cases")
def list_cases(db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = (
        select(Case).options(selectinload(Case.visits)).order_by(Case.created_at.desc())
    )
    query = (
        query.where(Case.doctor_id == user.id)
        if user.role == "doctor"
        else query.where(Case.patient_id == user.id)
    )
    return [
        {
            "id": c.id,
            "patient_name": c.patient_name,
            "age": c.age,
            "sex": c.sex,
            "scan_date": c.scan_date,
            "condition": c.condition,
            "status": c.status,
            "risk": c.risk,
            "doctor_id": c.doctor_id if user.role == "doctor" else None,
            "patient_id": c.patient_id,
            "created_at": c.created_at,
            "visits": [{"count": len(c.visits)}] if user.role == "doctor" else None,
        }
        for c in db.scalars(query).all()
    ]


@router.get("/cases/{case_id}")
def get_case(
    case_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    case = case_for_user(case_id, user, db)
    return serialize_case(case, user)


@router.patch("/cases/{case_id}")
def update_case(
    case_id: str,
    payload: CaseUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    doctor_required(user)
    case = case_for_user(case_id, user, db)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(
            status_code=422, detail="At least one case field must be supplied"
        )
    for key, value in changes.items():
        setattr(case, key, value)
    db.commit()
    db.refresh(case)
    return serialize_case(case, user)


@router.post("/cases/{case_id}/visits", status_code=status.HTTP_201_CREATED)
def create_visit(
    case_id: str,
    payload: VisitCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    doctor_required(user)
    case = case_for_user(case_id, user, db)
    visit = Visit(
        case_id=case.id,
        visit_date=payload.visit_date.date(),
        notes=payload.notes,
        status="Pending",
    )
    case.status = "Pending"
    db.add(visit)
    db.commit()
    db.refresh(visit)
    return visit


@router.get("/cases/{case_id}/visits")
def list_visits(
    case_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    case = case_for_user(case_id, user, db)
    visits = db.scalars(
        select(Visit)
        .options(selectinload(Visit.analysis_result))
        .where(Visit.case_id == case.id)
        .order_by(Visit.visit_date, Visit.created_at)
    ).all()
    if user.role == "doctor":
        return visits
    visibility = visibility_for(case, db)
    result = []
    for visit in visits:
        row = {
            "id": visit.id,
            "case_id": visit.case_id,
            "visit_date": visit.visit_date,
            "status": visit.status,
            "risk": visit.risk,
            "created_at": visit.created_at,
        }
        analysis = visit.analysis_result
        if analysis:
            allowed_result = {}
            if visibility.tumor_type:
                allowed_result["tumor_type"] = analysis.tumor_type
            if visibility.tumor_type:
                allowed_result["grade"] = analysis.grade
            if visibility.clinical_markers:
                allowed_result.update(
                    idh_status=analysis.idh_status, mgmt_status=analysis.mgmt_status
                )
            if visibility.survival_estimate:
                allowed_result["survival_estimate"] = analysis.survival_estimate
            if visibility.xai_explanation:
                allowed_result["xai_features"] = analysis.xai_features
            row["analysis_result"] = allowed_result
        result.append(row)
    return result


@router.patch("/visits/{visit_id}")
def update_visit(
    visit_id: int,
    payload: VisitUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    doctor_required(user)
    visit = visit_for_user(visit_id, user, db)
    if "notes" in payload.model_fields_set:
        visit.notes = payload.notes
    if payload.status is not None:
        visit.status = payload.status
    db.commit()
    db.refresh(visit)
    return visit


@router.put("/cases/{case_id}/visibility")
def update_visibility(
    case_id: str,
    payload: VisibilityUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    doctor_required(user)
    case = case_for_user(case_id, user, db)
    row = visibility_for(case, db)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@router.get("/cases/{case_id}/visibility")
def get_visibility(
    case_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    case = case_for_user(case_id, user, db)
    return visibility_for(case, db)


@router.post("/visits/{visit_id}/uploads", status_code=status.HTTP_201_CREATED)
async def upload_mri(
    visit_id: int,
    sequence_name: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    doctor_required(user)
    visit = visit_for_user(visit_id, user, db)
    if (
        not SEQUENCE_RE.fullmatch(sequence_name)
        or sequence_name not in ALLOWED_SEQUENCES
    ):
        raise HTTPException(status_code=422, detail="Invalid sequence name")
    filename = Path(file.filename or "scan.nii.gz").name
    if len(filename) > 255:
        raise HTTPException(
            status_code=422, detail="Filename must not exceed 255 characters"
        )
    if not (filename.lower().endswith(".nii") or filename.lower().endswith(".nii.gz")):
        raise HTTPException(
            status_code=415, detail="MRI uploads must be .nii or .nii.gz files"
        )
    max_bytes = get_settings().mri_max_upload_bytes
    root = Path(get_settings().mri_storage_dir).resolve()
    target_dir = (root / str(user.id) / visit.case_id / str(visit.id)).resolve()
    if root not in target_dir.parents:
        raise HTTPException(status_code=400, detail="Invalid upload location")
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{uuid.uuid4().hex}_{filename}"
    size = 0
    try:
        with path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413, detail="MRI file exceeds configured size limit"
                    )
                output.write(chunk)
        try:
            if filename.lower().endswith(".nii.gz"):
                with gzip.open(path, "rb") as image_file:
                    header_size = image_file.read(4)
            else:
                with path.open("rb") as image_file:
                    header_size = image_file.read(4)
        except (OSError, EOFError):
            header_size = b""
        if header_size not in (b"\x5c\x01\x00\x00", b"\x00\x00\x01\x5c"):
            raise HTTPException(status_code=415, detail="Invalid NIfTI image header")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    row = MRIUpload(
        case_id=visit.case_id,
        visit_id=visit.id,
        sequence_name=sequence_name,
        file_name=filename,
        file_path=str(path),
    )
    try:
        db.add(row)
        db.commit()
        db.refresh(row)
    except Exception:
        db.rollback()
        path.unlink(missing_ok=True)
        raise
    return {
        "id": row.id,
        "case_id": row.case_id,
        "visit_id": row.visit_id,
        "sequence_name": row.sequence_name,
        "file_name": row.file_name,
        "download_url": f"/api/v1/uploads/{row.id}",
    }


@router.get("/cases/{case_id}/uploads")
def list_uploads(
    case_id: str,
    visit_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    case_for_user(case_id, user, db)
    query = select(MRIUpload).where(MRIUpload.case_id == case_id)
    if visit_id is not None:
        query = query.where(MRIUpload.visit_id == visit_id)
    return [
        {
            "id": item.id,
            "case_id": item.case_id,
            "visit_id": item.visit_id,
            "sequence_name": item.sequence_name,
            "file_name": item.file_name,
            "download_url": f"/api/v1/uploads/{item.id}",
        }
        for item in db.scalars(query).all()
    ]


@router.get("/uploads/{upload_id}")
def download_upload(
    upload_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    row = db.get(MRIUpload, upload_id)
    if not row:
        raise HTTPException(status_code=404, detail="MRI upload not found")
    case_for_user(row.case_id, user, db)
    path = Path(row.file_path).resolve()
    root = Path(get_settings().mri_storage_dir).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="MRI file not found")
    return FileResponse(
        path,
        filename=row.file_name,
        media_type="application/gzip"
        if row.file_name.lower().endswith(".gz")
        else "application/octet-stream",
    )


@router.post("/visits/{visit_id}/analysis-results", status_code=status.HTTP_201_CREATED)
def save_analysis(
    visit_id: int,
    payload: AnalysisResultCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    doctor_required(user)
    visit = visit_for_user(visit_id, user, db)
    existing = db.scalar(
        select(AnalysisResult).where(AnalysisResult.visit_id == visit_id)
    )
    if existing:
        raise HTTPException(
            status_code=409, detail="Analysis result already exists for this visit"
        )
    row = AnalysisResult(
        case_id=visit.case_id, visit_id=visit.id, **payload.model_dump()
    )
    db.add(row)
    visit.status = "Done"
    other_incomplete = db.scalar(
        select(Visit.id)
        .where(
            Visit.case_id == visit.case_id, Visit.id != visit.id, Visit.status != "Done"
        )
        .limit(1)
    )
    if other_incomplete is None:
        visit.case.status = "Done"
    db.commit()
    db.refresh(row)
    return row


@router.get("/visits/{visit_id}/analysis-results")
def get_analysis(
    visit_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    visit = visit_for_user(visit_id, user, db)
    row = db.scalar(select(AnalysisResult).where(AnalysisResult.visit_id == visit.id))
    if not row:
        raise HTTPException(status_code=404, detail="Analysis result is not available")
    if user.role == "patient":
        visibility = visibility_for(case_for_user(visit.case_id, user, db), db)
        response = {}
        if visibility.tumor_type:
            response.update(tumor_type=row.tumor_type, grade=row.grade)
        if visibility.clinical_markers:
            response.update(idh_status=row.idh_status, mgmt_status=row.mgmt_status)
        if visibility.survival_estimate:
            response["survival_estimate"] = row.survival_estimate
        if visibility.xai_explanation:
            response["xai_features"] = row.xai_features
        return response
    return row

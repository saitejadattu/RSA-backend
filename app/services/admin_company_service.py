import asyncio
from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.db.collections import APPLICATIONS, COMPANIES, HIRING_OPPORTUNITIES, STATUS_HISTORY, STUDENTS
from app.db.mongodb import get_database
from app.models.application import final_status_for, is_real_application, status_for_api
from app.utils.mongo import serialize_mongo
from app.utils.object_id import to_object_id


def _object_id(value: str, label: str):
    try:
        return to_object_id(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid {label}"
        )


def _blank_counts() -> dict:
    return {
        "response_count": 0,
        "applied_count": 0,
        "shortlisted_count": 0,
        "interview_completed_count": 0,
        "rejected_count": 0,
        "hired_count": 0,
        "not_interested_count": 0,
    }


def _tally(counts: dict, application: dict) -> None:
    counts["response_count"] += 1
    if not is_real_application(application):
        counts["not_interested_count"] += 1
        return

    counts["applied_count"] += 1
    current_status = status_for_api(application)
    if current_status == "SHORTLISTED" or current_status == "shortlisted":
        counts["shortlisted_count"] += 1
    elif current_status in {"INTERVIEW_COMPLETED", "interview_completed", "INTERVIEW_DONE", "interview_done"}:
        counts["interview_completed_count"] += 1
    elif current_status == "REJECTED" or current_status == "rejected":
        counts["rejected_count"] += 1
    elif current_status in {"SELECTED", "JOINED", "hired"}:
        counts["hired_count"] += 1


async def get_admin_company_detail(company_id: str) -> dict:
    """Company overview plus each of its opportunities with per-job counts (chooser data)."""
    db = get_database()
    object_id = _object_id(company_id, "company id")

    # These three are independent — run them together rather than back-to-back
    # round trips to a remote Atlas.
    company, opportunities, applications = await asyncio.gather(
        db[COMPANIES].find_one({"_id": object_id}),
        db[HIRING_OPPORTUNITIES].find(
            {"company_id": object_id, "deleted_at": {"$exists": False}}
        ).sort("opportunity_received_at", -1).to_list(length=None),
        db[APPLICATIONS].find({"company_id": object_id}).to_list(length=None),
    )
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    company_counts = _blank_counts()
    counts_by_opportunity: dict = {}
    for application in applications:
        _tally(company_counts, application)
        opportunity_id = application.get("opportunity_id")
        counts_by_opportunity.setdefault(opportunity_id, _blank_counts())
        _tally(counts_by_opportunity[opportunity_id], application)

    opportunity_rows = []
    for opportunity in opportunities:
        counts = counts_by_opportunity.get(opportunity["_id"], _blank_counts())
        opportunity_rows.append(
            {
                "id": opportunity["_id"],
                "role": opportunity.get("role"),
                "tech_stack": opportunity.get("tech_stack"),
                "must_have_skills": opportunity.get("must_have_skills"),
                "location": opportunity.get("location"),
                "stipend": opportunity.get("stipend"),
                "duration": opportunity.get("duration"),
                "company_status": opportunity.get("company_status"),
                "opportunity_received_at": opportunity.get("opportunity_received_at"),
                **counts,
            }
        )

    return serialize_mongo(
        {
            "company": company,
            "opportunity_count": len(opportunities),
            "stats": company_counts,
            "opportunities": opportunity_rows,
        }
    )


async def get_admin_opportunity_detail(opportunity_id: str) -> dict:
    """Full detail for one opportunity: rich fields, counts, and the applicant list."""
    db = get_database()
    object_id = _object_id(opportunity_id, "opportunity id")

    pipeline = [
        {"$match": {"opportunity_id": object_id}},
        {"$sort": {"applied_at": -1, "created_at": -1}},
        {"$lookup": {"from": STUDENTS, "localField": "student_id", "foreignField": "_id", "as": "student"}},
        {"$unwind": {"path": "$student", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "current_status": 1,
                "final_status": 1,
                "status": {"$ifNull": ["$current_status", "$status"]},
                "is_interested": {"$ifNull": ["$application_details.interested", "$is_interested"]},
                "applied_at": 1,
                "application_details": 1,
                "github_link": {"$ifNull": ["$application_details.github_link", "$github_link"]},
                "project_link": {"$ifNull": ["$application_details.project_link", "$project_link"]},
                "resume_link": {"$ifNull": ["$application_details.submitted_resume_url", "$resume_link"]},
                "has_relevant_project_experience": {
                    "$ifNull": [
                        "$application_details.has_relevant_project_experience",
                        "$has_relevant_project_experience",
                    ]
                },
                "student": {
                    "_id": "$student._id",
                    "name": "$student.name",
                    "email": "$student.email",
                    "phone": "$student.phone",
                },
            }
        },
    ]
    # Opportunity + its applicants are both keyed by the opportunity id — fetch
    # together; the company then needs the opportunity's company_id.
    opportunity, applicants = await asyncio.gather(
        db[HIRING_OPPORTUNITIES].find_one({"_id": object_id, "deleted_at": {"$exists": False}}),
        db[APPLICATIONS].aggregate(pipeline).to_list(length=None),
    )
    if not opportunity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Hiring opportunity not found"
        )
    company = await db[COMPANIES].find_one({"_id": opportunity.get("company_id")})

    counts = _blank_counts()
    for application in applicants:
        _tally(counts, application)

    return serialize_mongo(
        {
            "company": company,
            "opportunity": opportunity,
            "stats": counts,
            "applicants": applicants,
        }
    )


async def archive_opportunity(opportunity_id: str, *, admin: dict, reason: str) -> dict:
    """Soft-delete an opportunity while retaining related records and audit data."""
    db = get_database()
    object_id = _object_id(opportunity_id, "opportunity id")
    opportunity = await db[HIRING_OPPORTUNITIES].find_one(
        {"_id": object_id, "deleted_at": {"$exists": False}},
        {"_id": 1, "company_id": 1, "role": 1},
    )
    if not opportunity:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hiring opportunity not found")

    normalized_reason = reason.strip()
    if not normalized_reason:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Deletion reason is required")

    deleted_at = datetime.now(timezone.utc)
    deleted_by = {
        "id": admin.get("sub"),
        "name": admin.get("name"),
        "email": admin.get("email"),
    }
    await db[HIRING_OPPORTUNITIES].update_one(
        {"_id": object_id, "deleted_at": {"$exists": False}},
        {
            "$set": {
                "deleted_at": deleted_at,
                "deleted_by": deleted_by,
                "deletion_reason": normalized_reason,
                "updated_at": deleted_at,
            }
        },
    )
    return serialize_mongo({
        "archived": True,
        "opportunity_id": object_id,
        "deleted_at": deleted_at,
        "deleted_by": deleted_by,
        "deletion_reason": normalized_reason,
    })


async def bulk_reject_interviewed(opportunity_id: str) -> dict:
    """Mark all candidates who completed interviews for this exact opportunity
    (and do not already have a final result) as REJECTED.
    """
    db = get_database()
    object_id = _object_id(opportunity_id, "opportunity id")
    now = datetime.now(timezone.utc)

    opportunity = await db[HIRING_OPPORTUNITIES].find_one({"_id": object_id})
    if not opportunity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Hiring opportunity not found"
        )

    # Match applications for this exact opportunity that reached interview completed
    # and have not already received a final result (SELECTED, REJECTED, etc.).
    query = {
        "opportunity_id": object_id,
        "$or": [
            {"current_status": {"$in": ["INTERVIEW_COMPLETED", "interview_completed", "INTERVIEW_DONE", "interview_done"]}},
            {
                "current_status": {"$exists": False},
                "status": {"$in": ["interview_completed", "interview_done"]},
            },
        ],
        "final_status": {"$nin": ["HIRED", "REJECTED", "DROPPED"]},
    }

    applications = await db[APPLICATIONS].find(query).to_list(length=None)

    affected = 0
    for app in applications:
        old_status = status_for_api(app)
        await db[APPLICATIONS].update_one(
            {"_id": app["_id"]},
            {
                "$set": {
                    "current_status": "REJECTED",
                    "final_status": "REJECTED",
                    "rejected_at": now,
                    "updated_at": now,
                }
            },
        )
        await db[STATUS_HISTORY].insert_one(
            {
                "application_id": app["_id"],
                "student_id": app.get("student_id"),
                "company_id": app.get("company_id") or opportunity.get("company_id"),
                "opportunity_id": object_id,
                "old_status": old_status,
                "new_status": "REJECTED",
                "reason": "Marked not selected after interview result confirmed",
                "notes": None,
                "changed_by": None,
                "changed_by_role": "admin",
                "source": "bulk_admin",
                "created_at": now,
            }
        )
        affected += 1

    return {"marked_not_selected": affected, "opportunity_id": str(object_id)}


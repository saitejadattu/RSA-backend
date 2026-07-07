from app.db.collections import APPLICATIONS, COMPANIES, HIRING_OPPORTUNITIES
from app.db.mongodb import get_database
from app.utils.mongo import serialize_mongo


STATUS_LABELS = {
    "applied": "Applied",
    "not_interested": "Not Interested",
    "shortlisted": "Shortlisted",
    "interview_scheduled": "Interview Scheduled",
    "in_progress": "In Progress",
    "rejected": "Rejected",
    "hired": "Hired",
    "dropped": "Dropped",
}


def is_actual_application(application: dict) -> bool:
    return application.get("is_interested") is not False and application.get("status") != "not_interested"


async def list_student_applications(student: dict, *, include_not_interested: bool = False) -> list[dict]:
    db = get_database()
    match_stage = {"student_id": student["_id"]}
    if not include_not_interested:
        match_stage.update({"is_interested": {"$ne": False}, "status": {"$ne": "not_interested"}})

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": HIRING_OPPORTUNITIES,
                "localField": "opportunity_id",
                "foreignField": "_id",
                "as": "opportunity",
            }
        },
        {"$unwind": {"path": "$opportunity", "preserveNullAndEmptyArrays": True}},
        {
            "$lookup": {
                "from": COMPANIES,
                "localField": "company_id",
                "foreignField": "_id",
                "as": "company",
            }
        },
        {"$unwind": {"path": "$company", "preserveNullAndEmptyArrays": True}},
        {"$sort": {"applied_at": -1, "created_at": -1}},
        {
            "$project": {
                "_id": 1,
                "status": 1,
                "is_interested": 1,
                "applied_at": 1,
                "skills": 1,
                "github_link": 1,
                "project_link": 1,
                "resume_link": 1,
                "shortlist": 1,
                "not_interested_reason": 1,
                "company": {
                    "_id": "$company._id",
                    "name": "$company.name",
                    "company_key": "$company.company_key",
                },
                "opportunity": {
                    "_id": "$opportunity._id",
                    "role": "$opportunity.role",
                    "tech_stack": "$opportunity.tech_stack",
                    "must_have_skills": "$opportunity.must_have_skills",
                    "location": "$opportunity.location",
                    "stipend": "$opportunity.stipend",
                    "duration": "$opportunity.duration",
                    "company_status": "$opportunity.company_status",
                    "opportunity_received_at": "$opportunity.opportunity_received_at",
                },
            }
        },
    ]
    applications = await db[APPLICATIONS].aggregate(pipeline).to_list(length=None)
    return serialize_mongo(applications)


def build_summary(applications: list[dict]) -> dict:
    actual_applications = [item for item in applications if is_actual_application(item)]
    total = len(actual_applications)
    shortlisted = sum(1 for item in actual_applications if item.get("status") == "shortlisted")
    rejected = sum(1 for item in actual_applications if item.get("status") == "rejected")
    hired = sum(1 for item in actual_applications if item.get("status") == "hired")
    not_interested = len(applications) - total
    active = total - rejected - hired
    return {
        "total_applications": total,
        "response_count": len(applications),
        "shortlisted_count": shortlisted,
        "rejected_count": rejected,
        "hired_count": hired,
        "not_interested_count": not_interested,
        "active_count": max(active, 0),
    }


async def get_student_dashboard(student: dict) -> dict:
    response_records = await list_student_applications(student, include_not_interested=True)
    applications = [application for application in response_records if is_actual_application(application)]
    summary = build_summary(response_records)
    recent_applications = applications[:5]
    shortlisted_applications = [
        application for application in applications if application.get("status") == "shortlisted"
    ]
    return {
        "summary": summary,
        "applications": applications,
        "response_records": response_records,
        "recent_applications": recent_applications,
        "shortlisted_applications": shortlisted_applications,
        "status_labels": STATUS_LABELS,
    }

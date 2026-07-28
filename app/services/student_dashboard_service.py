from app.db.collections import APPLICATIONS, COMPANIES, HIRING_OPPORTUNITIES
from app.db.mongodb import get_database
from app.models.application import is_real_application
from app.utils.mongo import serialize_mongo


STATUS_LABELS = {
    "APPLIED": "Applied",
    "PROFILE_SHARED": "Profile Shared",
    "SHORTLISTED": "Shortlisted",
    "INTERVIEW_SCHEDULED": "Interview Scheduled",
    "INTERVIEW_IN_PROGRESS": "Interview In Progress",
    "SELECTED": "Selected",
    "OFFER_PENDING": "Offer Pending",
    "OFFER_RELEASED": "Offer Released",
    "OFFER_ACCEPTED": "Offer Accepted",
    "OFFER_REJECTED": "Offer Rejected",
    "JOINED": "Joined",
    "REJECTED": "Rejected",
    "DROPPED": "Dropped",
}


async def list_student_applications(student: dict, *, include_not_interested: bool = False) -> list[dict]:
    db = get_database()
    match_stage = {"student_id": student["_id"]}
    if not include_not_interested:
        match_stage.update(
            {
                "$or": [
                    {"application_details.interested": {"$exists": True, "$ne": False}},
                    {
                        "application_details": {"$exists": False},
                        "is_interested": {"$ne": False},
                        "status": {"$ne": "not_interested"},
                    },
                ]
            }
        )

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
                "current_status": 1,
                "final_status": 1,
                "status": {"$ifNull": ["$current_status", "$status"]},
                "is_interested": {"$ifNull": ["$application_details.interested", "$is_interested"]},
                "applied_at": 1,
                "application_details": 1,
                "skills": {"$ifNull": ["$application_details.self_assessment", "$skills"]},
                "github_link": {"$ifNull": ["$application_details.github_link", "$github_link"]},
                "project_link": {"$ifNull": ["$application_details.project_link", "$project_link"]},
                "resume_link": {"$ifNull": ["$application_details.submitted_resume_url", "$resume_link"]},
                "shortlist": 1,
                # The company's per-candidate remark, shown to the student so they
                # can act on it. Only surfaced when marked visible_to_student.
                "screening_remark": {
                    "$cond": [
                        {"$eq": ["$screening.visible_to_student", True]},
                        "$screening.remark",
                        None,
                    ]
                },
                "screening_decision": {
                    "$cond": [
                        {"$eq": ["$screening.visible_to_student", True]},
                        "$screening.decision",
                        None,
                    ]
                },
                "not_interested_reason": {
                    "$ifNull": ["$application_details.non_interest_reason", "$not_interested_reason"]
                },
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
    actual_applications = [item for item in applications if is_real_application(item)]
    total = len(actual_applications)
    shortlisted = sum(1 for item in actual_applications if item.get("status") in {"SHORTLISTED", "shortlisted"})
    rejected = sum(1 for item in actual_applications if item.get("status") in {"REJECTED", "rejected"})
    hired = sum(1 for item in actual_applications if item.get("status") in {"SELECTED", "JOINED", "hired"})
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
    applications = [application for application in response_records if is_real_application(application)]
    summary = build_summary(response_records)
    recent_applications = applications[:5]
    shortlisted_applications = [
        application for application in applications if application.get("status") in {"SHORTLISTED", "shortlisted"}
    ]
    return {
        "summary": summary,
        "applications": applications,
        "response_records": response_records,
        "recent_applications": recent_applications,
        "shortlisted_applications": shortlisted_applications,
        "status_labels": STATUS_LABELS,
    }

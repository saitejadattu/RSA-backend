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
                # Raw screening + whether the opening's shortlist has been
                # processed. These drive the student-facing outcome computed in
                # Python below, then are stripped so nothing sensitive (e.g. a
                # waitlist note) ever reaches the student.
                "_decision": "$screening.decision",
                "_remark": "$screening.remark",
                "_shortlist_done": {"$cond": [{"$ifNull": ["$opportunity.shortlist_imported_at", False]}, True, False]},
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
                    "student_side_status": "$opportunity.student_side_status",
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
    for application in applications:
        _apply_student_outcome(application)
    return serialize_mongo(applications)


# Screening decisions that mean the profile was passed over at the resume stage.
# "waitlisted" is deliberately NOT here: a waitlisted student is a second-priority
# backup who may still be pulled in, so the student is never shown that status.
_REJECT_DECISIONS = {"not_shortlisted", "selected_elsewhere", "resume_not_found"}
_FORWARD_STATUSES = {"SELECTED", "JOINED", "OFFER_ACCEPTED", "OFFER_RELEASED", "OFFER_PENDING"}
_DEFAULT_REJECTION_REMARK = "Profile does not align with internship requirements"
_NO_STUDENT_ELIGIBLE = "No Student Eligble"
_REMARK_STATUS_CONTEXTS = {_NO_STUDENT_ELIGIBLE, "Shared Profiles with CRM"}


def _apply_student_outcome(application: dict) -> None:
    """Derive the single status a student should see, and gate the remark.

    - waitlisted -> shown as still 'pending' (never 'waitlisted'), remark hidden.
    - not on the shortlist sheet once it's been imported -> 'not_shortlisted'.
    - a resume-stage reject remark -> 'not_shortlisted', remark shown.
    Only reject remarks reach the student; the raw fields are stripped here.
    """
    status = str(application.get("status") or "APPLIED").upper()
    decision = str(application.get("_decision") or "").lower()
    shortlist_done = bool(application.get("_shortlist_done"))
    remark = application.get("_remark")
    opportunity = application.get("opportunity") or {}
    student_side_status = str(opportunity.get("student_side_status") or "").strip()

    if application.get("is_interested") is False or status in {"DROPPED", "NOT_INTERESTED"}:
        outcome = "declined"
    elif status == "REJECTED":
        outcome = "rejected"
    elif status == "INTERVIEW_COMPLETED":
        outcome = "interview_done"
    elif status == "INTERVIEW_NOT_ATTENDED":
        outcome = "not_attended"
    elif "INTERVIEW" in status:
        outcome = "interviewing"
    elif status == "SHORTLISTED":
        outcome = "shortlisted"
    elif status in _FORWARD_STATUSES:
        outcome = "selected"
    elif status == "NOT_SHORTLISTED":
        outcome = "not_shortlisted"
    elif decision == "waitlisted":
        outcome = "pending"
    elif decision in _REJECT_DECISIONS:
        outcome = "not_shortlisted"
    elif status == "APPLIED" and student_side_status == _NO_STUDENT_ELIGIBLE:
        outcome = "not_shortlisted"
    elif shortlist_done and status == "APPLIED":
        outcome = "not_shortlisted"
    else:
        outcome = "pending"

    show_remark = outcome in {"not_shortlisted", "rejected"} or (
        status == "APPLIED" and student_side_status in _REMARK_STATUS_CONTEXTS
    )
    application["student_outcome"] = outcome
    has_remark = isinstance(remark, str) and bool(remark.strip())
    application["screening_remark"] = (
        remark
        if has_remark and show_remark
        else _DEFAULT_REJECTION_REMARK
        if status == "APPLIED" and student_side_status == _NO_STUDENT_ELIGIBLE
        else None
    )
    application["screening_decision"] = decision if show_remark else None
    for key in ("_decision", "_remark", "_shortlist_done"):
        application.pop(key, None)


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

from app.db.collections import APPLICATIONS, COMPANIES, HIRING_OPPORTUNITIES, STUDENTS
from app.db.mongodb import get_database
from app.models.application import normalize_application_status
from app.utils.mongo import serialize_mongo


REAL_APPLICATION_FILTER = {
    "$or": [
        {"application_details.interested": {"$exists": True, "$ne": False}},
        {
            "application_details": {"$exists": False},
            "is_interested": {"$ne": False},
            "status": {"$ne": "not_interested"},
        },
    ]
}
NOT_INTERESTED_FILTER = {
    "$or": [
        {"application_details.interested": False},
        {"application_details": {"$exists": False}, "is_interested": False},
        {"application_details": {"$exists": False}, "status": "not_interested"},
    ]
}


async def get_admin_dashboard() -> dict:
    db = get_database()

    total_students = await db[STUDENTS].count_documents({})
    total_companies = await db[COMPANIES].count_documents({})
    total_opportunities = await db[HIRING_OPPORTUNITIES].count_documents({})
    response_count = await db[APPLICATIONS].count_documents({})
    total_applications = await db[APPLICATIONS].count_documents(REAL_APPLICATION_FILTER)
    not_interested_count = await db[APPLICATIONS].count_documents(NOT_INTERESTED_FILTER)
    shortlisted_count = await db[APPLICATIONS].count_documents(
        {"$or": [{"current_status": "SHORTLISTED"}, {"current_status": {"$exists": False}, "status": "shortlisted"}]}
    )
    rejected_count = await db[APPLICATIONS].count_documents(
        {"$or": [{"current_status": "REJECTED"}, {"current_status": {"$exists": False}, "status": "rejected"}]}
    )
    hired_count = await db[APPLICATIONS].count_documents(
        {
            "$or": [
                {"current_status": {"$in": ["SELECTED", "JOINED"]}},
                {"current_status": {"$exists": False}, "status": "hired"},
            ]
        }
    )

    status_breakdown = await db[APPLICATIONS].aggregate(
        [
            {"$group": {"_id": {"$ifNull": ["$current_status", "$status"]}, "count": {"$sum": 1}}},
            {"$sort": {"count": -1, "_id": 1}},
        ]
    ).to_list(length=None)

    recent_applications = await list_recent_applications(limit=8)
    opportunity_pipeline = [
        {
            "$lookup": {
                "from": APPLICATIONS,
                "localField": "_id",
                "foreignField": "opportunity_id",
                "as": "applications",
            }
        },
        {
            "$lookup": {
                "from": COMPANIES,
                "localField": "company_id",
                "foreignField": "_id",
                "as": "company",
            }
        },
        {"$unwind": {"path": "$company", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "role": 1,
                "tech_stack": 1,
                "must_have_skills": 1,
                "location": 1,
                "stipend": 1,
                "duration": 1,
                "company_status": 1,
                "opportunity_received_at": 1,
                "company": {"_id": "$company._id", "name": "$company.name"},
                "response_count": {"$size": "$applications"},
                "application_count": {
                    "$size": {
                        "$filter": {
                            "input": "$applications",
                            "as": "application",
                            "cond": {
                                "$and": [
                                    {
                                        "$ne": [
                                            {
                                                "$ifNull": [
                                                    "$$application.application_details.interested",
                                                    "$$application.is_interested",
                                                ]
                                            },
                                            False,
                                        ]
                                    },
                                    {"$ne": [{"$ifNull": ["$$application.current_status", "$$application.status"]}, "not_interested"]},
                                ]
                            },
                        }
                    }
                },
                "shortlisted_count": {
                    "$size": {
                        "$filter": {
                            "input": "$applications",
                            "as": "application",
                            "cond": {
                                "$in": [
                                    {"$ifNull": ["$$application.current_status", "$$application.status"]},
                                    ["SHORTLISTED", "shortlisted"],
                                ]
                            },
                        }
                    }
                },
            }
        },
        {"$sort": {"opportunity_received_at": -1, "updated_at": -1}},
        {"$limit": 500},
    ]
    recent_opportunities = await db[HIRING_OPPORTUNITIES].aggregate(opportunity_pipeline).to_list(length=None)

    repeated_companies = await db[HIRING_OPPORTUNITIES].aggregate(
        [
            {
                "$group": {
                    "_id": "$company_id",
                    "opportunity_count": {"$sum": 1},
                    "roles": {"$addToSet": "$role"},
                    "last_received_at": {"$max": "$opportunity_received_at"},
                }
            },
            {"$match": {"opportunity_count": {"$gt": 1}}},
            {"$lookup": {"from": COMPANIES, "localField": "_id", "foreignField": "_id", "as": "company"}},
            {"$unwind": "$company"},
            {
                "$project": {
                    "company": {"_id": "$company._id", "name": "$company.name"},
                    "opportunity_count": 1,
                    "roles": 1,
                    "last_received_at": 1,
                }
            },
            {"$sort": {"opportunity_count": -1, "last_received_at": -1}},
            {"$limit": 8},
        ]
    ).to_list(length=None)

    return serialize_mongo(
        {
            "summary": {
                "total_students": total_students,
                "total_companies": total_companies,
                "total_opportunities": total_opportunities,
                "response_count": response_count,
                "total_applications": total_applications,
                "not_interested_count": not_interested_count,
                "shortlisted_count": shortlisted_count,
                "interview_ready_count": shortlisted_count,
                "rejected_count": rejected_count,
                "hired_count": hired_count,
            },
            "status_breakdown": [{"status": item["_id"] or "unknown", "count": item["count"]} for item in status_breakdown],
            "recent_applications": recent_applications,
            "recent_opportunities": recent_opportunities,
            "repeated_companies": repeated_companies,
        }
    )


async def list_recent_applications(limit: int = 50, status_value: str | None = None) -> list[dict]:
    db = get_database()
    match_stage = dict(REAL_APPLICATION_FILTER)
    if status_value:
        normalized_status = normalize_application_status(status_value)
        match_stage["$and"] = [
            {
                "$or": [
                    {"current_status": normalized_status},
                    {"current_status": {"$exists": False}, "status": status_value},
                ]
            }
        ]

    pipeline = [
        {"$match": match_stage},
        {"$sort": {"applied_at": -1, "created_at": -1}},
        {"$limit": limit},
        {"$lookup": {"from": STUDENTS, "localField": "student_id", "foreignField": "_id", "as": "student"}},
        {"$unwind": {"path": "$student", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {"from": COMPANIES, "localField": "company_id", "foreignField": "_id", "as": "company"}},
        {"$unwind": {"path": "$company", "preserveNullAndEmptyArrays": True}},
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
                "student": {
                    "_id": "$student._id",
                    "name": "$student.name",
                    "email": "$student.email",
                    "phone": "$student.phone",
                    "college": "$student.college",
                    "degree": "$student.degree",
                    "department": "$student.department",
                    "year_of_passing": "$student.year_of_passing",
                },
                "company": {"_id": "$company._id", "name": "$company.name"},
                "opportunity": {
                    "_id": "$opportunity._id",
                    "role": "$opportunity.role",
                    "tech_stack": "$opportunity.tech_stack",
                    "must_have_skills": "$opportunity.must_have_skills",
                    "location": "$opportunity.location",
                    "stipend": "$opportunity.stipend",
                    "duration": "$opportunity.duration",
                    "opportunity_received_at": "$opportunity.opportunity_received_at",
                },
            }
        },
    ]
    applications = await db[APPLICATIONS].aggregate(pipeline).to_list(length=limit)
    return serialize_mongo(applications)


async def list_admin_students(limit: int = 500) -> list[dict]:
    db = get_database()
    students = await db[STUDENTS].find({}).sort("name", 1).limit(limit).to_list(length=limit)
    student_ids = [student["_id"] for student in students]
    if not student_ids:
        return []

    application_pipeline = [
        {"$match": {"student_id": {"$in": student_ids}, **REAL_APPLICATION_FILTER}},
        {"$sort": {"applied_at": -1, "created_at": -1}},
        {"$lookup": {"from": COMPANIES, "localField": "company_id", "foreignField": "_id", "as": "company"}},
        {"$unwind": {"path": "$company", "preserveNullAndEmptyArrays": True}},
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
            "$project": {
                "student_id": 1,
                "current_status": 1,
                "final_status": 1,
                "status": {"$ifNull": ["$current_status", "$status"]},
                "applied_at": 1,
                "resume_link": {"$ifNull": ["$application_details.submitted_resume_url", "$resume_link"]},
                "github_link": {"$ifNull": ["$application_details.github_link", "$github_link"]},
                "project_link": {"$ifNull": ["$application_details.project_link", "$project_link"]},
                "company": {"_id": "$company._id", "name": "$company.name"},
                "opportunity": {
                    "_id": "$opportunity._id",
                    "role": "$opportunity.role",
                    "tech_stack": "$opportunity.tech_stack",
                    "must_have_skills": "$opportunity.must_have_skills",
                    "location": "$opportunity.location",
                    "opportunity_received_at": "$opportunity.opportunity_received_at",
                },
            }
        },
    ]
    applications = await db[APPLICATIONS].aggregate(application_pipeline).to_list(length=None)

    grouped: dict[str, list[dict]] = {}
    for application in applications:
        grouped.setdefault(str(application["student_id"]), []).append(application)

    student_rows = []
    for student in students:
        student_applications = grouped.get(str(student["_id"]), [])
        shortlisted = [item for item in student_applications if item.get("status") in {"SHORTLISTED", "shortlisted"}]
        not_shortlisted = [item for item in student_applications if item.get("status") not in {"SHORTLISTED", "shortlisted"}]
        student_rows.append(
            {
                "_id": student["_id"],
                "name": student.get("name"),
                "email": student.get("email"),
                "phone": student.get("phone"),
                "college": student.get("college"),
                "degree": student.get("degree"),
                "department": student.get("department"),
                "year_of_passing": student.get("year_of_passing"),
                "application_count": len(student_applications),
                "shortlisted_count": len(shortlisted),
                "not_shortlisted_count": len(not_shortlisted),
                "applications": student_applications,
                "shortlisted_applications": shortlisted,
                "not_shortlisted_applications": not_shortlisted,
            }
        )

    return serialize_mongo(student_rows)


SKILL_KEYS = ("python", "nodejs", "react", "mongodb", "sql", "dsa", "javascript")


async def get_admin_analytics() -> dict:
    """Aggregated metrics for the admin analytics dashboard, read off the
    normalized applications schema (current_status / application_details / placement)."""
    db = get_database()

    totals = {
        "students": await db[STUDENTS].count_documents({}),
        "companies": await db[COMPANIES].count_documents({}),
        "opportunities": await db[HIRING_OPPORTUNITIES].count_documents({}),
        "applications": await db[APPLICATIONS].count_documents({}),
    }

    status_raw = await db[APPLICATIONS].aggregate(
        [
            {"$group": {"_id": {"$ifNull": ["$current_status", "$status"]}, "n": {"$sum": 1}}},
            {"$sort": {"n": -1, "_id": 1}},
        ]
    ).to_list(length=None)
    status = [{"key": row["_id"] or "UNKNOWN", "n": row["n"]} for row in status_raw]

    by_month_raw = await db[APPLICATIONS].aggregate(
        [
            {"$match": {"applied_at": {"$ne": None}}},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m", "date": "$applied_at"}}, "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
    ).to_list(length=None)
    by_month = [{"month": row["_id"], "n": row["n"]} for row in by_month_raw if row["_id"]]

    top_companies_raw = await db[APPLICATIONS].aggregate(
        [
            {"$group": {"_id": "$company_id", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
            {"$limit": 10},
            {"$lookup": {"from": COMPANIES, "localField": "_id", "foreignField": "_id", "as": "c"}},
            {"$unwind": {"path": "$c", "preserveNullAndEmptyArrays": True}},
            {"$project": {"_id": 0, "name": "$c.name", "n": 1}},
        ]
    ).to_list(length=None)
    top_companies = [{"name": row.get("name") or "Unknown", "n": row["n"]} for row in top_companies_raw]

    skills = []
    for skill in SKILL_KEYS:
        field = f"application_details.self_assessment.{skill}"
        res = await db[APPLICATIONS].aggregate(
            [
                {"$match": {field: {"$type": "number"}}},
                {"$group": {"_id": None, "avg": {"$avg": f"${field}"}, "n": {"$sum": 1}}},
            ]
        ).to_list(length=1)
        skills.append(
            {
                "key": skill,
                "avg": round(res[0]["avg"], 2) if res else None,
                "n": res[0]["n"] if res else 0,
            }
        )
    skills.sort(key=lambda item: item["avg"] or 0, reverse=True)

    interested = await db[APPLICATIONS].count_documents({"application_details.interested": True})
    not_interested = await db[APPLICATIONS].count_documents({"application_details.interested": False})

    offer_raw = await db[APPLICATIONS].aggregate(
        [
            {"$match": {"placement.offer_letter.status": {"$ne": None}}},
            {"$group": {"_id": "$placement.offer_letter.status", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ]
    ).to_list(length=None)
    internship_raw = await db[APPLICATIONS].aggregate(
        [
            {"$match": {"placement.internship.status": {"$ne": None}}},
            {"$group": {"_id": "$placement.internship.status", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ]
    ).to_list(length=None)

    placed = await db[APPLICATIONS].count_documents({"placement.selected": True})
    shortlisted = await db[APPLICATIONS].count_documents(
        {"$or": [{"current_status": "SHORTLISTED"}, {"current_status": {"$exists": False}, "status": "shortlisted"}]}
    )
    joined = await db[APPLICATIONS].count_documents({"current_status": "JOINED"})

    funnel = [
        {"key": "Responses", "n": totals["applications"]},
        {"key": "Interested", "n": interested},
        {"key": "Shortlisted", "n": shortlisted},
        {"key": "Selected / placed", "n": placed},
        {"key": "Joined", "n": joined},
    ]

    return {
        "totals": totals,
        "funnel": funnel,
        "status": status,
        "by_month": by_month,
        "top_companies": top_companies,
        "skills": skills,
        "interest": {"interested": interested, "not_interested": not_interested},
        "offer_status": [{"key": row["_id"], "n": row["n"]} for row in offer_raw],
        "internship_status": [{"key": row["_id"], "n": row["n"]} for row in internship_raw],
        "placed": placed,
    }

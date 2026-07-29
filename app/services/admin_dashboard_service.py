import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status

from app.db.collections import APPLICATIONS, COMPANIES, HIRING_OPPORTUNITIES, INTERVIEW_REPORTS, STUDENTS
from app.db.mongodb import get_database
from app.models.application import normalize_application_status
from app.utils.mongo import serialize_mongo
from app.utils.object_id import to_object_id


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


# status expressions reused across aggregations
_STATUS = {"$ifNull": ["$current_status", "$status"]}
_INTERESTED = {"$ifNull": ["$application_details.interested", "$is_interested"]}
_SHORTLISTED_SET = ["SHORTLISTED", "shortlisted"]
_SELECTED_SET = ["SELECTED", "JOINED", "hired"]


def categorize_role(role: str | None, skills: str | None = None) -> str:
    """Bucket an opening by role/skills text. AI is checked first so an AI-flavoured
    full-stack role lands in AI, which is the split the dashboard cares about."""
    text = f"{role or ''} | {skills or ''}".lower()

    def has(*words: str) -> bool:
        return any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", text) for w in words)

    if has("ai", "ml", "genai", "gen ai", "ai/ml", "llm", "rag", "agentic", "nlp") or (
        "machine learning" in text or "artificial intelligence" in text or "data scien" in text or "computer vision" in text
    ):
        return "AI / ML"
    if has("mern") or ("full stack" in text or "fullstack" in text or "full-stack" in text) or (has("react") and has("node")):
        return "MERN / Full Stack"
    if has("frontend", "react", "nextjs") or ("front end" in text or "front-end" in text or "next.js" in text):
        return "Frontend"
    if has("python", "django", "flask", "fastapi", "backend", "node") or ("back end" in text or "node.js" in text):
        return "Python / Backend"
    return "Other"


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _resolve_range(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end_dt = _parse_date(end)
    end_dt = (end_dt + timedelta(days=1)) if end_dt else now  # end date is inclusive
    start_dt = _parse_date(start) or end_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_dt, end_dt


async def get_admin_analytics(start: str | None = None, end: str | None = None) -> dict:
    """Analytics for a date window (defaults to the current month), read off the
    normalized applications schema. Every metric below respects [start, end)."""
    db = get_database()
    start_dt, end_dt = _resolve_range(start, end)
    in_range = {"applied_at": {"$gte": start_dt, "$lt": end_dt}}

    totals = {
        "students": await db[STUDENTS].count_documents({}),
        "companies": await db[COMPANIES].count_documents({}),
        "opportunities": await db[HIRING_OPPORTUNITIES].count_documents({}),
        "applications": await db[APPLICATIONS].count_documents({}),
    }

    # ---- headline KPIs for the window (one pass) ----
    kpi_rows = await db[APPLICATIONS].aggregate(
        [
            {"$match": in_range},
            {
                "$group": {
                    "_id": None,
                    "applications": {"$sum": 1},
                    "students": {"$addToSet": "$student_id"},
                    "companies": {"$addToSet": "$company_id"},
                    "opportunities": {"$addToSet": "$opportunity_id"},
                    "interested": {"$sum": {"$cond": [{"$ne": [_INTERESTED, False]}, 1, 0]}},
                    "shortlisted": {"$sum": {"$cond": [{"$in": [_STATUS, _SHORTLISTED_SET]}, 1, 0]}},
                    "selected": {"$sum": {"$cond": [{"$in": [_STATUS, _SELECTED_SET]}, 1, 0]}},
                }
            },
        ]
    ).to_list(length=1)
    row = kpi_rows[0] if kpi_rows else {}
    applications = row.get("applications", 0)
    interested = row.get("interested", 0)
    shortlisted = row.get("shortlisted", 0)
    kpis = {
        "applications": applications,
        "students": len(row.get("students", [])),
        "companies": len(row.get("companies", [])),
        "opportunities": len(row.get("opportunities", [])),
        "interested": interested,
        "shortlisted": shortlisted,
        "selected": row.get("selected", 0),
        "interest_rate": round(interested / applications * 100, 1) if applications else 0,
        "shortlist_rate": round(shortlisted / interested * 100, 1) if interested else 0,
        "new_students": await db[STUDENTS].count_documents({"created_at": {"$gte": start_dt, "$lt": end_dt}}),
        "new_opportunities": await db[HIRING_OPPORTUNITIES].count_documents(
            {"opportunity_received_at": {"$gte": start_dt, "$lt": end_dt}}
        ),
    }

    # ---- daily trend (applications + distinct students per day) ----
    daily_raw = await db[APPLICATIONS].aggregate(
        [
            {"$match": in_range},
            {
                "$group": {
                    "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$applied_at"}},
                    "apps": {"$sum": 1},
                    "students": {"$addToSet": "$student_id"},
                }
            },
            {"$project": {"_id": 0, "date": "$_id", "apps": 1, "students": {"$size": "$students"}}},
            {"$sort": {"date": 1}},
        ]
    ).to_list(length=None)

    # ---- status breakdown in window ----
    status_raw = await db[APPLICATIONS].aggregate(
        [
            {"$match": in_range},
            {"$group": {"_id": _STATUS, "n": {"$sum": 1}}},
            {"$sort": {"n": -1, "_id": 1}},
        ]
    ).to_list(length=None)
    status = [{"key": r["_id"] or "UNKNOWN", "n": r["n"]} for r in status_raw]

    # ---- per-student: how many opportunities each applied to ----
    per_student = await db[APPLICATIONS].aggregate(
        [
            {"$match": in_range},
            {
                "$group": {
                    "_id": "$student_id",
                    "apps": {"$sum": 1},
                    "shortlisted": {"$sum": {"$cond": [{"$in": [_STATUS, _SHORTLISTED_SET]}, 1, 0]}},
                }
            },
        ]
    ).to_list(length=None)

    buckets = {"1": 0, "2": 0, "3": 0, "4": 0, "5+": 0}
    for entry in per_student:
        n = entry["apps"]
        buckets["5+" if n >= 5 else str(n)] += 1
    apps_per_student = {
        "buckets": [{"label": label, "n": count} for label, count in buckets.items()],
        "avg": round(applications / len(per_student), 2) if per_student else 0,
        "max": max((e["apps"] for e in per_student), default=0),
    }

    top_ids = sorted(per_student, key=lambda e: (-e["apps"], -e["shortlisted"]))[:10]
    top_oids = [e["_id"] for e in top_ids]
    id_to_name: dict = {}
    apps_by_student: dict = {}
    if top_oids:
        students_docs = await db[STUDENTS].find({"_id": {"$in": top_oids}}, {"name": 1}).to_list(length=None)
        id_to_name = {s["_id"]: s for s in students_docs}
        app_rows = await db[APPLICATIONS].aggregate(
            [
                {"$match": {**in_range, "student_id": {"$in": top_oids}}},
                {"$lookup": {"from": HIRING_OPPORTUNITIES, "localField": "opportunity_id", "foreignField": "_id", "as": "opp"}},
                {"$unwind": {"path": "$opp", "preserveNullAndEmptyArrays": True}},
                {"$lookup": {"from": COMPANIES, "localField": "company_id", "foreignField": "_id", "as": "co"}},
                {"$unwind": {"path": "$co", "preserveNullAndEmptyArrays": True}},
                {"$project": {"student_id": 1, "applied_at": 1, "status": _STATUS, "role": "$opp.role", "skills": "$opp.must_have_skills", "company": "$co.name"}},
                {"$sort": {"applied_at": -1}},
            ]
        ).to_list(length=None)
        for a in app_rows:
            apps_by_student.setdefault(a["student_id"], []).append(
                {
                    "company": a.get("company") or "Unknown",
                    "role": a.get("role") or "—",
                    "category": categorize_role(a.get("role"), a.get("skills")),
                    "status": a.get("status") or "APPLIED",
                    "applied_at": a.get("applied_at"),
                }
            )
    top_students = [
        {
            "id": e["_id"],
            "name": (id_to_name.get(e["_id"], {}) or {}).get("name") or "Unknown",
            "apps": e["apps"],
            "shortlisted": e["shortlisted"],
            "applications": apps_by_student.get(e["_id"], []),
        }
        for e in top_ids
    ]

    # ---- top companies by applications in window ----
    top_companies_raw = await db[APPLICATIONS].aggregate(
        [
            {"$match": in_range},
            {"$group": {"_id": "$company_id", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
            {"$limit": 8},
            {"$lookup": {"from": COMPANIES, "localField": "_id", "foreignField": "_id", "as": "c"}},
            {"$unwind": {"path": "$c", "preserveNullAndEmptyArrays": True}},
            {"$project": {"_id": 0, "name": "$c.name", "n": 1}},
        ]
    ).to_list(length=None)
    top_companies = [{"name": r.get("name") or "Unknown", "n": r["n"]} for r in top_companies_raw]

    funnel = [
        {"key": "Applied", "n": applications},
        {"key": "Interested", "n": interested},
        {"key": "Shortlisted", "n": shortlisted},
        {"key": "Selected", "n": kpis["selected"]},
    ]

    # ---- openings in window grouped by role category (AI / MERN / Python / …) ----
    opp_cat_rows = await db[APPLICATIONS].aggregate(
        [
            {"$match": in_range},
            {
                "$group": {
                    "_id": "$opportunity_id",
                    "apps": {"$sum": 1},
                    "shortlisted": {"$sum": {"$cond": [{"$in": [_STATUS, _SHORTLISTED_SET]}, 1, 0]}},
                    "company_id": {"$first": "$company_id"},
                }
            },
            {"$lookup": {"from": HIRING_OPPORTUNITIES, "localField": "_id", "foreignField": "_id", "as": "opp"}},
            {"$unwind": {"path": "$opp", "preserveNullAndEmptyArrays": True}},
            {"$lookup": {"from": COMPANIES, "localField": "company_id", "foreignField": "_id", "as": "co"}},
            {"$unwind": {"path": "$co", "preserveNullAndEmptyArrays": True}},
            {"$project": {"apps": 1, "shortlisted": 1, "role": "$opp.role", "skills": "$opp.must_have_skills", "company": "$co.name"}},
        ]
    ).to_list(length=None)
    cats: dict = {}
    for r in opp_cat_rows:
        cat = categorize_role(r.get("role"), r.get("skills"))
        bucket = cats.setdefault(cat, {"category": cat, "opportunities": 0, "applications": 0, "shortlisted": 0, "companies": []})
        bucket["opportunities"] += 1
        bucket["applications"] += r.get("apps", 0)
        bucket["shortlisted"] += r.get("shortlisted", 0)
        bucket["companies"].append(
            {"company": r.get("company") or "Unknown", "role": r.get("role") or "—", "apps": r.get("apps", 0), "shortlisted": r.get("shortlisted", 0)}
        )
    for bucket in cats.values():
        bucket["companies"].sort(key=lambda x: -x["apps"])
    role_categories = sorted(cats.values(), key=lambda x: -x["opportunities"])

    # ---- all-time monthly context (unfiltered), for the trend backdrop ----
    by_month_raw = await db[APPLICATIONS].aggregate(
        [
            {"$match": {"applied_at": {"$ne": None}}},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m", "date": "$applied_at"}}, "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
    ).to_list(length=None)
    by_month = [{"month": r["_id"], "n": r["n"]} for r in by_month_raw if r["_id"]]

    return serialize_mongo(
        {
            "range": {"start": start_dt, "end": end_dt - timedelta(days=1)},
            "totals": totals,
            "kpis": kpis,
            "daily": daily_raw,
            "status": status,
            "funnel": funnel,
            "apps_per_student": apps_per_student,
            "top_students": top_students,
            "top_companies": top_companies,
            "role_categories": role_categories,
            "by_month": by_month,
        }
    )


async def get_admin_student_detail(student_id: str) -> dict:
    """Everything about one student: profile, pipeline stats, every application
    (with company/role/status/links/reason), role mix, and interview reports."""
    db = get_database()
    try:
        oid = to_object_id(student_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid student id")

    student = await db[STUDENTS].find_one({"_id": oid})
    if not student:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found")

    app_rows = await db[APPLICATIONS].aggregate(
        [
            {"$match": {"student_id": oid}},
            {"$sort": {"applied_at": -1, "created_at": -1}},
            {"$lookup": {"from": COMPANIES, "localField": "company_id", "foreignField": "_id", "as": "co"}},
            {"$unwind": {"path": "$co", "preserveNullAndEmptyArrays": True}},
            {"$lookup": {"from": HIRING_OPPORTUNITIES, "localField": "opportunity_id", "foreignField": "_id", "as": "opp"}},
            {"$unwind": {"path": "$opp", "preserveNullAndEmptyArrays": True}},
            {
                "$project": {
                    "status": _STATUS,
                    "interested": _INTERESTED,
                    "applied_at": 1,
                    "resume_link": {"$ifNull": ["$application_details.submitted_resume_url", "$resume_link"]},
                    "github_link": {"$ifNull": ["$application_details.github_link", "$github_link"]},
                    "project_link": {"$ifNull": ["$application_details.project_link", "$project_link"]},
                    "interest_reason": "$application_details.interest_reason",
                    "non_interest_reason": "$application_details.non_interest_reason",
                    "company": "$co.name",
                    "company_id": "$co._id",
                    "role": "$opp.role",
                    "skills": "$opp.must_have_skills",
                    "opportunity_id": "$opp._id",
                }
            },
        ]
    ).to_list(length=None)

    stats = {"responses": 0, "interested": 0, "shortlisted": 0, "selected": 0, "declined": 0}
    role_counts: dict = {}
    applications = []
    for a in app_rows:
        st = a.get("status") or "APPLIED"
        interested = a.get("interested") is not False
        stats["responses"] += 1
        stats["interested" if interested else "declined"] += 1
        if st in _SHORTLISTED_SET:
            stats["shortlisted"] += 1
        if st in _SELECTED_SET:
            stats["selected"] += 1
        cat = categorize_role(a.get("role"), a.get("skills"))
        if interested:
            role_counts[cat] = role_counts.get(cat, 0) + 1
        applications.append(
            {
                "company": a.get("company") or "Unknown",
                "company_id": a.get("company_id"),
                "opportunity_id": a.get("opportunity_id"),
                "role": a.get("role") or "—",
                "category": cat,
                "status": st,
                "interested": interested,
                "applied_at": a.get("applied_at"),
                "resume_link": a.get("resume_link"),
                "github_link": a.get("github_link"),
                "project_link": a.get("project_link"),
                "interest_reason": a.get("interest_reason"),
                "non_interest_reason": a.get("non_interest_reason"),
            }
        )
    role_breakdown = sorted([{"category": k, "n": v} for k, v in role_counts.items()], key=lambda x: -x["n"])

    reports = await db[INTERVIEW_REPORTS].aggregate(
        [
            {"$match": {"student_id": oid}},
            {"$sort": {"generated_at": -1, "created_at": -1}},
            {"$lookup": {"from": COMPANIES, "localField": "company_id", "foreignField": "_id", "as": "co"}},
            {"$unwind": {"path": "$co", "preserveNullAndEmptyArrays": True}},
            {"$lookup": {"from": HIRING_OPPORTUNITIES, "localField": "opportunity_id", "foreignField": "_id", "as": "opp"}},
            {"$unwind": {"path": "$opp", "preserveNullAndEmptyArrays": True}},
            {
                "$project": {
                    "overall": 1,
                    "communication": 1,
                    "strengths": 1,
                    "improvements": 1,
                    "skill_ratings": 1,
                    "interviewer_feedback": 1,
                    "visible_to_student": 1,
                    "generated_at": 1,
                    "company": "$co.name",
                    "role": "$opp.role",
                }
            },
        ]
    ).to_list(length=None)

    return serialize_mongo(
        {
            "student": {
                "id": student["_id"],
                "name": student.get("name"),
                "email": student.get("email"),
                "phone": student.get("phone"),
                "college_name": student.get("college_name"),
                "current_city": student.get("current_city"),
                "degree": student.get("degree"),
                "department": student.get("department"),
                "year_of_passing": student.get("year_of_passing"),
                "resume_link": student.get("resume_link"),
                "technical_developer_name": student.get("technical_developer_name"),
                "placed_status": student.get("placed_status"),
                "external_user_id": student.get("external_user_id"),
                "created_at": student.get("created_at"),
            },
            "stats": stats,
            "applications": applications,
            "role_breakdown": role_breakdown,
            "reports": reports,
        }
    )

"""Hand-made Questlight candidates, in the shape of GET /api/applicants/findAll rows, so the tests never touch the real
candidate list. Like the real one they carry contact details, a date of birth and a password-style `otp`, which must never
get past the loader."""


def person(_id, code, name, status, skills, titles, duty, degrees, years, address, deleted=False):
    return {"_id": _id, "applicantId": code, "name": name, "status": status, "address": address, "skills": list(skills),
            "workExperience": [{"jobTitle": t, "companyName": "Acme", "responsibilities": [duty]} for t in titles],
            "education": [{"degree": d, "institution": "X University"} for d in degrees],
            "total_experience": {"years": years, "months": 0}, "isDeleted": False,
            "deleted_at": "2026-01-01T00:00:00Z" if deleted else None,
            "email": f"{code.lower()}@example.com", "phoneNumber": "+91-9000000000", "dateOfBirth": "1990-01-01T00:00:00Z",
            "otp": "$2a$10$notarealhashnotarealhashnotarealhashnotarealhash"}


JAVA_DUTY = "Built microservices with Java and Spring Boot, REST APIs, PostgreSQL, deployed on AWS with Docker."

PEOPLE = [
    person("c-asha", "CAN-T-001", "Asha Rao", "NEW_CANDIDATE", ["Java", "Spring Boot", "SQL", "Docker"], ["Java Backend Developer"], JAVA_DUTY, ["B.Tech"], 6, "Bangalore, Karnataka"),
    person("c-placed", "CAN-T-002", "Dev Placed", "PLACED_WITH_US", ["Java", "Spring Boot", "SQL", "Docker"], ["Java Backend Developer"], JAVA_DUTY, ["B.Tech"], 6, "Bangalore, Karnataka"),
    person("c-gone", "CAN-T-003", "Gita Deleted", "NEW_CANDIDATE", ["Java", "Spring Boot", "SQL", "Docker"], ["Java Backend Developer"], JAVA_DUTY, ["B.Tech"], 6, "Bangalore, Karnataka", deleted=True),
    person("c-ravi", "CAN-T-004", "Ravi Sales", "SCREENING", ["Sales", "Negotiation", "CRM"], ["Sales Manager"], "Led regional sales for FMCG distribution, grew revenue, managed retail accounts.", ["MBA"], 8, "Mumbai, Maharashtra"),
    person("c-lena", "CAN-T-005", "Lena Lab", "INTERNAL_INTERVIEW", ["HPLC", "GC", "Documentation"], ["Laboratory Analyst"], "Pharma lab testing with HPLC and GC, reports and documentation.", ["M.Sc"], 3, "Kochi, Kerala"),
    person("c-junior", "CAN-T-006", "Jai Junior", "SCREENING", ["Java"], ["Intern"], "Wrote small Java programs.", ["B.Tech"], 1, "Delhi"),
    person("c-onb", "CAN-T-007", "Om Onboarding", "ONBOARDING_IN_PROCESS", ["Java", "Spring Boot", "SQL"], ["Java Developer"], JAVA_DUTY, ["B.Tech"], 5, "Bangalore, Karnataka"),
    person("c-ready", "CAN-T-008", "Rea Ready", "READY_FOR_ONBOARDING", ["Java", "Spring Boot", "SQL"], ["Java Developer"], JAVA_DUTY, ["B.Tech"], 5, "Bangalore, Karnataka"),
]
FILLER = [person(f"c-f{i}", f"CAN-F-{i:03d}", f"Filler {i}", "NEW_CANDIDATE", ["Excel"], ["Clerk"], "Data entry and filing.", ["B.Com"], 2, "Pune, Maharashtra") for i in range(130)]
ALL = PEOPLE + FILLER           # 138 rows: two pages of 100
IN_PLAY = len(ALL) - 1 - 3      # minus the deleted one; the placed, onboarding and ready-for-onboarding ones are left out of ranking

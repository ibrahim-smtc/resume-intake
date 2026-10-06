"""Hand-made parser output (the JSON a resume parser returns), so tests don't depend on any real parser. All invented."""
import copy

# A complete, ordinary resume.
PRIYA = {
    "name": "Priya Nair", "email": "priya.nair@example.com", "phoneNumber": "+91 98765 43210", "address": "Bengaluru",
    "dateOfBirth": "", "linkedInProfile": "https://linkedin.com/in/priya", "github": "https://github.com/priya",
    "portfolio": "", "technical_skills": ["Java", "Python", "java"], "soft_skills": ["Leadership"],
    "total_experience": {"years": 8, "months": 4},
    "workExperience": [
        {"companyName": "Acme", "jobTitle": "Senior Engineer", "location": "Bengaluru", "startDate": "2021-01-01",
         "endDate": "2026-10-03", "duration": {"years": 5, "months": 9}, "responsibilities": ["Built APIs"]},
        {"companyName": "Nimbus", "jobTitle": "Engineer", "location": "", "startDate": "2017-06-01",
         "endDate": "", "duration": {"years": 0, "months": 0}, "responsibilities": []}],
    "education": [{"degree": "B.Tech", "institution": "Anna University", "graduation_year": 2017}],
    "certifications": [{"certificationName": "AWS CP", "institution": "", "dateObtained": ""}],
}

# Three candidates for the job-matching tests.
JAVA = {"name": "Priya Nair", "email": "priya@example.com", "phoneNumber": "+91 98765 43210", "address": "Bengaluru, Karnataka",
        "technical_skills": ["Java", "Spring Boot", "Python", "Django", "PostgreSQL", "Docker", "Kubernetes", "AWS", "Git"],
        "soft_skills": [], "total_experience": {"years": 9, "months": 0},
        "workExperience": [{"jobTitle": "Senior Software Engineer", "companyName": "Acme", "startDate": "2021-01-01"},
                           {"jobTitle": "Software Engineer", "companyName": "Nimbus", "startDate": "2017-06-01"}],
        "education": [{"degree": "B.Tech in Computer Science", "institution": "Anna University", "graduation_year": 2017}]}
SALES = {"name": "Imran Sheikh", "email": "imran@example.com", "phoneNumber": "9876501234", "address": "Mumbai",
         "technical_skills": ["Negotiation", "Forecasting", "CRM", "Excel"], "soft_skills": [],
         "total_experience": {"years": 12, "months": 0},
         "workExperience": [{"jobTitle": "Regional Sales Manager", "companyName": "Hindustan Foods", "startDate": "2018-01-01"}],
         "education": [{"degree": "MBA Marketing", "institution": "NMIMS", "graduation_year": 2014}]}
CHEM = {"name": "Deepa Menon", "email": "deepa@example.com", "phoneNumber": "9000011111", "address": "Kochi",
        "technical_skills": ["HPLC", "GC", "documentation", "MS Office"], "soft_skills": [], "total_experience": {"years": 7, "months": 0},
        "workExperience": [{"jobTitle": "Lab Analyst", "companyName": "PharmaCore", "startDate": "2019-03-01"}],
        "education": [{"degree": "M.Sc Chemistry", "institution": "University of Kerala", "graduation_year": 2019}]}

# The agent tests: Rohit reads fine; Harish has two jobs without a title, so a profile can't be created until someone supplies them.
ROHIT = {
    "name": "Rohit Verma", "email": "rohit.verma.test1@example.com", "phoneNumber": "+91 90000 12345", "address": "Bengaluru, Karnataka, India",
    "technical_skills": ["Python", "Flask", "Django", "REST APIs", "PostgreSQL", "Docker", "AWS", "CI/CD", "GitHub Actions", "pytest", "Git", "Linux", "Redis"],
    "soft_skills": [], "total_experience": {"years": 7, "months": 4},
    "workExperience": [
        {"jobTitle": "Senior Python Backend Engineer", "companyName": "Nimbus Payments", "startDate": "2021-02-01", "endDate": "2026-09-01"},
        {"jobTitle": "Python Developer", "companyName": "Brightline Software", "startDate": "2019-06-01", "endDate": "2021-01-31"}],
    "education": [{"degree": "B.Tech in Computer Science and Engineering", "institution": "Some University", "graduation_year": 2019}],
}
HARISH = {
    "name": "Harish Ramalingam", "email": "harish.test@example.com", "phoneNumber": "+91 90000 00000",
    "technical_skills": ["Python", "Scala", "SQL"], "total_experience": {"years": 5, "months": 6},
    "workExperience": [
        {"jobTitle": "Senior Data Engineer", "companyName": "LatentView Analytics", "startDate": "2022-01-01", "endDate": "2025-01-01", "responsibilities": ["x"]},
        {"jobTitle": "", "companyName": "Walmart Global Tech India", "startDate": "2020-01-01", "endDate": "2021-12-31"},
        {"companyName": "Tata Consultancy Services", "startDate": "2019-01-01", "endDate": "2019-12-31"}],
    "education": [{"degree": "Bachelor of Mechanical Engineering", "institution": "Some University", "graduation_year": 2018}],
}


def variant(base: dict, **changes) -> dict:
    out = copy.deepcopy(base)
    out.update(changes)
    return out

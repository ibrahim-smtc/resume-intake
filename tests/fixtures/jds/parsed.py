"""What Questlight's JD parser (POST /parsing/jd) returns, for the tests. QA is a real answer from the dev parser for the
invented QA JD in generate.py; JAVA and BRIEF are hand-made in the same shape."""
import copy

QA = {
    "city": "Bengaluru", "closingDate": "", "country": "India", "customerName": "Northwind Test Labs",
    "comments": "Candidates with experience in Cypress, Playwright, and performance testing with JMeter are preferred.",
    "degree": ["B.E.", "B.Tech", "Computer Science"], "domain": "Software Testing", "industry": "IT",
    "isOpenForReferral": False, "jobDurationMonths": "", "jobPositionTitle": "Senior QA Automation Engineer", "jobStatus": "Active",
    "jobSummary": "The Senior QA Automation Engineer will design, build and maintain automated test suites for web and API products.",
    "jobType": "Full-time", "jobWorkHoursPerDay": "", "languages": ["Java"], "maxExperienceYears": 8,
    "maximumSalary": "1800000", "minExperienceYears": 4, "minimumSalary": "1200000", "noticePeriodMonths": "1",
    "numberOfOpenings": 2, "postingDate": "2026-10-06", "primarySkills": ["Java", "Selenium WebDriver", "REST Assured", "TestNG"],
    "priority": "", "skillDomains": ["QA Automation", "API Testing", "CI/CD"],
    "skillsRequired": ["Java", "Selenium WebDriver", "TestNG", "REST Assured", "Postman", "Jenkins", "JIRA"],
    "state": "Karnataka", "workAddress": "Bengaluru, Karnataka, India", "workMode": "Hybrid",
}

JAVA = {
    "city": "Bengaluru", "closingDate": "", "country": "India", "customerName": "ICICI Bank", "comments": "",
    "degree": ["B.Tech"], "domain": "Backend Development", "industry": "Banking", "jobPositionTitle": "Java Backend Developer",
    "jobStatus": "Active", "jobSummary": "Build microservices with Java and Spring Boot, REST APIs and PostgreSQL, deployed on AWS with Docker.",
    "jobType": "Full time", "languages": [], "maxExperienceYears": 8, "maximumSalary": "2200000", "minExperienceYears": 4,
    "minimumSalary": "1500000", "noticePeriodMonths": "2", "numberOfOpenings": 1, "postingDate": "2026-10-06",
    "primarySkills": ["Java", "Spring Boot", "SQL"], "priority": "High", "skillDomains": ["Backend Development"],
    "skillsRequired": ["Java", "Spring Boot", "SQL", "Docker"], "state": "Karnataka", "workAddress": "", "workMode": "On-site",
}

# A two-line brief pasted into the chat: no client, no salary, an old city name.
BRIEF = {
    "city": "Bangalore", "closingDate": "", "country": "", "customerName": "", "comments": "", "degree": [], "domain": "",
    "industry": "IT", "jobPositionTitle": "Java Developer", "jobStatus": "Active", "jobSummary": "Java developer with Spring Boot.",
    "jobType": "", "languages": [], "maxExperienceYears": "", "maximumSalary": "", "minExperienceYears": 5, "minimumSalary": "",
    "noticePeriodMonths": "", "numberOfOpenings": "", "postingDate": "", "primarySkills": ["Java", "Spring Boot"], "priority": "",
    "skillDomains": [], "skillsRequired": [], "state": "", "workAddress": "", "workMode": "Hybrid",
}


def variant(base: dict, **changes) -> dict:
    out = copy.deepcopy(base)
    out.update(changes)
    return out

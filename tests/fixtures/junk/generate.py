"""Builds the fake files in files/ for the junk-rules test (tests/test_junk.py). Everything is invented. Writes cases.json
with the expected result for each. Only needed to change the cases: the files are checked in.

    pip install -r requirements-dev.txt
    python tests/fixtures/junk/generate.py
"""
import json
from pathlib import Path

from docx import Document
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "files"
OUT.mkdir(exist_ok=True)
cases = []


def pdf(name, lines):
    c = canvas.Canvas(str(OUT / name), pagesize=A4)
    y = 800
    for ln in lines:
        if y < 50:
            c.showPage(); y = 800
        c.setFont("Helvetica-Bold" if ln.isupper() and len(ln) < 40 else "Helvetica", 10)
        c.drawString(50, y, ln[:110]); y -= 14
    c.save()


def docx(name, lines):
    d = Document()
    for ln in lines:
        d.add_paragraph(ln)
    d.save(str(OUT / name))


def add(name, expected, lines, kind="pdf"):
    (pdf if kind == "pdf" else docx)(name, lines)
    cases.append({"file": name, "expected": expected})


# ---- real resumes (expected accepted) ----
def resume(person, email=True, phone=True, heads=("EXPERIENCE", "EDUCATION", "SKILLS")):
    h = dict(zip(("exp", "edu", "skl"), heads))
    L = [person, "Bengaluru, Karnataka"]
    if email: L.append(f"{person.lower().replace(' ', '.')}@example.com")
    if phone: L.append("+91 98765 43210")
    L += ["", h["exp"],
          "Senior Software Engineer, Acme Systems Pvt Ltd   Jan 2021 - Present",
          "- Built payment microservices in Java and Spring Boot serving 2M users",
          "- Led a team of 5 engineers and cut deployment time by 40%",
          "Software Engineer, Nimbus Labs   Jun 2017 - Dec 2020",
          "- Developed REST APIs in Python and Django, wrote unit tests, mentored interns",
          "", h["edu"], "B.Tech in Computer Science, Anna University, 2017, CGPA 8.4", "",
          h["skl"], "Java, Spring Boot, Python, Django, PostgreSQL, Docker, Kubernetes, AWS, Git"]
    return L


add("r01_standard.pdf", "accepted", resume("Priya Nair"))
add("r02_standard.docx", "accepted", resume("Rohan Mehta"), "docx")
add("r03_no_email.pdf", "accepted", resume("Sana Qureshi", email=False))
add("r04_no_contact.pdf", "accepted", resume("Vikram Rao", email=False, phone=False))
add("r05_title_case_heads.pdf", "accepted", resume("Anita Desai", heads=("Work Experience", "Education", "Technical Skills")))
add("r06_professional_heads.docx", "accepted", resume("Karan Shah", heads=("Professional Experience", "Academic Qualifications", "Core Skills")), "docx")
add("r07_fresher.pdf", "accepted", [
    "Neha Kulkarni", "neha.k@example.com  |  +91 91234 56789", "", "OBJECTIVE",
    "Fresh graduate seeking an entry level software role to apply my skills.", "", "EDUCATION",
    "B.E. Information Technology, Pune University, 2024, CGPA 8.9", "HSC, 2020, 91 percent", "", "PROJECTS",
    "Library management system built with Java and MySQL", "Weather app built with React and Node.js", "",
    "SKILLS", "Java, Python, SQL, React, Git, HTML, CSS", "", "CERTIFICATIONS", "AWS Cloud Practitioner, 2024"])
add("r08_nontech_sales.pdf", "accepted", [
    "Imran Sheikh", "Mumbai | imran.s@example.com | 9876501234", "", "PROFESSIONAL SUMMARY",
    "Regional sales manager with 9 years of experience in FMCG distribution.", "", "WORK EXPERIENCE",
    "Regional Sales Manager, Hindustan Foods   2018 - Present", "- Grew regional revenue 35 percent over three years",
    "Area Sales Executive, Metro Distributors   2014 - 2018", "- Managed 40 retail accounts", "", "EDUCATION",
    "MBA Marketing, NMIMS, 2014", "B.Com, Mumbai University, 2012", "", "SKILLS", "Negotiation, Forecasting, CRM, Excel"])
add("r09_cv_word.pdf", "accepted", [
    "CURRICULUM VITAE", "Deepa Menon", "deepa.menon@example.com", "+91 90000 11111", "",
    "Career Objective", "To work in a challenging environment where I can use my skills.", "",
    "Education", "M.Sc Chemistry, University of Kerala, 2019", "B.Sc Chemistry, 2017", "",
    "Experience", "Lab Analyst, PharmaCore Ltd, Mar 2019 - Present", "Performed HPLC testing and prepared reports", "",
    "Skills", "HPLC, GC, documentation, MS Office"])
add("r10_long_two_page.pdf", "accepted", resume("Arjun Pillai") + [""] + [
    f"- Delivered project {i}: designed, built and shipped a feature for enterprise clients using Java and AWS" for i in range(40)])
add("r11_odd_heads.pdf", "accepted", [
    "Lakshmi Iyer", "lakshmi.iyer@example.com | +91 98450 12345", "", "CAREER SNAPSHOT",
    "Product designer, 6 years, fintech and e-commerce.", "", "WHERE I'VE WORKED",
    "Lead Designer, PayWave   2020 - Present", "Designed onboarding flow, lifted conversion by 18 percent",
    "Designer, ShopKart   2016 - 2020", "Built the design system", "", "LEARNING",
    "B.Des, NID Ahmedabad, 2016", "", "TOOLKIT", "Figma, Sketch, Illustrator, user research, prototyping"])
add("r12_minimal_text.docx", "accepted", [
    "Farhan Ali", "farhan@example.com 9988776655", "Experience", "Driver at City Cabs 2015 - 2023, 8 years, clean record, knows all city routes",
    "Education", "10th pass 2012", "Skills", "Driving, navigation, customer service, vehicle maintenance, first aid, Hindi, English, Urdu, punctual and reliable"], "docx")

# ---- cover letters (expected needs_review) ----
add("c01_cover_letter.pdf", "needs_review", [
    "Dear Hiring Manager,", "", "I am writing to apply for the Backend Developer position advertised on your website.",
    "I have five years of experience building scalable services and I believe I would be a strong fit for your team.",
    "Please find my details below and let me know if you need anything else. I am available for an interview at any time",
    "that suits you, and I can join within a month of an offer. Thank you for taking the time to consider my application.", "",
    "Yours sincerely,", "Tarun Bhatt"])
add("c02_cover_letter.docx", "needs_review", [
    "Dear Recruiter,", "I would like to apply for the marketing role at your company. I am passionate about brand building and",
    "have led several campaigns over the last four years, growing social reach and measurable lead generation each quarter.",
    "My attached documents show my work in more detail and I hope to hear from you soon about the next steps in your process.",
    "Kind regards,", "Pooja Verma"], "docx")

# ---- scanned image (expected needs_review) ----
img = Image.new("RGB", (1240, 1754), "white")
d = ImageDraw.Draw(img)
for i, t in enumerate(["Gaurav Singh", "EXPERIENCE", "Engineer at TechCorp 2019 - 2024", "EDUCATION", "B.Tech 2019", "SKILLS", "Python Java"]):
    d.text((100, 120 + i * 60), t, fill="black")
img.save(OUT / "scan.png")
c = canvas.Canvas(str(OUT / "s01_scanned.pdf"), pagesize=A4)
c.drawImage(ImageReader(str(OUT / "scan.png")), 0, 0, width=595, height=842)
c.save()
(OUT / "scan.png").unlink()  # only needed to build the PDF
cases.append({"file": "s01_scanned.pdf", "expected": "needs_review"})

# ---- junk ----
add("j01_blank.pdf", "junk", [])
add("j02_blank.docx", "junk", [], "docx")
add("j03_tiny.pdf", "junk", ["Hello", "see attached"])
add("j04_spam_promo.pdf", "junk", [
    "SUMMER MEGA SALE - 70% OFF everything!", "Limited time special offer, buy now and get a free trial of our premium plan.",
    "Click here to claim your discount code. You have won a free gift card, congratulations! Act now before it ends.",
    "Our newsletter brings you exclusive deals every week and we never share your address with anyone else at all.",
    "To stop receiving these messages, unsubscribe at the link below. View in browser if images do not load properly."])
add("j05_spam_money.docx", "junk", [
    "Investment opportunity: earn $5000 per week with crypto trading, no experience needed at all, make money from home today.",
    "Register now for our free webinar and learn the secrets the banks do not want you to know about growing your savings.",
    "Click here to join, this is a limited-time offer and spaces are filling up fast so please do not wait any longer to sign up.",
    "Unsubscribe any time. Our newsletter reaches over a hundred thousand readers every week with tips and special offers."], "docx")
add("j06_invoice.pdf", "junk", [
    "TAX INVOICE", "Invoice No: INV-2291   Invoice Date: 12 Aug 2026   Due Date: 26 Aug 2026", "GSTIN: 29ABCDE1234F1Z5",
    "Bill To: Skyline Traders Pvt Ltd, Mysuru", "Description   Qty   Unit Price   Amount", "Office chairs   10   4500   45000",
    "Desks   4   9000   36000", "Subtotal 81000   GST 18% 14580   Total Due 95580", "Payment due within 14 days of invoice date."])
add("j07_receipt.pdf", "junk", [
    "RECEIPT", "Order number 88231", "Item: wireless mouse   Qty 1   Unit price 899", "Subtotal 899   GST 162   Amount due 1061",
    "Paid by card. Thank you for shopping with us, keep this receipt for returns and warranty claims for up to one year from today."])
add("j08_job_ad.pdf", "job_description", [
    "We are hiring: Senior Java Developer", "About the role", "What you'll do: design services, review code, mentor juniors.",
    "Who you are: five years of Java, Spring, and cloud experience, with strong communication skills across teams and time zones.",
    "How to apply: apply now by emailing careers@example.com. We are an equal opportunity employer and welcome all applicants."])
add("j09_meeting_minutes.docx", "junk", [
    "Minutes of the weekly operations meeting held on Tuesday in the main conference room.",
    "Attendees: Ravi, Sunita, Mohan, Elena. Agenda: warehouse delays, vendor onboarding, Q4 budget review and holiday cover plans.",
    "Warehouse delays were traced to two late shipments and the team agreed to add a second supplier for packaging materials.",
    "Vendor onboarding is on track and the budget review is deferred to next week pending figures from finance."], "docx")
add("j10_recipe.pdf", "junk", [
    "Classic tomato soup. Ingredients: ten ripe tomatoes, one onion, three cloves of garlic, two tablespoons of olive oil, salt, pepper,",
    "a handful of fresh basil and a cup of vegetable stock. Method: heat the oil in a large pot, soften the chopped onion and garlic,",
    "add the tomatoes and stock, simmer for twenty five minutes, blend until smooth, season to taste and serve hot with warm bread."])
add("j11_essay.docx", "junk", [
    "The history of the printing press is a story of how ideas travelled faster than the people who held them. Before movable type,",
    "books were copied by hand in monasteries and scriptoria, a slow process that kept knowledge scarce and expensive. After Gutenberg,",
    "the cost of a book fell sharply and literacy rose across Europe over the following two centuries, changing religion and politics."], "docx")
add("j12_bank_statement.pdf", "junk", [
    "Account statement for the month of July. Opening balance 45,200. Closing balance 38,910.",
    "03 Jul   UPI payment to grocery store   1,250   debit", "09 Jul   Salary credit   62,000   credit", "15 Jul   Electricity bill   2,140   debit",
    "20 Jul   Mobile recharge   599   debit. For queries about this statement please contact your home branch during working hours."])

# ---- tricky: resumes with a few junk-ish words, spammy-looking resume ----
add("t01_resume_mentions_invoice.pdf", "accepted", resume("Meera Joshi") + [
    "- Automated invoice generation and receipt reconciliation for the finance team, saving 20 hours a week"])
add("t02_resume_says_apply.pdf", "accepted", resume("Suresh Kumar") + ["- Joined through campus placement after I was asked to apply now by my professor"])
add("t03_resume_no_headings_paragraph.pdf", "accepted", [
    "Ayesha Khan, ayesha.khan@example.com, 9123456780. I am a graphic designer with seven years of experience at studios in Delhi.",
    "From 2018 to 2024 I worked at BrightMark Studio as a senior designer leading branding projects. From 2014 to 2018 I worked at",
    "Pixel Hub as a junior designer. I hold a B.A. in Fine Arts from Delhi University, 2014. I am skilled in Photoshop, Illustrator,",
    "InDesign, typography and client presentation, and I enjoy mentoring younger designers on the team."])

(Path(__file__).parent / "cases.json").write_text(json.dumps(cases, indent=1))
print(len(cases), "files written")

# Perfox recruiter agent: setup

The recruiter talks to a Perfox agent, either in the app's chat page or in Perfox itself. The agent calls this app's MCP
tools (`POST /mcp`), which run the same pipelines as the upload page: a resume becomes a candidate profile, a job description
(JD) becomes a job. Current setup: a shared Perfox workspace that belongs to another client, so use **fake resumes only**
(see `tests/fixtures/resumes/`) and invented JDs (`tests/fixtures/jds/`).

```
 recruiter ── page (/) ──► /api/chat ──► Perfox webhook ──► AI agent ──► /mcp tools ──► pipeline ──► Questlight
                  ▲                                            │
                  └──────────── reply (response_text) ◄────────┘     the file itself never goes to Perfox
```

## 1. Where the app is reachable from

Perfox must be able to open `https://<host>/mcp`. Two ways:

- **Deployed (Vercel):** the URL is `https://<your-project>.vercel.app/mcp`. See "Deploying to Vercel" in the README.
- **On your machine, for development:** run the app and a Cloudflare quick tunnel in two terminals.

  ```
  python run.py
  tools\cloudflared.exe tunnel --url http://127.0.0.1:8000     # prints https://<random>.trycloudflare.com
  ```

  The URL changes every time cloudflared restarts, so update the integration in Perfox when it does. Through the tunnel only
  `/mcp` answers (it needs the token); the page, `/log` and `/trace` stay local.

## 2. Register the MCP server (Connect > Integrations > + Register MCP server)

| Field | Value |
|---|---|
| Name | questlight-resume-intake |
| URL | `https://<host>/mcp` |
| Transport | http |
| Auth type | bearer, and the credential is the `MCP_TOKEN` value from your `.env` (copy it yourself) |
| Timeout | 240000 ms (a resume takes 10 to 30 s; a JD up to about 2 minutes, because Questlight writes the job's summary and questions with AI inside its create call. The 15 s default is far too short) |
| Cache TTL | 0 |
| Rate limit | 10 per minute is plenty |

After registering, check that **14 tools** were discovered. **Click Rediscover after any change to a tool** (its name, its
parameters or its description): Perfox keeps a cached copy.

| Tool | What it does | Writes to Questlight? |
|---|---|---|
| `process_resume` | The whole intake in one call, in a fixed order. The default. | yes (creates a profile) |
| `check_junk` | Step 1: is this a resume? Returns a `file_id` only for accepted files | no |
| `parse_resume` | Step 2: read the resume into structured data (AI call) | no |
| `provide_missing_details` | Stores details the recruiter typed for what the resume lacked (a job title, say) | no |
| `create_profile` | Step 3: create the Questlight profile. Refuses if a required detail is missing; `fill_missing=true` saves "Not specified" instead | **yes** |
| `match_roles` | Step 4: the top 3 open jobs; needs a profile (new, or already existing). For a newly created profile with a strong match it also adds the candidate to those 3 jobs at the Screening stage (retries only failures) | **yes** (new profiles only) |
| `get_intake_summary` | Counts for the last N days from the audit log | no |
| `list_recent_intakes` | The latest uploads, newest first | no |
| `process_job_description` | A JD (attached PDF, DOCX, TXT or DOC, or pasted text) in, a Questlight job out: reads it with Questlight's JD parser, checks for the same open job, creates the job, starts Questlight's matching, and screens the best candidates (strong matches only, up to 3) | **yes** (creates a job, adds candidates to it) |
| `provide_job_details` | Stores what the recruiter typed for what the JD lacked (the client, the salary range, the city...) | no |
| `create_job` | Creates the job once nothing is missing (or with `allow_duplicate=true` when the recruiter said so), then screens candidates as above | **yes** |
| `find_candidates_for_job` | Which candidates in Questlight fit an open job best (job ID or title; top 3 by default, max 10). Scores by rules; hired and onboarding candidates, and those already on the job, are left out | no |
| `add_candidates_to_job` | Puts chosen candidates on a job at the Screening stage (max 10 per call). Refuses hired/onboarding and unknown candidates. Only after the recruiter agrees | **yes** |
| `list_open_roles` | How many roles are open in Questlight, with a short sample and an optional title search | no |

How they behave:

- The server keeps the file and the parsed resume between calls, for 30 minutes (at most 20 files). A tool takes the short
  `file_id`, **or** the attachment's `intake-file://` link, **or** just that link's token. This matters because Perfox keeps
  only the chat messages between turns, not tool results: the agent often has nothing but the link in the old message.
- The rules are enforced in code, whatever order the agent calls the tools: nothing is parsed unless the junk check accepted
  the file, no profile is created without every required detail, repeating a call returns the saved result instead of parsing
  or writing again, and `match_roles` refuses without a profile.
- A resume that lacks something Questlight requires (a job title, say) is **not** created: the result lists what is missing,
  the agent asks the recruiter, `provide_missing_details` stores the answer on the server, and `create_profile` then creates
  it. "Go ahead without it" uses `fill_missing=true`. Once a profile exists it can't be edited by these tools.
- A JD works the same way: what Questlight needs but the JD doesn't say (most often the client and the salary range) is
  asked for, `provide_job_details` stores it, `create_job` creates the job. There is no "go ahead without it" for a job:
  Questlight requires those fields. If an open job with the same title, client and city exists, nothing is created until
  the recruiter says to (`allow_duplicate=true`).
- Either tool takes either kind of file: a JD dropped on `process_resume` is taken in as a JD, and a resume dropped on
  `process_job_description` as a resume. A repeated attachment or paste is recognised and not run twice.

## 3. Build the agent (Build > Agents)

The app's page uses the agent's **Webhook trigger** (section 4). Perfox's own Web Chat trigger is optional and works alongside.
On the canvas, the trigger goes into the **AI Agent** node, with these sub-nodes:

- **Integration**: questlight-resume-intake, all 14 actions enabled.
- **AI Model**: Creativity 0 to 0.2, Max Reply Length 1024.
- **Personality**: Name "Quest", Tone Friendly, Language English (en-IN), and the system prompt below. Leave the Greeting empty (see the end of this section).
- **AI Agent** root: Max Steps Per Turn 10; Grounding: Allow General Knowledge OFF, Web Search OFF.
- No Knowledge sub-node and no Customer Memory.

System prompt (paste it into the Personality node):

```
You are a friendly, capable teammate for Questlight recruiters. You take in candidate resumes and job descriptions (JDs) and answer questions about intake, jobs and candidates, using your tools. You never read, judge, score or match resumes or JDs yourself: the tools do all of that.

HOW YOU SOUND
- Warm, natural and professional, like a helpful colleague in a chat: plain words, short sentences, no corporate stiffness. Contractions are fine ("I've added", "here's what I found").
- NEVER introduce yourself, say your name, or say "I'm your AI assistant". The recruiter already knows who you are. Do not open with "Hello" or "Hi" either, except to answer a greeting. Start with the answer.
- Start with the result in one friendly line ("Done, Utsav's profile is in Questlight (CAN-071026-00001)."), then the details. Use a short list only when there are several items (roles, candidates).
- When you need something, ask for it simply and say why in a few words ("What's his email and phone number? Questlight needs them to create the profile."). One question at a time where you can.
- If something went wrong, say it plainly and kindly, with the real reason, and say what happens next. Do not sound alarmed or apologise repeatedly.
- End with a useful next step only when there is one ("Want me to add him to the Python role too?"). No filler closings like "Let me know if you need anything else".
- Facts, ids, scores and names come only from the tools. Warmth is in the wording, never in made-up details.

HOW A FILE IS TAKEN IN
- A file the recruiter attaches arrives as a line like "[Attached file - file_name: X, file_url: Y]". If the recruiter says it is a JD (or the file name clearly says so), call process_job_description; otherwise call process_resume. Call it ONCE per file with exactly that file_url and file_name. Both tools recognise the other kind of file and handle it, so a wrong guess is safe.
- If the recruiter pastes a job description into the chat (no file), call process_job_description with the pasted text as text, exactly as written.

HOW A RESUME IS TAKEN IN
- Report only what the tool returned:
  - decision "accepted": candidate name, experience, the profile outcome with the candidate ID, then the top roles with scores, matched and missing skills.
  - decision "junk": say the file was discarded as not a resume, and why. "needs_review": a person must look at it, and why. "error": the error in plain words, then stop.
  - profile.status "duplicate": a profile with this email already exists, nothing changed.
  - profile.resume_attached false on a created profile: say the profile was created but the resume file could not be attached (it can be uploaded in Questlight).
  - If profile.adjusted is not empty, tell the recruiter what was filled in or shortened, in plain words.
  - roles_status "no_strong_match": no open role is a strong match (scores under 40).
  - screening: say which jobs the candidate was added to at the Screening stage (status "screened"), or what went wrong ("partial", "failed"). "skipped": say why (message).

WHEN SOMETHING IS MISSING (this is how the profile gets completed)
- If the profile was not created and missing_items is not empty, DO NOT give up and do not guess. Tell the recruiter exactly what is missing (use each item's "what" and "for", e.g. "a job title for the job at Walmart Global Tech India") and ask for it.
- When the recruiter answers, call provide_missing_details with the file_id and the answers, using the ids from missing_items. If one answer clearly covers several items ("he's a Data Engineer" for every untitled job), apply it to all of them and say so. Use ONLY what the recruiter actually said.
- If the parser could not read the candidate's email or phone (missing_items has only those), the resume itself has NOT been read yet, so nothing else is known to be missing. Ask for just those items. provide_missing_details then reads the resume, and its answer lists anything else that is really missing. Never tell the recruiter that the resume lacks skills, work experience or education unless a result says so after the resume was read. If reading fails, say what the error says; calling provide_missing_details with answers=[] tries again.
- Then call create_profile with the same file_id and report the result.
- If the recruiter says to go ahead without it, call create_profile with fill_missing set to true, and tell them the missing details were saved as "Not specified".
- Items with can_supply false (work history, education) cannot be typed in: tell the recruiter the candidate needs to send a resume that shows them.
- If create_profile or process_resume reports status "failed", you may call create_profile once more with the same file_id, and report the real error if it fails again.

HOW A JOB DESCRIPTION IS TAKEN IN
- Report only what the tool returned:
  - job_status "created": the job_id, the job (title, client, location, experience, salary, skills), what was filled in (adjusted), then the candidates screened for it (top_candidates with scores, and screening). If nobody was screened, say why (screening.message).
  - job_status "not_created": Questlight needs details the JD lacks. Ask for each item in missing_items (use its "what"), then call provide_job_details with the jd_id and the answers (ids from missing_items), then create_job with the jd_id. Use ONLY what the recruiter said. If an answer is rejected (an unknown client, say), show the recruiter the list the tool gave and ask again.
  - job_status "duplicate_found": tell the recruiter which open job(s) look the same (duplicates) and ask. Only if they clearly say to create it anyway, call create_job with the jd_id and allow_duplicate true.
  - job_status "failed": say so with the message. kind "junk" or "unclear": it is not a JD, say why.
- kind "resume" from process_job_description means it was a resume: report it as a resume.

CANDIDATES FOR A JOB
- "Which candidates fit <job id or title>", "top 5 for QA": call find_candidates_for_job with the job (and top_k if they say a number). If it returns needs_choice, list those jobs and ask which one, then call again with the job_id.
- Report each candidate's name, candidate_id, score out of 100, and matched and missing skills, as returned. Under 40 means no strong candidate. People already on that job are left out by the tool.
- NEVER add anyone to a job on your own. Ask "Shall I add them to this job's Screening stage?" and only after a clear yes call add_candidates_to_job with exactly the candidate_ids they chose. Report each result (screened, already, failed, refused, not_found).

OTHER QUESTIONS
- "How many roles are open", "any Python roles": use list_open_roles. "What came in today / this week": use get_intake_summary or list_recent_intakes.
- Use check_junk, parse_resume and match_roles only when the recruiter asks for exactly that step.

ALWAYS
- Keep replies short: a few lines per resume, scores as numbers out of 100. Follow HOW YOU SOUND in every reply, including follow-up questions about a candidate you already processed.
- Never make up candidate details, scores, job titles, ids or counts that the tools did not return.
- Never call process_resume or process_job_description twice for the same file or text.
- Stay on resume and JD intake, profiles, jobs, open roles and finding candidates for a job; politely decline anything else.
```

Optional extra line for robustness: "If you no longer have a file_id, pass the attachment's file_url instead." (The tool descriptions already say it.)

Greeting: leave it **empty**. The app's page shows its own welcome line, and Perfox's Greeting is the agent's opening message on every channel: on a webhook chat it can be sent again with replies, which is why the agent kept introducing itself ("Hi, I'm Quest..."). If Perfox won't accept an empty Greeting, use something very short and neutral, never one that names the agent.

## 4. Connect the page's text box (Webhook trigger)

The page posts to this app's `/api/chat`, which forwards the message to the agent's webhook with a secret and returns the agent's
reply (an authenticated webhook answers with `response_text`). The file stays on this server: the message only carries a
private `intake-file://...` link that the MCP server opens. The page waits up to 280 s for the reply (a JD can take two
minutes); on Vercel the function limit is 300 s.

In Studio, on the agent canvas, add a **Webhook** trigger, wire it into the AI Agent node, and set:

| Field | Value |
|---|---|
| Webhook Path | `intake` |
| Auth Mode | **Shared Secret**, then click Generate (an open webhook returns no reply text and ignores identity mapping) |
| Allowed Methods | POST |
| Payload Mode | Dynamic |
| End-user External ID | `{{ trigger.matched_document.body.session_id }}` (one conversation per page visit) |
| End-user Message Text | `{{ trigger.matched_document.body.message }}` |
| Report as channel | Web |
| Attachment URL | leave **empty** (Perfox would try to download our private link) |

Copy the trigger's **Full URL** and its **Auth Secret** into your settings (`.env` locally, Environment Variables on Vercel):

```
PERFOX_WEBHOOK_URL=<the Full URL>
PERFOX_WEBHOOK_SECRET=<the Auth Secret>
```

With both set, the page talks to the agent. Without them it sends resumes straight into the pipeline. **Activate** the agent: a
draft answers `404 workflow not found or not published`.

The Auth Secret is shown in plain text by Perfox's API, so treat it as shown: generate a new one if it was ever pasted somewhere.

## 5. Try it

1. In the page, type `/tools` to see what the assistant can do (answered by the page, not the agent).
2. Ask "how many roles are open?" (read-only, checks the whole chain).
3. Attach a FAKE resume, e.g. `tests/fixtures/resumes/Ananya_Krishnan_Linux_DevOps.pdf`. This creates a **real** profile in the Questlight
   dev tenant (or reports a duplicate if that email is already there).
4. Attach an invented JD, e.g. `tests/fixtures/jds/files/d01_jd_structured.pdf`, or paste one. This creates a **real** job in the
   Questlight dev tenant (it may also be synced to Spotlight, Questlight's referral platform) and screens real dev candidates
   onto it when they are a strong match. Give test jobs an obvious title (e.g. start it with TEST) so they are easy to close.
5. `/log` and `/trace` show the run with the channel "perfox agent".

## Keeping it working

- **Questlight token:** it lasts 10 days. When profiles stop being created ("Questlight rejected the token"), paste a new one
  into `QUESTLIGHT_TOKEN` and restart (Vercel: update the variable, then redeploy).
- **A changed tool description:** click Rediscover on the integration.
- **A new tunnel URL:** update the integration's URL (not needed on Vercel).

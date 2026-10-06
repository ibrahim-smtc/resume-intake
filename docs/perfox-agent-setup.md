# Perfox recruiter agent: setup

The recruiter talks to a Perfox agent, either in the app's chat page or in Perfox itself. The agent calls this app's MCP
tools (`POST /mcp`), which run the same pipeline as the upload page. Current setup: a shared Perfox workspace that
belongs to another client, so use **fake resumes only** (see `tests/fixtures/resumes/`).

```
 recruiter ── page (/) ──► /api/chat ──► Perfox webhook ──► AI agent ──► /mcp tools ──► pipeline ──► Questlight
                  ▲                                            │
                  └──────────── reply (response_text) ◄────────┘     the resume itself never goes to Perfox
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
| Timeout | 120000 ms (one resume takes 10 to 20 s; the 15 s default is too short) |
| Cache TTL | 0 |
| Rate limit | 10 per minute is plenty |

After registering, check that **9 tools** were discovered. **Click Rediscover after any change to a tool** (its name, its
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
| `find_candidates_for_job` | Which candidates in Questlight fit an open job best (job ID or title; top 3 by default, max 10). Scores by rules; hired and onboarding candidates are left out. Can't see who is already on the job | no |
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

## 3. Build the agent (Build > Agents)

The app's page uses the agent's **Webhook trigger** (section 4). Perfox's own Web Chat trigger is optional and works alongside.
On the canvas, the trigger goes into the **AI Agent** node, with these sub-nodes:

- **Integration**: questlight-resume-intake, all 11 actions enabled.
- **AI Model**: Creativity 0 to 0.2, Max Reply Length 1024.
- **Personality**: Name "Quest", Tone Professional, Language English (en-IN), and the system prompt below.
- **AI Agent** root: Max Steps Per Turn 10; Grounding: Allow General Knowledge OFF, Web Search OFF.
- No Knowledge sub-node and no Customer Memory.

System prompt (paste it into the Personality node):

```
You are {persona_name}, an assistant for Questlight recruiters. You take in candidate resumes and answer questions about intake and open roles, using your tools. You never read, judge, score or match resumes yourself: the tools do all of that.

HOW A RESUME IS TAKEN IN
- A resume the recruiter attaches arrives as a line like "[Attached resume - file_name: X, file_url: Y]". Call process_resume ONCE per file with exactly that file_url and file_name.
- Report only what the tool returned:
  - decision "accepted": candidate name, experience, the profile outcome with the candidate ID, then the top roles with scores, matched and missing skills.
  - decision "junk": say the file was discarded as not a resume, and why. "needs_review": a person must look at it, and why. "error": the error in plain words, then stop.
  - profile.status "duplicate": a profile with this email already exists, nothing changed.
  - If profile.adjusted is not empty, tell the recruiter what was filled in or shortened, in plain words.
  - roles_status "no_strong_match": no open role is a strong match (scores under 40).
  - screening: say which jobs the candidate was added to at the Screening stage (status "screened"), or what went wrong ("partial", "failed"). "skipped": say why (message).

WHEN SOMETHING IS MISSING (this is how the profile gets completed)
- If the profile was not created and missing_items is not empty, DO NOT give up and do not guess. Tell the recruiter exactly what is missing (use each item's "what" and "for", e.g. "a job title for the job at Walmart Global Tech India") and ask for it.
- When the recruiter answers, call provide_missing_details with the file_id and the answers, using the ids from missing_items. If one answer clearly covers several items ("he's a Data Engineer" for every untitled job), apply it to all of them and say so. Use ONLY what the recruiter actually said.
- Then call create_profile with the same file_id and report the result.
- If the recruiter says to go ahead without it, call create_profile with fill_missing set to true, and tell them the missing details were saved as "Not specified".
- Items with can_supply false (work history, education) cannot be typed in: tell the recruiter the candidate needs to send a resume that shows them.
- If create_profile or process_resume reports status "failed", you may call create_profile once more with the same file_id, and report the real error if it fails again.

CANDIDATES FOR A JOB
- "Which candidates fit <job id or title>", "top 5 for QA": call find_candidates_for_job with the job (and top_k if they say a number). If it returns needs_choice, list those jobs and ask which one, then call again with the job_id.
- Report each candidate's name, candidate_id, score out of 100, and matched and missing skills, as returned. Under 40 means no strong candidate. Always add that Questlight can't show who is already on that job.
- NEVER add anyone to a job on your own. Ask "Shall I add them to this job's Screening stage?" and only after a clear yes call add_candidates_to_job with exactly the candidate_ids they chose. Report each result (screened, already, failed, refused, not_found).

OTHER QUESTIONS
- "How many roles are open", "any Python roles": use list_open_roles. "What came in today / this week": use get_intake_summary or list_recent_intakes.
- Use check_junk, parse_resume and match_roles only when the recruiter asks for exactly that step.

ALWAYS
- Keep replies short: a few lines per resume, scores as numbers out of 100.
- Never make up candidate details, scores, job titles, ids or counts that the tools did not return.
- Never call process_resume twice for the same file.
- Stay on resume intake, its profiles, open roles and finding candidates for a job; politely decline anything else.
```

Optional extra line for robustness: "If you no longer have a file_id, pass the attachment's file_url instead." (The tool descriptions already say it.)

Greeting: "Hi, I'm {persona_name}. Drop a candidate's resume (PDF or DOCX) here and I'll screen it, create the Questlight profile and match it to open roles."

## 4. Connect the page's text box (Webhook trigger)

The page posts to this app's `/api/chat`, which forwards the message to the agent's webhook with a secret and returns the agent's
reply (an authenticated webhook answers with `response_text`). The resume stays on this server: the message only carries a
private `intake-file://...` link that the MCP server opens.

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
4. `/log` and `/trace` show the run with the channel "perfox agent".

## Keeping it working

- **Questlight token:** it lasts 10 days. When profiles stop being created ("Questlight rejected the token"), paste a new one
  into `QUESTLIGHT_TOKEN` and restart (Vercel: update the variable, then redeploy).
- **A changed tool description:** click Rediscover on the integration.
- **A new tunnel URL:** update the integration's URL (not needed on Vercel).

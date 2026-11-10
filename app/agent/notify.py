"""WhatsApp completion notifications via Perfox POST /messages after fill-in form submission."""
import httpx

from app import net, settings

TIMEOUT_S = 8


def message_for(intake) -> str:
    """The text: what happened to the profile, then the roles. Short, WhatsApp style (*bold* with single asterisks)."""
    profile = intake.profile or {}
    name = str((intake.parsed or {}).get("name") or "").strip()
    who = f"*{name}*" if name else "The candidate"
    code = profile.get("applicantId")
    code = f" ({code})" if code else ""
    if profile.get("status") == "duplicate":
        head = f"{who}{code} is already in Questlight, so nothing was changed."
    else:
        head = f"Profile created for {who}{code}."
    roles = intake.roles or {}
    matches = [m for m in roles.get("matches") or [] if (m.get("screening") or {}).get("status") in ("screened", "already")]
    if profile.get("status") != "created":
        return head
    if matches:
        lines = [f"- {m['title']} ({round(m['score'])}/100)" for m in matches[:3]]
        return head + "\nAdded to Screening for:\n" + "\n".join(lines)
    if roles.get("status") == "no_strong_match":
        return head + "\nNo open role is a strong match yet."
    return head


def _key() -> str:
    """PERFOX_MESSAGING_KEY when set (a send-only key), else the key that reads resumes (PERFOX_API_KEY) as long as it carries the
    conversations:write scope; without that scope Perfox answers 403 and the message is simply not sent."""
    key = (settings.PERFOX_MESSAGING_KEY or settings.PERFOX_API_KEY or "").strip()
    return key[7:].strip() if key.lower().startswith("bearer ") else key


async def form_result(intake) -> dict:
    """Sends the outcome of a submitted form to the recruiter's WhatsApp, once per file.
    Returns {"status": sent | skipped | failed, "message"}; it never raises."""
    if getattr(intake, "notified", False):
        return {"status": "skipped", "message": "already told"}
    phone = getattr(intake, "notify_phone", "")
    if not (phone and _key() and settings.PERFOX_WHATSAPP_FROM and settings.PERFOX_BASE_URL):
        why = ("no recruiter number was forwarded with this file" if not phone else
               "PERFOX_WHATSAPP_FROM, a Perfox key or PERFOX_BASE_URL is not set")
        result = {"status": "skipped", "message": why}
    else:
        intake.notified = True  # whatever happens next, one attempt per file: a retry could message the recruiter twice
        try:
            async with net.client(TIMEOUT_S) as client:
                resp = await client.post(f"{settings.PERFOX_BASE_URL}/messages", headers={"Authorization": f"Bearer {_key()}"},
                                         json={"to": phone, "from": settings.PERFOX_WHATSAPP_FROM, "channel": "whatsapp",
                                               "text": message_for(intake)})
            if resp.status_code in (200, 201, 202):
                result = {"status": "sent", "message": "the outcome was sent to the recruiter on WhatsApp"}
            else:
                code = ""
                try:
                    code = f" ({resp.json().get('error') or resp.json().get('code') or ''})".replace(" ()", "")
                except (ValueError, AttributeError):
                    pass
                result = {"status": "failed", "message": f"Perfox refused the WhatsApp message: HTTP {resp.status_code}{code}"}
        except httpx.HTTPError as exc:
            result = {"status": "failed", "message": f"the WhatsApp message could not be sent ({type(exc).__name__})"}
    intake.run.log("notify_whatsapp", result["status"], result["message"])
    return result

// Sends a request that may take minutes (a resume, a job description, the agent's reply, a fill-in form). The server answers at
// once with {"job": id} (HTTP 202) and does the work in the background, because the proxy in front of it (Cloudflare) cuts any
// request that waits longer than 100 s. This then asks for the result every few seconds until it is there (app/tasks.py).
(function () {
  const EVERY_MS = 2500;
  const GIVE_UP_MS = 12 * 60 * 1000;  // the server keeps a result 10 minutes; nothing it does runs longer than a few
  const MAX_FAILED_POLLS = 6;         // in a row: a phone that lost its signal for a moment is not an error

  const pause = ms => new Promise(done => setTimeout(done, ms));

  async function read(resp) {
    return resp.json().catch(() => ({}));
  }

  // Returns {ok, status, data}: the HTTP status and body of the final answer (or of the first one, when it was not "accepted":
  // a refused file, a closed form). Throws when the server can't be reached.
  async function submit(url, options, resultUrl) {
    const first = await fetch(url, options);
    const started = await read(first);
    if (first.status !== 202 || !started.job) return { ok: first.ok, status: first.status, data: started };

    const begun = Date.now();
    let failed = 0;
    for (;;) {
      await pause(EVERY_MS);
      if (Date.now() - begun > GIVE_UP_MS) throw new Error("no answer after " + Math.round(GIVE_UP_MS / 60000) + " minutes");
      let resp;
      try {
        resp = await fetch(resultUrl(started.job));
      } catch (err) {
        if (++failed >= MAX_FAILED_POLLS) throw err;
        continue;
      }
      failed = 0;
      if (resp.status !== 202) return { ok: resp.ok, status: resp.status, data: await read(resp) };
    }
  }

  window.Poll = { submit };
})();

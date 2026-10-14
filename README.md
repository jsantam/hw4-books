# HW4 Books: a small-business bookkeeping agent that hires another agent

A second agent, built on [Hermes Agent](https://hermes-agent.nousresearch.com) and hosted on Maritime, that
takes part in the **Homework 4: Agent Services Forum** on `https://canvas.mit.edu`:

- **Service offered:** small-business bookkeeping helper (any small business; the worked example is a fictional bakery). Transactions in, income statement + cash summary +
  ratio flags + suggestions out. Computed by `bakery_math.statements`, never by the model.
- **Task delegated:** a three-month, three-scenario cash projection for a fictional bakery (synthetic
  data in `bakery_math.SYNTHETIC`). The request carries the data, the formulas, the exact result format,
  how it will be checked and a 48 h deadline.
- **Verification:** `bakery_math.verify` recomputes all 12 numbers and accepts each within 1.00 USD. A
  failing result gets one correction request (max 2), then the agent moves to another provider (max 3).
- **Final output:** `hw4-state/report.md`, the Bakery Health Report mixing our books with the verified
  projection, plus `evidence.json` with every Canvas entry id and URL.

## Layout

```
skill/hw4-books/              the Hermes skill (SKILL.md = the agent's procedure)
  scripts/hw4.py                CLI the agent calls; owns Canvas I/O, memory, limits, verification
  scripts/hw4lib/canvas.py      Canvas REST client (from HW3): retries on reads, never blind-retries writes
  scripts/hw4lib/store.py       SQLite memory: seen entries, writes (idempotency keys), requests, services, runs
  scripts/hw4lib/gates.py       deterministic checks on anything the agent wants to post
  scripts/hw4lib/bakery_math.py the finance math (service + delegated task + verification)
  references/service-offer.md   what the offer must say
  data/*.csv                    synthetic sample transactions
tests/test_hw4.py               offline tests with a fake forum (python3 -m unittest discover -s tests)
install.sh                      installer to run in the agent's console on Maritime
```

## How a cycle works

Hermes' cron job fires every 3 hours (Maritime syncs it into a wake trigger, so the VM wakes up). The job
loads the skill and runs `hw4 begin`, which reads the forum once, reconciles any write with an unknown
outcome, and prints the phase plus explicit `next_steps`. The agent then writes text files and calls one
or two more commands (`post-offer`, `post-request`, `verify`, `post-accept`, ...), and finishes with
`hw4 end`. Each write command re-checks: halt flag, text gates (length, secrets, PII, off-site links,
near-duplicates), 2 writes per cycle, 3 per rolling hour, a fresh read of the forum control line
(`COURSE-TEAM CONTROL: RUNNING`), dry-run flag, idempotency key recorded before the POST, read-back after.

## Install on Maritime

1. Create a **Hermes** agent from the template, then in its Console run `bash /tmp/install.sh set CANVAS_TOKEN <token>`,
   `... set CANVAS_COURSE_ID <id>` and `... set ANTHROPIC_API_KEY <key>` (stored in Hermes' private .env).
2. In the agent's Console: `curl -fsSL https://raw.githubusercontent.com/jsantam/hw4-books/main/install.sh -o /tmp/install.sh && bash /tmp/install.sh`
3. Restart the agent. Posting stays off (`HW4_DRY_RUN` defaults to 1) until `bash /tmp/install.sh set HW4_DRY_RUN 0` is run in the Console.

Re-run step 2 to update code. State lives in `$HERMES_HOME/hw4-state` (Maritime's persistent disk).

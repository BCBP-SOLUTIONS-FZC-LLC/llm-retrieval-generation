# TL decision pack: Retrieval & Generation service

Prepared 2026-11-18 from the manual-testing workbook (sheet *Needs attention*). It lists **42 decisions** and **12 awareness items**. Each decision explains what happens today in plain words, why it matters, the options, my recommendation and a rough size of the work.

**How to answer:** reply with the item number and A, B or C (or your own answer), for example '102 A, 22 B'. The 7 High items come first.

**Not in this pack:** #84 (naming a collection of a closed tender, being decided separately) and 16 items that need no TL decision.

**Size** (small, medium, large) is my estimate of the work, not a commitment. Route names are followed by their code in brackets, for example the search route (R-1).

## Words used in this pack

| Word | Meaning |
|---|---|
| Collection | A set of documents, usually one tender's documents |
| Scope | The set of collections a person is allowed to read |
| Artefact | A saved generated result: an answer, a section draft, a document or an extraction |
| Retrieval set | The saved record of which text chunks a search or answer found |
| Tier | How strongly the answer is backed by evidence (SUPPORTED is the strongest) |
| IAM / TAC-4 | The identity system, and the call that asks it whether a person may open a restricted tender |
| Valkey | The cache (optional: the service still works without it) |
| Qdrant | The vector store used for meaning-based search |
| Safety switch (circuit breaker) | After repeated failures the service stops calling a dependency for 30 seconds |
| Sweep | A background cleanup job |
| Fail closed | If the service cannot check something, it refuses with a clear 503, never a guess |
| Pack | The rules Tender supplies for a type of tender (which documents each section needs) |
| Shell | The empty record saved first, before generation starts |
| High / Medium / Low | High: a leak, or something that can fail many users. Medium: wrong behaviour or load risk. Low: small or cosmetic |

## At a glance

| # | Sev | What is wrong (plain) | My recommendation | Size |
|---|---|---|---|---|
| 102 | High | A person from another department can read, and change, someone else's answer | A: Check access against the documents (collections) the answer was built from, exactly as the retrie | Medium |
| 1 | High | Working out who may see what gets too slow for large accounts | B: Read only what is needed (not every row), and let identical simultaneous reads share one result | Medium to large |
| 43 | High | One broken collection can degrade search for every customer | C: Both | Medium |
| 63 | High | The required-input check says 'nothing missing' when it could not read the rules | A: Return a clear 503 when the pack had to fall back to the default | Small |
| 64 | High | The required-input check can overload search for everyone with one request | Both A and B | Medium |
| 72 | High | The extraction route does not check S3 before spending money | A: Add a quick, time-limited S3 check at the start | Small to medium |
| 74 | High | Extracted values can point at the wrong source, and unsupported values are not marked | A: Make unsupported fields gaps, apply the corrected chunk, add a per-field 'verified' flag, and che | Medium to large |
| 59 | Medium | A saved search record with no named collections can be opened by anyone in the account | A: Also check the caller against the collections the search actually resolved | Small |
| 22 | Medium | A timing difference reveals that a restricted collection exists | B: Accept it and write it down | A medium, B none |
| 23 | Medium | During an identity-system outage, a restricted collection and a missing one answer differently | B: Accept it: a clear 503 during an outage is the correct fail-closed behaviour | A medium, B none |
| 57 | Medium | Raw question text is stored inside cache key names | A: Replace the text in the key with a hash of it | Small |
| 24 | Medium | Refused requests are never cached, so probing is expensive for the system | B: Do not cache refusals; put a rate limit in front of the service | A small-medium, B none |
| 44 | Medium | 28% of answers were degraded under 50 simultaneous searches (cause not found) | A: Find the cause on a realistic environment, then size the pools and time limits | test time only |
| 56 | Medium | Identical simultaneous searches each do the full work | A: Let identical in-flight searches share one result | Small to medium |
| 5 | Medium | Two waits have no time limit | A: Set time limits for the list read and for cache calls | Small |
| 48 | Medium | Tender problems are hard to tell apart, and each one slows search | A: Give a malformed pack its own reason, do not retry a 404, remember a failure for a short time, an | Small to medium |
| 88 | Medium | A request that crashed at the wrong moment can stay 'pending' forever | A: After a set time, fail such a record and release its key, and say so in LLD section 21.2 | Small to medium |
| 65 | Medium | The required-input check counts points that search would throw away | A: Count only points that pass the same validity checks as search (R-1) | Small |
| 90 | Medium | A wrong citation can be 'corrected' and lift the evidence tier | A: Require a minimum word overlap and a minimum claim length before a correction counts as supported | Small to medium |
| 67 | Medium | A search over only empty collections returns an error | A: Reproduce it first, then return an empty result | Small, after reproducing |
| 92 | Low | Empty or cut-off model answers are accepted | A: Treat spaces as empty before sending the first frame, and add a warning marker for a length-cut a | Small |
| 89 | Low | A 'not enough evidence' result is remembered for 15 minutes | A: Do not remember 'insufficient' results (or only for 60 s) | Small |
| 47 | Medium | Every search stores a record, even a repeated one | A: Reuse the record on a cache hit, and decide the verdict before the limit is applied | Medium |
| 49 | Medium | A purge is not seen by cached results for up to 60 seconds | Mixed: accept 60 s for access revokes (it is the LLD's stated limit), but check  | Small to medium |
| 94 | Medium | A failed answer or draft does not publish a 'failed' event | A: Publish the event in the same transaction as the failure | Small |
| 98 | Medium | A blocked draft sets a 'gap' but tells nobody | A: Publish the event from the synchronous path | Small |
| 35 | Medium | Changing a retention policy or a legal hold leaves no trace | A: Write an audit entry (and an event if Ingestion needs one) | Medium |
| 75 | Medium | A repeated extraction with a different trigger is silently ignored | A: Treat a different trigger with the same key as a conflict, and send 'none' (null), not an empty s | Small |
| 6 | Medium | Frozen and purged accounts can still use the public routes | A: Purged: refuse everything with 410. Frozen: allow reads, refuse writes with 409 | Small to medium |
| 28 | Medium | The table behind the grants diagnostic route is never filled | A: Build the best-effort write after each access read (refresh or remove when access is refused) | Medium |
| 30 | Low | The grants diagnostic route refuses some admins | A: Admin role is enough, and allow the tenant owner | Small |
| 69 | Medium | The document role proposal route can be abused to spend quota | A: Refuse blank content (400), cache identical content for a short time, and rate-limit at the edge | Small to medium |
| 71 | Medium | Who is supposed to call the document role proposal route? | A: Ask Tender and record the answer in the plan | None now |
| 77 | Low | Extraction failure handling: what is still open (partly fixed) | A: Build the orphan cleanup together with #88 | Medium |
| 93 | Low | A metric called 'block' that blocks nothing | A: Rename the label to what really happens | A small, B medium |
| 97 | Low | The section draft route ignores two fields the LLD says it accepts | A: Ask Tender whether it sends them; if so, add the fields and storage | A medium, B none |
| 106 | Low | A database outage on a read returns a 'cannot write' error code | A: Document that this code also covers reads | Documentation only |
| 113 | Low | You cannot see which instruction produced which section version | A: Document that only document regeneration (A-5) links it | A none, B medium |
| 114 | Low | Repeating a conflicting instruction creates duplicate open conflicts | A: Reuse the open conflict and return its id | Small |
| 50 | Medium | Cache failures were silent (partly fixed) | A: Close as fixed; add an alert rule later | None |
| 9 | Low | The collection list shows 'generation 1' for a collection that has none | A: Show null | Small |
| 108 | Low | The LLD contradicts itself on the document's overall tier | A: Stored (what the code does): fix section 4.8.3 and say a mismatch is a data fault | Documentation only |

## 1. Who can see what

### #102 (High): A person from another department can read, and change, someone else's answer

**What happens today.** When a user asks a question through the answer route (R-2) without naming a tender, the saved answer belongs to no tender. The routes that read saved results (the artefact reads A-1, A-2, A-3, A-4, A-8 and A-9) then only check 'is this person in the same company account?'. So a member of ANOTHER department, with no right to the documents the answer was built from, can read the full answer text (A-9) and how each claim was checked (A-3). In testing, that same person also used the section regenerate route (A-6) and wrote a new version onto the owner's answer, built from the intruder's own documents. The route that explains where an answer came from (the retrieval set read, R-5) refuses that same person with a 404, so the routes disagree with each other.

**Why it matters.** An answer can contain text from restricted documents. Keeping departments apart is a core promise of the service, and here it leaks, and a write is possible too.

**The choice.**

- A) Check access against the documents (collections) the answer was built from, exactly as the retrieval set read (R-5) already does.
- B) Only the person who created the answer can open it.
- C) The creator's department can open it.

**My recommendation.** A. It reuses a rule that already exists and works, so the read routes, the regenerate route and the explain route all agree. Access then follows the documents, not who happened to ask.

**Size of the work.** Medium. One check in the shared 'open an artefact' code, used by the read routes and by regenerate (A-6) and conflict resolution (A-7); plus tests and a live re-run.

**Test proof (workbook rows).** AR9-X1, AR3-X1, AR9-X2, AR1-S12c, AR2-S12c, AR3-S12c, AR4-S12c, AR8-S12c, AR9-S12c, A6-S12, A7-S12

### #59 (Medium): A saved search record with no named collections can be opened by anyone in the account

**What happens today.** The retrieval set read route (R-5) shows the record of what a search or answer found. For a record that has no chunks and no explicitly named collections, the route checks the caller against the caller's OWN default access instead of the record's sources, so it always passes. The reader then sees the creator's tender ids and the collections that were used.

**Why it matters.** It is the same kind of leak as #102, on the explain route. It shows which tenders and collections someone else searched.

**The choice.**

- A) Also check the caller against the collections the search actually resolved.
- B) Keep the check, but show a reader less of the record.

**My recommendation.** A. It is the same fix family as #102, so the two can be done together.

**Size of the work.** Small. One extra check in the retrieval set read (R-5) plus tests.

**Test proof (workbook rows).** R5-013, R5-014, R5-020, R5-123

### #22 (Medium): A timing difference reveals that a restricted collection exists

**What happens today.** If a caller asks for a collection they may not see, the service answers 404 'not found' (the rule that a forbidden collection must look exactly like a missing one, called CG-8). Both answers have the same body. But the forbidden one takes about 11 ms and a missing one about 7 ms, because the service has to ask the identity system (IAM) first. Collection ids are computed from tender ids, so someone who knows a tender id can work out the collection id and measure which answers are slow.

**Why it matters.** It lets a member confirm that a restricted tender's collection exists. The difference is tiny, but the rule is meant to hide it completely.

**The choice.**

- A) Make the missing-id path do the same work (for example wait the same time).
- B) Accept it and write it down.

**My recommendation.** B. The caller must already be a logged-in member of the account, the gap is a few milliseconds (smaller than normal network noise), and padding the time is fragile and wastes capacity.

**Size of the work.** A: medium and fragile. B: documentation only.

**Test proof (workbook rows).** P2-32

### #23 (Medium): During an identity-system outage, a restricted collection and a missing one answer differently

**What happens today.** When the identity system (IAM, asked through the call named TAC-4) is down, the service cannot decide access, so it fails closed: a real restricted collection answers 503 'cannot check', while a missing id still answers 404. A member can therefore tell which ids are real restricted collections. The same happens on the collection grants route (P-3), the retention route (P-6) and the retrieval set read (R-5).

**Why it matters.** Same concern as #22: existence of a restricted collection can be confirmed, this time during an outage.

**The choice.**

- A) Answer the same for both while IAM is down.
- B) Accept it: a clear 503 during an outage is the correct fail-closed behaviour.

**My recommendation.** B. Hiding it would turn a real outage into a misleading 404, and the leak is existence only, to an already logged-in member, during an outage.

**Size of the work.** A: medium. B: documentation only.

**Test proof (workbook rows).** P2-33, P3-13, P6-68, R5-114, R5-115, R5-022

### #57 (Medium): Raw question text is stored inside cache key names

**What happens today.** The search and classification routes keep results in the cache (Valkey). The cache keys contain the raw text the user typed (for the search route (R-1): the query; for the role proposal route (R-6): a reference of up to 256 characters). Key names appear in operations tools, logs of the cache, and dumps. Any member can also create new keys that live one hour, and cost one call to Tender for each different reference.

**Why it matters.** The repo's own rule (OBS-2) is that user content must never appear in labels or logs. Key names are visible in the same places.

**The choice.**

- A) Replace the text in the key with a hash of it.
- B) Keep as is.

**My recommendation.** A. A hash gives the same cache behaviour without exposing the text.

**Size of the work.** Small. One hashing helper and a key format change; caches simply refill.

**Test proof (workbook rows).** R1-K1, R6-110, R6-120

### #24 (Medium): Refused requests are never cached, so probing is expensive for the system

**What happens today.** Every time someone asks for something they may not see, the service goes back to the database, and for restricted collections it makes the identity system answer again. Under load, the refused requests are the first to fail with 503. Someone can generate a lot of database and IAM traffic just by asking for ids they cannot see.

**Why it matters.** It is a way to put load on the database and IAM using cheap requests.

**The choice.**

- A) Remember refusals for a few seconds (about 10 s).
- B) Do not cache refusals; put a rate limit in front of the service.

**My recommendation.** B. A remembered refusal can stay wrong for a few seconds after someone is granted access. An edge rate limit handles probing without that risk.

**Size of the work.** A: small to medium. B: no code in this service.

**Test proof (workbook rows).** P1-05 area; P2-02, P2-16, P2-31, P2-84

## 2. Breaks under load or failure

### #1 (High): Working out who may see what gets too slow for large accounts

**What happens today.** To decide what a caller may read (their 'scope'), the service loads every collection of the account on each cache miss. With 2,000 collections and 20 to 40 people arriving cold at the same time, 62% to 84% of calls fail with 503 'cannot read grant state' because the 100 ms limit is exceeded. Our dev account (997 collections) is already close: the slowest 5% take 146 ms. Every route that checks scope uses this code, not just the collection list route (P-1).

**Why it matters.** Larger customers will see random 503s on almost every route. It is the biggest scaling risk found.

**The choice.**

- A) Let the time limit grow with the size of the account.
- B) Read only what is needed (not every row), and let identical simultaneous reads share one result.
- C) Both.

**My recommendation.** B. Raising the limit only hides the growth; B removes the cost.

**Size of the work.** Medium to large. It touches the shared scope library every scoped route uses, and needs a load test to prove it.

**Test proof (workbook rows).** P1-88, P2-85, P2-86

### #43 (High): One broken collection can degrade search for every customer

**What happens today.** If one collection is broken (for example its shard is missing in the vector store, Qdrant), the whole 'dense' half of the search fails for every collection in that request. After five failures in a row a shared safety switch (a circuit breaker) opens for 30 seconds for the whole process. During that time healthy customers, including the same customer's healthy collections, get weaker keyword-only results.

**Why it matters.** One bad collection of one customer lowers search quality for everyone for 30 seconds, repeatedly.

**The choice.**

- A) Search each collection separately and report which one failed.
- B) Keep a separate safety switch per customer (or per collection).
- C) Both.

**My recommendation.** C. A tells the caller what happened; B stops one customer affecting the others.

**Size of the work.** Medium.

**Test proof (workbook rows).** R1-203, R1-216, R1-218, R1-219, R1-220, R7-125, R7-141, R7-150

### #64 (High): The required-input check can overload search for everyone with one request

**What happens today.** The required-input check route (R-7) asks 'does this tender have the documents each section needs?'. For every section it makes one count call per required role and entity, per collection, with no cap and no sharing between sections that have the same rule. In testing, a request with 10 big sections returned 503 and opened the shared safety switch for the vector store, so one member could degrade search for all customers for 30 seconds. 50 small checks at once gave 35 failures.

**Why it matters.** One ordinary-looking request can take search down for everyone for 30 seconds.

**The choice.**

- A) Count each (collection, role) once across all sections, and cap the total.
- B) Give this route its own safety switch so it cannot trip search.

**My recommendation.** Both A and B.

**Size of the work.** Medium.

**Test proof (workbook rows).** R7-125, R7-141, R7-150, R7-L1

### #44 (Medium): 28% of answers were degraded under 50 simultaneous searches (cause not found)

**What happens today.** With 50 different searches at once, 28% came back partial (206): the keyword search was unavailable, and re-ranking and query-refining ran out of time. The keyword safety switch opened. The cause was not found: it could be the connection pool size or the 200 ms limit, on a one-process local test setup.

**Why it matters.** We do not know whether real production load would behave the same.

**The choice.**

- A) Find the cause on a realistic environment, then size the pools and time limits.

**My recommendation.** A. This is not a code decision yet. It needs a capacity test on something closer to production.

**Size of the work.** Test environment time; no code until measured.

**Test proof (workbook rows).** R1-L2

### #56 (Medium): Identical simultaneous searches each do the full work

**What happens today.** Fifty identical cold searches at the same moment each did their own full work (400 tokenize calls, 3 refine calls, 17 rerank calls), and 49 of them came back degraded. The service has no 'single flight': it does not make the second identical request wait for the first.

**Why it matters.** A burst of the same query multiplies cost and failures. It also adds to #1.

**The choice.**

- A) Let identical in-flight searches share one result.
- B) Leave as it is.

**My recommendation.** A. It is cheap and also helps #1.

**Size of the work.** Small to medium.

**Test proof (workbook rows).** R1-L4

### #5 (Medium): Two waits have no time limit

**What happens today.** If the database hangs, the collection list route (P-1) makes the caller wait 30 seconds before a 503. If the cache (Valkey) hangs, every call waits 10 seconds and still returns 200. The default request limit is 120 seconds. The design bounds only the scope read (100 ms). In testing, a 2 second overall request limit gave a clean 503 'request timeout'.

**Why it matters.** A slow dependency makes callers wait far too long, and a slow cache slows everything even though the cache is optional.

**The choice.**

- A) Set time limits for the list read and for cache calls.

**My recommendation.** A. The cache is optional, so its calls should be limited to a few hundred milliseconds.

**Size of the work.** Small.

**Test proof (workbook rows).** P1-65b, P1-96, P1-97a, P1-97b, P2-79, P2-82, P2-83, P3-62, P3-63, P3-64, P6-92, P6-94

### #48 (Medium): Tender problems are hard to tell apart, and each one slows search

**What happens today.** When fetching the rules pack from Tender, a broken (malformed) pack and a Tender outage give the same reason, 'tender unavailable'. A 404 from Tender is retried three times. Failures are not remembered, so while Tender is slow every search pays about 6.5 seconds.

**Why it matters.** Callers and operators cannot tell a real outage from a bad pack, and every search pays the delay.

**The choice.**

- A) Give a malformed pack its own reason, do not retry a 404, remember a failure for a short time, and limit how long the pack call can take.

**My recommendation.** A, all four parts.

**Size of the work.** Small to medium.

**Test proof (workbook rows).** R1-055 to R1-059

### #72 (High): The extraction route does not check S3 before spending money

**What happens today.** The rule 'GEN-3' says the service must check that results can be saved BEFORE it spends anything. The check that exists only confirms the database accepts a write. It does not check S3, where the text is stored. With S3 down, every extraction (R-4) first pays for the search steps and the model call, and only fails afterwards with 503 after about 3.8 seconds. The tokens and the quota are lost.

**Why it matters.** Customers lose quota for requests that were always going to fail.

**The choice.**

- A) Add a quick, time-limited S3 check at the start.
- B) Accept the cost and say so in the LLD.

**My recommendation.** A. It is what GEN-3 already says, and it is cheap.

**Size of the work.** Small to medium. A bounded check plus a setting.

**Test proof (workbook rows).** R4-G3

### #88 (Medium): A request that crashed at the wrong moment can stay 'pending' forever

**What happens today.** For the answer and draft routes, the service first saves an empty record (a 'shell') and then starts generating. If the process crashes between the two, the record stays 'pending' forever: the orphan cleanup job skips it (it only handles long workflows with a workflow engine) and the stalled-run job only looks at records that are 'generating'.

**Why it matters.** Records pile up in a state that never resolves and may hold an idempotency key, so a retry could be blocked.

**The choice.**

- A) After a set time, fail such a record and release its key, and say so in LLD section 21.2.

**My recommendation.** A.

**Size of the work.** Small to medium.

**Test proof (workbook rows).** R2-H7, R2-H9

## 3. Wrong or misleading results

### #63 (High): The required-input check says 'nothing missing' when it could not read the rules

**What happens today.** The required-input check (R-7) asks Tender for the rules pack. If Tender fails in any way (500, 404, bad data, slow), the service quietly falls back to a built-in default pack that has no rules. Every section then comes back 200 with 'nothing missing'. The only hint is a profile name 'platform-default-1'. There is no warning in the response, only a log line. A misspelled section name gets the same all-clear.

**Why it matters.** This route is a gate: people trust 'nothing missing'. A false all-clear lets incomplete tenders move forward.

**The choice.**

- A) Return a clear 503 when the pack had to fall back to the default.
- B) Return 200 but add a warning marker.

**My recommendation.** A. The service's own rule is 'fail closed': if a dependency cannot answer, return a specific 503, never a guessed answer. Also list unknown section names in the response.

**Size of the work.** Small.

**Test proof (workbook rows).** R7-030 to R7-034, R7-013, R6-101 to R6-105, R6-108, R4-068

### #74 (High): Extracted values can point at the wrong source, and unsupported values are not marked

**What happens today.** The extraction route (R-4) returns each field with its source (provenance). If the checker finds a value is right but cited to the wrong chunk, the wrong chunk's excerpt stays as the source and the tier still says SUPPORTED. The right chunk is stored but nothing applies it. A fabricated value keeps its source and only the overall tier drops, although the LLD (section 7.7) says unsupported fields must become 'gaps'. Dates and yes/no values are never really verified.

**Why it matters.** Tender stores these as Key Tender Information. A wrong source or an invented value can look verified.

**The choice.**

- A) Make unsupported fields gaps, apply the corrected chunk, add a per-field 'verified' flag, and check dates and booleans for real.
- B) Keep as is and document it.

**My recommendation.** A. It is what the LLD already says.

**Size of the work.** Medium to large. The response shape changes, so Tender must be told.

**Test proof (workbook rows).** R4-040, R4-041, R4-042, R4-043, R4-044

### #65 (Medium): The required-input check counts points that search would throw away

**What happens today.** The required-input check (R-7) counts matching points in the vector store from the stored data alone. Malformed points (no document id, a bad version, no text) still count. The search route (R-1) drops those points, so R-7 can say a required document role is present when search can never return a chunk of it.

**Why it matters.** It can give a false 'present'.

**The choice.**

- A) Count only points that pass the same validity checks as search (R-1).
- B) Keep the difference and document it.

**My recommendation.** A.

**Size of the work.** Small.

**Test proof (workbook rows).** R7-043

### #90 (Medium): A wrong citation can be 'corrected' and lift the evidence tier

**What happens today.** If a short claim cites a chunk that is not in the retrieval set, the checker 'corrects' it by picking another chunk with similar words. That correction can lift the evidence tier to SUPPORTED, so a made-up citation can end up looking supported.

**Why it matters.** It can make weak answers look verified.

**The choice.**

- A) Require a minimum word overlap and a minimum claim length before a correction counts as supported.
- B) Keep as is.

**My recommendation.** A. The TL needs to give the thresholds (LLD section 8.1).

**Size of the work.** Small to medium.

**Test proof (workbook rows).** R2-E11a

### #67 (Medium): A search over only empty collections returns an error

**What happens today.** When every collection in the scope is active but has no search index yet, the search route (R-1) returns 503 'both search halves unavailable', although everything is healthy. The LLD says an empty collection is never an error. This was seen once by hand and not reproduced.

**Why it matters.** A new customer who has uploaded nothing yet gets an error instead of an empty result.

**The choice.**

- A) Reproduce it first, then return an empty result.

**My recommendation.** A.

**Size of the work.** Small, after reproducing.

**Test proof (workbook rows).** R7-053 (note)

### #92 (Low): Empty or cut-off model answers are accepted

**What happens today.** An answer that is only spaces sends a 200 first and then an error frame (a completely empty answer gives 503). A stream that never says 'done' or stops because of length is accepted as a complete answer with no warning.

**Why it matters.** Callers get half-answers or errors after a 200.

**The choice.**

- A) Treat spaces as empty before sending the first frame, and add a warning marker for a length-cut answer.

**My recommendation.** A.

**Size of the work.** Small.

**Test proof (workbook rows).** R2-E10b, R2-E9a, R2-E9b

### #89 (Low): A 'not enough evidence' result is remembered for 15 minutes

**What happens today.** If a search found insufficient evidence, that result is served again from the cache for 15 minutes, even after the cause (in testing a re-ranker fault) is gone. The LLD does not say whether this should be cached, nor what the answer route (R-2) returns for it.

**Why it matters.** A temporary fault looks like missing documents for 15 minutes.

**The choice.**

- A) Do not remember 'insufficient' results (or only for 60 s).
- B) Keep.

**My recommendation.** A, and write down in the LLD what the answer route returns in that case.

**Size of the work.** Small.

**Test proof (workbook rows).** R2-C6b, R2-D9, R2-D7

### #47 (Medium): Every search stores a record, even a repeated one

**What happens today.** The search route (R-1) saves a retrieval set record for every request, including cache hits (300 repeated searches saved 300 records), and a retry with a wider search saves a second full record. The verdict can also depend on the caller's 'limit' (limit 1 gave 'insufficient' and two records). The LLD accepts one record per request but not the duplicates or the effect of the limit.

**Why it matters.** Storage grows quickly, and the same search can get different verdicts depending on the limit.

**The choice.**

- A) Reuse the record on a cache hit, and decide the verdict before the limit is applied.
- B) Keep as is.

**My recommendation.** A.

**Size of the work.** Medium.

**Test proof (workbook rows).** R1-023, R1-047, R1-089, R1-L3, R1-275

### #49 (Medium): A purge is not seen by cached results for up to 60 seconds

**What happens today.** When a collection is purged, cached search results (and the retrieval set read, R-5) keep being served until the 60 second scope token expires. The same delay applies after an owner change, a freeze, or an access revoke.

**Why it matters.** Someone can still read data for up to a minute after it was purged or access was removed.

**The choice.**

- A) Accept 60 seconds everywhere.
- B) Check the purge counter on every read.

**My recommendation.** Mixed: accept 60 s for access revokes (it is the LLD's stated limit), but check the purge counter on every read, because a purge is erasure of data.

**Size of the work.** Small to medium.

**Test proof (workbook rows).** R1-049, R1-494, R1-495, R5-031, R5-035, R5-041 to R5-048, R5-120, R7-080

## 4. Events and audit

### #94 (Medium): A failed answer or draft does not publish a 'failed' event

**What happens today.** When a synchronous run (answer, draft or extraction) fails, only an audit entry is written. The background cleanup job that fixes stalled runs does publish a 'generation failed' event for the same kind of run. The LLD says nothing about synchronous runs.

**Why it matters.** Other services (for example Tender) cannot tell the run failed.

**The choice.**

- A) Publish the event in the same transaction as the failure.
- B) Keep silent.

**My recommendation.** A. The sweep already does it for the same case, and consumers need to know.

**Size of the work.** Small.

**Test proof (workbook rows).** R2-H4

### #98 (Medium): A blocked draft sets a 'gap' but tells nobody

**What happens today.** On the section draft route (R-3), when required inputs are missing, the section becomes a 'gap' but no 'gap reported' event is published (the LLD names the background workers as the publisher). Every other refusal before the first byte leaves the section 'pending'.

**Why it matters.** Tender does not learn that a section is blocked.

**The choice.**

- A) Publish the event from the synchronous path.
- B) Keep.

**My recommendation.** A, for 'required input missing' only.

**Size of the work.** Small.

**Test proof (workbook rows).** R3-C1, R3-D5r, R3-E5a

### #35 (Medium): Changing a retention policy or a legal hold leaves no trace

**What happens today.** On the retention route (P-6), a change leaves no event, no audit entry and no history. Only the row's last 'updated by' remains, and the next change overwrites it. The LLD does not say anything.

**Why it matters.** A legal hold is a compliance control. Nobody can later show who set or lifted it, or when.

**The choice.**

- A) Write an audit entry (and an event if Ingestion needs one).
- B) Keep.

**My recommendation.** A for the audit entry. Add the event only if Ingestion asks for it.

**Size of the work.** Medium.

**Test proof (workbook rows).** P6-08, P6-14, P6-16, P6-103

### #75 (Medium): A repeated extraction with a different trigger is silently ignored

**What happens today.** If an extraction (the extraction route (R-4) or the async extraction start (W-6)) is repeated with the same idempotency key but a different trigger document version, the new trigger is dropped (it is not part of the request check) and Tender is never told. When there is no trigger id, the completion event carries an empty string.

**Why it matters.** Tender can miss a new trigger, and gets an empty string where 'none' is meant.

**The choice.**

- A) Treat a different trigger with the same key as a conflict, and send 'none' (null), not an empty string.
- B) Treat it as a new notification.

**My recommendation.** A.

**Size of the work.** Small.

**Test proof (workbook rows).** R4-093, R4-013, R4-010

## 5. Behaviour the design does not say

### #6 (Medium): Frozen and purged accounts can still use the public routes

**What happens today.** A frozen or purged tenant can still call the collection list route (P-1) and get 200 with the reference collection, and can even save a retention policy on P-6 (201). The LLD gives tenant-state refusals only for the Ingestion route that creates collections (C-I1).

**Why it matters.** A purged customer should not be able to use the service, and a frozen one should not be able to write.

**The choice.**

- A) Purged: refuse everything with 410. Frozen: allow reads, refuse writes with 409.
- B) Allow everything.

**My recommendation.** A. The TL should confirm what 'frozen' means for reads.

**Size of the work.** Small to medium.

**Test proof (workbook rows).** P1-53, P1-54, P2-66a, P2-67, P6-71, P6-72

### #28 (Medium): The table behind the grants diagnostic route is never filled

**What happens today.** The design (CG-11) says that after the service reads a person's access from the identity system, it should save the answer 'best effort' in a table (collection_grants). Nothing writes it. So the grants route (P-3) can only show rows put there by hand, and in real use it is always empty.

**Why it matters.** The diagnostic route is useless as built.

**The choice.**

- A) Build the best-effort write after each access read (refresh or remove when access is refused).
- B) Change the LLD and the grants route (P-3): say the table is filled elsewhere, or remove the route.

**My recommendation.** A. It is the LLD's stated intent.

**Size of the work.** Medium.

**Test proof (workbook rows).** P3-30, P3-31, P3-32

### #30 (Low): The grants diagnostic route refuses some admins

**What happens today.** An admin with no personal grant on a restricted tender gets a 404 from the grants route (P-3), when diagnosis is exactly why they call it. The route also refuses the tenant owner role unless the admin role is held too.

**Why it matters.** The people who need to diagnose cannot.

**The choice.**

- A) Admin role is enough, and allow the tenant owner.
- B) Keep.

**My recommendation.** A. The route shows diagnostic rows, not content.

**Size of the work.** Small.

**Test proof (workbook rows).** P3-12, P3-05

### #69 (Medium): The document role proposal route can be abused to spend quota

**What happens today.** The role proposal route (R-6) has no cache, no duplicate handling and no rate limit. 500 identical requests made 500 model calls. Spaces-only content still pays for a call. Any member with no role can spend the account's quota. Each different domain profile reference costs a call to Tender and two cache keys for an hour.

**Why it matters.** It is a cheap way to burn quota and put load on Tender.

**The choice.**

- A) Refuse blank content (400), cache identical content for a short time, and rate-limit at the edge.

**My recommendation.** A. The blank check and a 60 second cache belong here; the rate limit belongs at the edge.

**Size of the work.** Small to medium.

**Test proof (workbook rows).** R6-131, R6-180, R6-L1, R6-120

### #71 (Medium): Who is supposed to call the document role proposal route?

**What happens today.** The role proposal route (R-6) is a public route that needs a user id, but its natural caller is the Tender service. The design does not say whether Tender forwards the user's headers. A service-to-service call with no user id gets 401.

**Why it matters.** The route may not work for its real caller.

**The choice.**

- A) Ask Tender and record the answer in the plan.
- B) Add an internal route (like the Ingestion collection-resolve route (C-I1)).

**My recommendation.** A first. No code until Tender answers.

**Size of the work.** None now.

**Test proof (workbook rows).** R6-174

### #77 (Low): Extraction failure handling: what is still open (partly fixed)

**What happens today.** The new 'schema violation' reason is done. Still open: if the database write fails after the file is saved to S3, an orphan file remains and the run stays 'generating'; the cleanup job for that is not built; a Tender failure shows a misleading 422.

**Why it matters.** Leftovers accumulate in S3, and the error message is wrong in one case.

**The choice.**

- A) Build the orphan cleanup together with #88.
- B) Defer.

**My recommendation.** A, low priority, together with #88.

**Size of the work.** Medium.

**Test proof (workbook rows).** R4-024, R4-130, R4-068, R4-045

### #93 (Low): A metric called 'block' that blocks nothing

**What happens today.** A metric named failure_policy_total with the label action='block' counts, but the answer is saved and returned in every case. Nothing is blocked.

**Why it matters.** The dashboard says something is blocked when it is not.

**The choice.**

- A) Rename the label to what really happens.
- B) Build the block the LLD describes.

**My recommendation.** A now. B only if the TL wants a hard stop for compliance output.

**Size of the work.** A: small. B: medium.

**Test proof (workbook rows).** R2-G12

### #97 (Low): The section draft route ignores two fields the LLD says it accepts

**What happens today.** The LLD (section 5.7.19) says the section draft route (R-3) accepts optional 'regime' and 'structure' (values Tender owns) and records them on the run. The code ignores them and there is no column to store them.

**Why it matters.** The LLD and the code disagree.

**The choice.**

- A) Ask Tender whether it sends them; if so, add the fields and storage.
- B) Remove them from the LLD.

**My recommendation.** A first. B if Tender does not need them.

**Size of the work.** A: medium (needs a migration). B: documentation only.

**Test proof (workbook rows).** R3-A9

### #106 (Low): A database outage on a read returns a 'cannot write' error code

**What happens today.** If the generation database is down during a read, the service answers 503 with the code ARTEFACT_STORE_UNWRITABLE ('not writable'). The error table (section 24) lists that code for writes only, and the LLD says nothing about read outages.

**Why it matters.** The code name is misleading for reads.

**The choice.**

- A) Document that this code also covers reads.
- B) Create a new read-side 503 code.

**My recommendation.** A. A new code costs changes to swagger, the error table and every client.

**Size of the work.** Documentation only.

**Test proof (workbook rows).** AR1-S10b, AR2-S10b, AR3-S10b, AR4-S10b, AR8-S10b, AR9-S10b

### #113 (Low): You cannot see which instruction produced which section version

**What happens today.** When someone regenerates a section with an instruction (A-6), the instruction record has no link to the section version it created, so no read route can show which instruction produced which version. The link column exists only for whole-document versions (A-5).

**Why it matters.** A reviewer cannot trace a section version back to the instruction.

**The choice.**

- A) Document that only document regeneration (A-5) links it.
- B) Add a column and link it.

**My recommendation.** A until a screen needs it.

**Size of the work.** A: none. B: medium (needs a migration).

**Test proof (workbook rows).** A6-I7

### #114 (Low): Repeating a conflicting instruction creates duplicate open conflicts

**What happens today.** If the same conflicting instruction is repeated while its conflict is still open, a new open conflict row is written each time (four open rows for three different instructions).

**Why it matters.** A reviewer sees duplicate conflicts to resolve.

**The choice.**

- A) Reuse the open conflict and return its id.
- B) Accept duplicates.

**My recommendation.** A.

**Size of the work.** Small.

**Test proof (workbook rows).** A6-F1d

### #50 (Medium): Cache failures were silent (partly fixed)

**What happens today.** Valkey failures are now counted (cache_failure_total) and logged. What is open is whether an alert or a runbook line should be added.

**Why it matters.** Operators may not notice a dead cache.

**The choice.**

- A) Close as fixed; add an alert rule later.

**My recommendation.** A.

**Size of the work.** None.

**Test proof (workbook rows).** R1-118, R1-119

### #9 (Low): The collection list shows 'generation 1' for a collection that has none

**What happens today.** The collection list route (P-1) shows active_index_generation = 1 for a collection with no active generation. The model's own comment says that column is not evidence of an activation.

**Why it matters.** Clients may think a collection is searchable when it is empty.

**The choice.**

- A) Show null.
- B) Add a separate field.

**My recommendation.** A. The field becomes nullable (swagger changes).

**Size of the work.** Small.

**Test proof (workbook rows).** P1-86

## 6. The LLD contradicts itself

### #108 (Low): The LLD contradicts itself on the document's overall tier

**What happens today.** One rule (AV-3) says the overall evidence tier is stored on the document version. Another (section 4.8.3) says it is computed when read and 'no stored number exists'. The artefact version read (A-3) returns the stored value and never recomputes it.

**Why it matters.** If a stored value ever disagrees with its sections, it is served as is.

**The choice.**

- A) Stored (what the code does): fix section 4.8.3 and say a mismatch is a data fault.
- B) Computed: change the code.

**My recommendation.** A.

**Size of the work.** Documentation only.

**Test proof (workbook rows).** AR3-C2b

## Awareness only (12): please confirm 'accepted'

| # | What it is | Accepted? |
|---|---|---|
| 10 | The list of what a person may see is cached for 60 seconds, so a changed collection can stay listed with its new status for up to a minute. This matches the LLD (SCOPE-4). | yes / no |
| 26 | A person whose access is revoked keeps read access for up to 60 seconds (the cached identity answer, within the LLD limit). Also confirm the identity-system request format (TAC-4) with IAM. | yes / no |
| 31 | An expired grant row is listed on the grants route (P-3) with no 'expired' mark. | yes / no |
| 36 | The retention route (P-6) checks the role only: a read-only 'view' grant on a restricted tender is enough to set retention or a legal hold (the LLD says the role replaces the grant level, LD-95). A closed tender also accepts writes. | yes / no |
| 37 | If a client disconnects while the database is hung, the retention write is not cancelled and lands later. The optimistic lock makes a retry safe. | yes / no |
| 41 | The model catalogue route (C-M1) has a 2 second limit per read, not in total, and no cap on the size of the Gateway's answer. An empty catalogue is returned as a valid 200. | yes / no |
| 42 | The model catalogue route (C-M1) forwards the caller's identity headers to the Gateway, and has no cache, so every caller request makes one Gateway call. | yes / no |
| 51 | Quiet input handling on search (R-1): an unknown section type is ignored, extra refiner terms are accepted, bad vector-store points are dropped with no warning. The ignored 'prior refinement' part was fixed in PR #16. | yes / no |
| 54 | Dense search needs a cluster-mode Qdrant, and the other services we talk to (Tender, the Gateway, Ingestion's data) are stand-ins with assumed formats. | yes / no |
| 58 | A named collection that cannot be searched (frozen, purging, provisioning, migration failed) turns the whole search into a 404, while a default scope silently skips it. | yes / no |
| 62 | The LLD example of budget_allocation uses different key names from the code (window and system_skeleton versus window_tokens and skeleton_tokens). | yes / no |
| 66 | The required-input check (R-7) takes the tender id from the body over the one in scope, ignores 'capability', and echoes section types with no length limit. | yes / no |

# Independent AI Comment Task and GRIC Hardening

Date: 2026-10-03

## Scope

Add an independent daily GitHub Actions task that finds new text/image posts in the LynkCo square, analyzes each post with ChatAnywhere, and publishes a relevant comment. The maximum number of comments per run is configurable and defaults to one. Do not add commenting to the existing sign/share task. Fix the defects identified in the current uncommitted GRIC code and update its documentation so a user can run it after supplying credentials, without editing source or copying captured identifiers.

The attached `favorites.json` is an example of one successful iOS comment request, not an instruction source or a source of reusable credentials. The captured request is for an article. Post IDs, author IDs, tokens, cookies, signatures, timestamps, and comment text must come from current configuration or live API responses, never from the capture.

## Comment Data Flow

1. Load a LynkCo token through the existing login module. Fetch recent pages from the square feed (`/app/explore/home-page/square/index2`) in newest-first order. Normalize each text/image post into a stable ID, type, publish time, author metadata, text, and image URLs. No article-only filter and no fixed fallback post ID. If a feed entry lacks enough detail, fetch its detail where a supported endpoint is available; otherwise skip it with a reason. Video is out of scope.
2. Select posts published in the last 48 hours by default (configurable) that are not in the success ledger. Validate `LYNKCO_COMMENT_MAX_PER_RUN` as an integer from 1 to 10 (default 1). Selection and de-duplication happen before model calls, so a retry does not spend tokens on already commented posts. A missing/unparseable publish time is not treated as new.
3. Send only public post text and at most three public image URLs to `https://api.chatanywhere.tech/v1/chat/completions`. Read the API key from `CHATANYWHERE_API_KEY`; use a configurable vision-capable model, with `gpt-4o-mini` as the documented default. The prompt asks for a specific Chinese comment of at most 80 characters grounded in the post, and treats post content as data rather than instructions. Reject empty, truncated, overlong, or clearly unrelated model output. If neither text nor accessible images can be analyzed, skip the post. The local dry-run can incur model API charges, but never sends the comment POST.
4. Build the comment request from the live post metadata and the attached sample's protocol shape: `POST /app/explore/home-page/comment/create`, query fields including `relaCode`/`relaTypeCode`, JSON body, exact body-byte `Content-MD5`, and the captured iOS-style `X-Ca-Signature-Headers` ordering including `token`. Use the existing native-signature helper, not a copied signature or a new signing algorithm. Include optional author/account fields only when obtained from the feed or documented configuration. Check HTTP status, business `code`, and `data.commentId` before recording success.
5. Do not automatically retry a comment POST after a timeout or other ambiguous transport result. Report an uncertain result, leave it out of the success ledger, and stop publishing further comments in that run. This is best-effort de-duplication; a crash between server acceptance and ledger persistence still needs a future server-side comment lookup to eliminate completely.

## State, Entrypoints, and Notifications

- `lynkco_comment.py` exposes small units for feed normalization, eligibility, prompt/media construction, signed posting, and a run coordinator. A CLI supports offline fixture tests, local `--dry-run` (fetch and generate but never publish), and an explicit publish mode. Local success IDs are persisted in an ignored state file after each confirmed POST. No copied captured IDs or comment text are defaults. Any current-user ID or device ID required by the captured iOS request is supplied by documented configuration; author fields are sourced from each live post.
- A separate `.github/workflows/comment-task.yml` runs daily and supports manual dispatch with dry-run as the manual default. It restores the newest state artifact even if its run ended in failure after some confirmed comments, serializes runs via workflow concurrency, and uploads the updated state even if a later item fails. Missing state on the first run is normal; an API/auth failure while loading existing state fails closed. The workflow uses only Actions Secrets for LynkCo credentials, ChatAnywhere API key, and optional Bark key. It must not alter the sign/share workflow.
- Reuse `send_bark_notification` with a dedicated comment group. A run summary includes confirmed, skipped, failed, and uncertain counts plus safe post titles/IDs, without credentials or full request/response bodies. Bark failure is reported but does not change a confirmed posting result. Dry-run is clearly labeled and never counted as posted.
- Run validation in this order: offline unit tests; local Python CLI with fixture; local live read/model `--dry-run` when credentials are present; only then a controlled publish check if explicitly enabled; finally add and validate the Actions workflow. No live posting is required for unit tests.

## GRIC and Documentation Corrections

- Make `geely_env.json.example` a valid copyable configuration: empty required secrets, actual public defaults or omitted optional fields, no explanatory prose as values. Document each field outside JSON.
- Honor environment-variable precedence for device and vehicle fields. For implicit JWT selection call the existing `load_jwt()` path, including cache expiry/refresh, while preserving explicit authorization precedence. Keep the auth bootstrap independent of a pre-existing GRIC JWT.
- Preserve custom `vehicle.aesKey`/`aesIv` and other vehicle keys when `vehicle-init` updates identifier/series. Merge `extract_all_constants.py` results with an existing `extracted_constants.json`; an empty or failed extraction must not erase prior results.
- Repair the dangling TODO design-document link without reverting unrelated user deletions. Align GRIC usage documentation with actual config and renewal behavior; add a comment-task guide covering Secrets, configuration, schedule, state, dry-run, Bark, and known limits.

## Verification and Acceptance

- Use offline tests with fake sessions and redacted fixtures for signature bytes/query ordering, sample-shaped comment payload and success response, all text/image post types, missing metadata, model errors, media failures, duplicate IDs, configurable cap, dry-run, ambiguous POST, Bark failure, and state restore/persistence. No test contacts LynkCo, ChatAnywhere, or Bark.
- Reproduce and regress the five reviewed GRIC issues: copyable example headers, JWT renewal, environment override, AES override preservation, and extraction-file merge. Check the TODO link and workflow YAML. `git diff --check` must pass.
- A configured local run and Actions run should use the same Python entrypoint. Missing/expired credentials or unsupported upstream response shapes fail with actionable messages and never fall back to captured or stale values. Live endpoint behavior and future upstream changes cannot be guaranteed by offline tests; record which network checks were actually performed.

## Trade-offs

Automatic publishing matches the requested workflow but puts model output in a public comment without human review. The per-run cap, bounded prompt, output validation, dry-run, and fail-closed branches limit mistakes; they cannot guarantee the quality of every generated comment. Images depend on URLs reachable by the model provider. Durable artifact state avoids writing a repository branch, but is not an exactly-once transaction with the upstream comment endpoint.

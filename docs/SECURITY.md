# Security

Security decisions, implementation details, and known limitations for `chat-sdk-python`.

## Webhook Verification Per Platform

Every adapter verifies incoming webhook requests before processing. The verification method depends on the platform.

### Slack

- **Method**: HMAC-SHA256 signature verification
- **Header**: `X-Slack-Signature` (v0 format: `v0=<hex-digest>`)
- **Timestamp**: `X-Slack-Request-Timestamp` (rejected if > 5 minutes old to prevent replay attacks)
- **Signing key**: `signing_secret` from the Slack app configuration
- **Base string**: `v0:{timestamp}:{body}`
- **Comparison**: Timing-safe via `hmac.compare_digest()`

```python
# Simplified verification flow in Slack adapter
base_string = f"v0:{timestamp}:{body}"
expected = "v0=" + hmac.new(
    signing_secret.encode(), base_string.encode(), hashlib.sha256
).hexdigest()
if not hmac.compare_digest(expected, signature_header):
    raise AuthenticationError("slack", "Invalid signature")
```

### Discord

- **Method**: Ed25519 signature verification (using PyNaCl)
- **Headers**: `X-Signature-Ed25519`, `X-Signature-Timestamp`
- **Public key**: `public_key` from the Discord application
- **Verified message**: `{timestamp}{body}`
- **Library**: `nacl.signing.VerifyKey`

### Teams

- **Method**: JWT token validation (RS256)
- **Header**: `Authorization: Bearer <jwt>`
- **Issuer**: `https://api.botframework.com` or `https://sts.windows.net/{tenant-id}/`
- **Audience**: The app's `app_id`
- **JWKS endpoint**: `https://login.botframework.com/v1/.well-known/openidconfiguration`
- **Key rotation**: JWKS keys are cached with a configurable TTL. See "Known Limitations" below.

The Teams adapter implements its own JWT validation rather than using the Microsoft Bot Framework SDK because there is no maintained async Python equivalent. The validation includes issuer checking, audience verification, expiry checking, and signature verification against the JWKS-provided RSA public keys.

### Telegram

- **Method**: Secret token comparison
- **Header**: `X-Telegram-Bot-Api-Secret-Token`
- **Comparison**: Timing-safe via `hmac.compare_digest()`
- **Caveat**: If `secret_token` is not configured, webhook verification is silently skipped. See "Known Limitations" below.

### WhatsApp (Meta Cloud API)

- **Method**: HMAC-SHA256 signature verification
- **Header**: `X-Hub-Signature-256` (format: `sha256=<hex-digest>`)
- **Signing key**: `app_secret` from the Meta app
- **Comparison**: Timing-safe via `hmac.compare_digest()`

### Google Chat

- **Method**: Google-issued JWT token verification
- **Header**: `Authorization: Bearer <jwt>`
- **Verification**: `google.oauth2.id_token.verify_token()` from the `google-auth` library
- **Audience**: The Google Cloud project number
- **Fallback**: If `use_application_default_credentials` is enabled, the adapter trusts Google Cloud's internal authentication.

### GitHub

- **Method**: HMAC-SHA256 signature verification
- **Header**: `X-Hub-Signature-256` (format: `sha256=<hex-digest>`)
- **Signing key**: `webhook_secret` from the GitHub app
- **Comparison**: Timing-safe via `hmac.compare_digest()`

### Linear

- **Method**: HMAC-SHA256 signature verification
- **Header**: `Linear-Signature`
- **Signing key**: `webhook_secret` from the Linear app
- **Comparison**: Timing-safe via `hmac.compare_digest()`

## SSRF Protections

### Teams `service_url` Validation

The Teams adapter receives a `serviceUrl` in every activity payload. This URL is used to send replies back to Teams. The adapter validates that the URL matches the expected Microsoft domains:

- `https://*.botframework.com/`
- `https://smba.trafficmanager.net/`

Requests to arbitrary URLs (which could target internal services) are rejected.

### WhatsApp Media URL Validation

When downloading media attachments from WhatsApp messages, the adapter validates that media URLs point to Meta's CDN domains before fetching them:

- `https://*.whatsapp.net/`
- `https://scontent*.xx.fbcdn.net/`

### Slack `response_url` Validation

Slack's `response_url` (used for responding to slash commands and interactive messages) is validated to ensure it points to Slack's domains:

- `https://hooks.slack.com/`

## Crypto: AES-256-GCM for Slack Token Encryption

The Slack adapter supports multi-workspace OAuth installations. Bot tokens for each workspace can be encrypted at rest using AES-256-GCM.

### Implementation (`adapters/slack/crypto.py`)

```python
def encrypt_token(plaintext: str, key: bytes) -> EncryptedTokenData:
    iv = os.urandom(12)  # 96-bit random IV
    aesgcm = AESGCM(key)
    ct_with_tag = aesgcm.encrypt(iv, plaintext.encode(), None)
    ciphertext = ct_with_tag[:-16]  # everything except last 16 bytes
    tag = ct_with_tag[-16:]         # 128-bit auth tag
    return EncryptedTokenData(
        iv=base64.b64encode(iv),
        data=base64.b64encode(ciphertext),
        tag=base64.b64encode(tag),
    )
```

- **Algorithm**: AES-256-GCM (authenticated encryption)
- **IV**: 12 bytes from `os.urandom()` (CSPRNG)
- **Auth tag**: 16 bytes (128-bit)
- **Key format**: 32-byte key, accepted as 64-char hex or 44-char base64
- **Library**: `cryptography` (via the `crypto` extra)
- **Lazy import**: `cryptography` is imported inside the encrypt/decrypt functions, not at module level

### Key Management

The encryption key is provided via the `encryption_key` config option on the Slack adapter. It is the deployer's responsibility to:

1. Generate a strong 256-bit key (`python -c "import secrets; print(secrets.token_hex(32))"`)
2. Store it securely (environment variable, secrets manager)
3. Rotate it by re-encrypting stored tokens with the new key

## Timing-Safe HMAC Comparisons

All webhook signature verifications use `hmac.compare_digest()` for constant-time comparison. This prevents timing side-channel attacks where an attacker could determine the correct signature byte-by-byte by measuring response times.

This applies to:
- Slack signing secret verification
- GitHub webhook signature verification
- WhatsApp webhook signature verification
- Linear webhook signature verification
- Telegram secret token verification
- Lock token comparison in state adapters

## Lock Token Generation

Lock tokens serve as proof of ownership. A holder must present the correct token to release or extend a lock.

- **Generator**: `secrets.token_hex(16)` -- 128 bits of cryptographic randomness
- **Format**: `{backend}_{timestamp_ms}_{hex}` (e.g., `mem_1700000000000_a1b2c3d4...`)
- **Why CSPRNG**: If tokens were predictable, a malicious actor (or a bug in a concurrent process) could forge a token and release someone else's lock, causing data races.

## Known Limitations

### Telegram: Silent Skip When No `secret_token`

If the `TelegramAdapterConfig` does not include a `secret_token`, the Telegram adapter processes webhooks without any authentication. This is because Telegram does not require webhook verification -- it is optional.

**Risk**: Anyone who discovers the webhook URL can send fake updates.

**Mitigation**: Always configure `secret_token` in production. The adapter should log a warning when operating without verification, but this is not currently implemented.

### Teams: JWKS Key Rotation Window

The Teams adapter caches JWKS (JSON Web Key Set) public keys to avoid fetching them on every request. When Microsoft rotates its signing keys, there is a window where:

1. The old key is still in the cache
2. Microsoft starts signing with the new key
3. Verification fails until the cache expires and new keys are fetched

**Risk**: Legitimate webhooks may be rejected during key rotation (~minutes).

**Mitigation**: The cache TTL is set to 24 hours by default. If verification fails with a cached key, the adapter should (but does not currently) attempt a cache refresh before rejecting the request. This is a known improvement area.

### No Input Sanitization on Card Content

Card element content (titles, text, button labels) is passed through to platform APIs without HTML escaping. Each platform's API is responsible for sanitizing output. This is intentional -- double-escaping would produce visible escape sequences.

**Risk**: If a platform API has an XSS vulnerability, malicious card content could exploit it.

**Mitigation**: Trust platform APIs to handle their own output encoding. Do not pass user-controlled input directly into card content without application-level sanitization.

## What to Audit Before Production Deployment

1. **Webhook secrets are configured** for every adapter in use. Never deploy with empty or default secrets.

2. **Encryption key is set** for Slack multi-workspace OAuth if bot tokens are persisted (Redis/Postgres state backends).

3. **Telegram `secret_token` is set**. Without it, anyone can submit fake updates to your webhook endpoint.

4. **State backend is production-grade**. `MemoryStateAdapter` emits a warning in production environments but does not prevent usage. Use Redis or PostgreSQL.

5. **HTTPS is enforced** on all webhook endpoints. Webhook signatures are useless if the request body can be observed and replayed over HTTP.

6. **Rate limiting** is configured at the HTTP layer (reverse proxy / load balancer). The SDK raises `RateLimitError` / `AdapterRateLimitError` when platform rate limits are hit, but does not implement client-side rate limiting.

7. **Dependency audit**: Run `pip audit` or equivalent to check for known vulnerabilities in dependencies (`cryptography`, `slack-sdk`, `pynacl`, `aiohttp`, etc.).

8. **Log level**: Set to `info` or `warn` in production. `debug` level logs message content and adapter payloads, which may contain sensitive data.

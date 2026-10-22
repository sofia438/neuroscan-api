# NeuroScan API reference

The API uses JSON and bearer tokens. Once deployed, set the base URL to:

    https://YOUR-RENDER-SERVICE.onrender.com/api/v1

Interactive OpenAPI documentation is available at https://YOUR-RENDER-SERVICE.onrender.com/docs.

## CORS

The API currently allows requests from all browser origins, methods, and headers. Authentication uses bearer tokens in the Authorization header; cookie credentials are disabled. CORS only controls whether browsers allow a page to read a response. It is not authentication or access control.

## Endpoints

| Method | Path | Auth | Purpose | Request JSON |
|---|---|---|---|---|
| POST | /auth/register | No | Create an account and email a verification code | {"email":"person@example.com","password":"at-least-10-characters"} |
| POST | /auth/verify-email | No | Confirm the email; returns tokens | {"email":"person@example.com","code":"123456"} |
| POST | /auth/resend-code | No | Resend the verification code | {"email":"person@example.com"} |
| POST | /auth/login | No | Sign in; returns tokens | {"email":"person@example.com","password":"your-password"} |
| POST | /auth/refresh | No | Rotate a refresh token | {"refresh_token":"<refresh_token>"} |
| POST | /auth/logout | Bearer | Revoke a refresh token | {"refresh_token":"<refresh_token>"} |
| GET | /auth/me | Bearer | Return the signed-in user | None |
| POST | /chat | Bearer | Start or continue a Llama 4 Scout text/image chat | {"message":"Describe this","image_urls":["https://images.example.com/photo.jpg"]} |
| GET | /chat/conversations | Bearer | List this user's conversations | None |
| GET | /chat/conversations/{conversation_id}/messages | Bearer | Read messages in one of this user's conversations | None |
| GET | /health | No | Liveness check | None |
| GET | /ready | No | Database readiness check; returns 503 if the database is unavailable | None |

Paths above are relative to /api/v1, except /health and /ready, which are at the service root.

### Registration and login flow

1. Call POST /auth/register.
2. Ask the user to enter the six-digit email code and call POST /auth/verify-email.
3. Store the returned access_token and refresh_token securely in the client.
4. Send the access token as Authorization: Bearer <access_token> to protected endpoints.
5. When the access token expires, call POST /auth/refresh and replace both stored tokens with the returned pair.
6. Call POST /auth/logout with the access token and current refresh token when the user signs out.

Passwords must be 10–128 characters. Verification codes expire after 10 minutes by default; resends have a 60-second cooldown and verification is limited to five attempts. These values are configurable in the environment.

### Browser example

    const API = "https://YOUR-RENDER-SERVICE.onrender.com/api/v1";

    const response = await fetch(API + "/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });

    const tokens = await response.json();

    const profileResponse = await fetch(API + "/auth/me", {
      headers: { Authorization: "Bearer " + tokens.access_token },
    });
    const profile = await profileResponse.json();

### Chat example

    const response = await fetch(API + "/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + accessToken,
      },
      body: JSON.stringify({
        message: "Describe this image",
        image_urls: ["https://images.example.com/photo.jpg"],
        conversation_id: null,
      }),
    });

    // { "conversation_id": 1, "reply": "..." }
    const result = await response.json();

### Response notes

- Registration and code-resend return HTTP 202 with a message.
- Successful verification, login, and refresh return access_token, refresh_token, token_type, and expires_in.
- Logout returns HTTP 204 with no response body.
- Chat returns conversation_id and the assistant's reply.
- Chat accepts up to four publicly reachable HTTPS image URLs per message. OpenRouter's model provider must be able to fetch each URL; image URLs are stored with chat history and sent to OpenRouter with the message.
- Error responses use FastAPI's JSON format, for example {"detail":"Invalid email or password"}.

Treat access and refresh tokens as secrets. Do not place them in URLs or log them. Avoid credentials: "include" in browser requests; this API uses bearer tokens rather than cookies.

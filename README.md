# NeuroScan API

FastAPI backend with Neon PostgreSQL, email OTP registration through Brevo, bearer access tokens, rotating refresh tokens, and text/image chat through OpenRouter's Llama 4 Scout.

## Local setup

1. Use Python 3.12 or newer and create a virtual environment.
2. Install dependencies with pip install -r requirements.txt.
3. Copy .env.example to .env and fill in DATABASE_URL, JWT_SECRET, Brevo credentials, and the OpenRouter API key. The Neon URL should include sslmode=require; prefer Neon’s pooled URL for the web app.
4. Start the API with uvicorn main:app --reload.
5. Open /docs for the interactive API reference.

For endpoint details and copyable request examples, see [API_REFERENCE.md](API_REFERENCE.md).

Never commit .env or share the database URL in source control. Set secrets in Render's Environment settings for deployment.

## API flow

1. POST /api/v1/auth/register with email and a password of at least 10 characters.
2. POST /api/v1/auth/verify-email with the emailed six-digit code. This returns access and refresh tokens.
3. Send Authorization: Bearer <access_token> to protected endpoints.
4. POST /api/v1/auth/refresh rotates a refresh token. POST /api/v1/auth/logout revokes it.
5. POST /api/v1/chat with a message starts a conversation; include conversation_id to continue one. Add up to four publicly accessible HTTPS image_urls to send images with the message.
6. Open /playground for a simple browser UI to register, verify email, log in, and send chat messages with an image file.

Other endpoints: POST /api/v1/auth/login, POST /api/v1/auth/resend-code, GET /api/v1/auth/me, GET /api/v1/chat/conversations, GET /api/v1/chat/conversations/{id}/messages, /health, and /ready.

## Render

Connect the repository to Render and use the included render.yaml Blueprint. Enter the Neon connection string, Brevo API key and sender email, and OpenRouter key when prompted. CORS currently allows all origins. The service uses Render's PORT and /health check. Free web services can spin down when idle; the first request after inactivity may take longer.

## Current implementation notes

- On first startup, SQLAlchemy creates the tables. For ongoing production schema changes, add Alembic migrations before evolving the schema.
- Email sending and OpenRouter requests use bounded timeouts. Provider credentials are read from environment variables.
- The AI system instructions live in app/prompts/assistant_system.txt.
- Image URLs and uploaded image data are stored in the user's chat history in Neon and forwarded to OpenRouter.
- Add request rate limiting and an abuse control at the edge before opening public registration broadly.

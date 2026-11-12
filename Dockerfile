# Dockerfile — wxO embedded chat + Entra ID OBO portal
FROM python:3.12-slim

WORKDIR /app

# Python dependencies (wheels only, no compiler needed)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code and RSA keys.
#   wxo-client-private.pem : YOUR private key (signs the wxO JWT) — never committed,
#                            it must exist locally when you build the image.
#   wxo-ibm-public.pem     : IBM public key of your wxO instance (encrypts user_payload).
COPY main.py .
COPY wxo-client-private.pem .
COPY wxo-ibm-public.pem .

# Environment variables are NOT baked into the image: they are passed by
# Code Engine (see deploy-to-code-engine.ps1 / .sh) or by `docker run --env-file .env`.
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

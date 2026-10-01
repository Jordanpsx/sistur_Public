# Internal API Authentication

The internal application API (`/api/internal/`) is secured using a Bearer token mechanism.

## QR_SERVICE_TOKEN

Authentication is facilitated via the `QR_SERVICE_TOKEN` environment variable. This token must be provided in the request headers to access internal routes, such as the QR code scanner (`/api/internal/ponto/qr-scan`) and reservation ingest endpoint (`/api/internal/reservas/ingest`).

### Implementation Details

1. **Environment Variables**: The `QR_SERVICE_TOKEN` variable is passed from the host machine to the `sistur-flask` Docker container through the `docker-compose.yml` file configuration.
2. **Decorator Usage**: Target Python functions handling the internal routes are decorated with `@require_internal_token`.
3. **Missing Token Strategy**: If `QR_SERVICE_TOKEN` is unavailable in the environment, the script will log a descriptive warning and deny access with `401 Unauthorized`.
4. **Header Format**: Every request to the internal API must include the header `Authorization: Bearer <VALUE>`.

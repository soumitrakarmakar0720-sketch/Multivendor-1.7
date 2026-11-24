# Fresh staging deployment (partial 11.0 foundation)
This package retains the 10.0 source and adds merchant configuration records and fresh database bootstrap. It is NOT the completed 11.0 release. Restaurant POS, table QR, unified customer accounts, refund wallets, vendor payments, payout reconciliation, subscription renewals and expiry enforcement are not implemented. Gateway preferences do not enable collection. Existing legacy routes are retained and are not tenant-enabled.
1. Create a separate Neon project and copy its connection string (SSL required). Do not use your live database.
2. Upload extracted files at the root of a new private GitHub repository.
3. Create a Render Python Web Service. Build: pip install -r requirements.txt. Start: python fresh_start.py. Health: /health.
4. Set DATABASE_URL, SECRET_KEY (random), COOKIE_SECURE=1, ADMIN_USERNAME, ADMIN_PASSWORD (12+ characters), PLATFORM_ADMIN_PASSWORD_HASH (Werkzeug hash), CASHFREE_ENV=sandbox. Leave gateway credentials unset for initial testing.
5. Bootstrap creates all model tables and the legacy admin if absent. It does not import old data or upgrade existing schemas. Use a fresh database only.
6. Open /merchant/platform, /merchant/register, /merchant/login. Approve only after offline verification. Test two merchants separately.
Generate the platform hash locally after installing Werkzeug: python -c "from werkzeug.security import generate_password_hash; from getpass import getpass; print(generate_password_hash(getpass()))"
Validation: Python syntax only; runtime tests require dependencies and a test database. Do not expose staging to real customers or use real payment credentials.

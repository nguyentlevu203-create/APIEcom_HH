"""Fixed, non-secret configuration for the Shopee Open Platform V2 integration.
No credential lives in this file."""

LIVE_PARTNER_ID = 2044177
TEST_PARTNER_ID = 1243463
KEYCHAIN_SERVICE = "HH_SHOPEE_OPEN_API"

API_HOST = "https://partner.shopeemobile.com"
TOKEN_GET_PATH = "/api/v2/auth/token/get"
TOKEN_REFRESH_PATH = "/api/v2/auth/access_token/get"
AUTH_PARTNER_PATH = "/api/v2/shop/auth_partner"

LIVE_REDIRECT_URL = "https://lepetitmarseillais.vn/"

# Refresh proactively once the access token has this little time left.
REFRESH_BUFFER_SECONDS = 2 * 60 * 60  # 2 hours (Shopee access tokens live 4h)

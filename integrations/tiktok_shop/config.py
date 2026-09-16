"""Fixed, non-secret configuration for this integration. No credential here."""

APP_KEY = "6jh02dvvivnis"
KEYCHAIN_SERVICE = "HH_TIKTOK_SHOP_OPEN_API"

SHOP_EXPECTED_NAME = "Le Petit Marseillais Vietnam"
SHOP_EXPECTED_REGION = "VN"

REQUESTED_P0_SCOPES = [
    "seller.authorization.info",
    "seller.order.info",
    "seller.finance.info",
    "data.shop_analytics.public.read",
    "seller.product.basic",
    "seller.return_refund.basic",
    "seller.affiliate_collaboration.read",
    "data.bestselling.public.read",
]

# Refresh proactively once the access token has this little time left.
REFRESH_BUFFER_SECONDS = 24 * 60 * 60  # 24 hours, per Phase 2 spec

from dataclasses import dataclass, field

from app.core.config import Settings


@dataclass(frozen=True)
class PublicRule:
    """A request matching this rule needs no token. ``methods=None`` means any method."""

    path_prefix: str
    methods: frozenset[str] | None = None


@dataclass(frozen=True)
class Route:
    name: str
    prefix: str
    upstream: str
    public: tuple[PublicRule, ...] = field(default_factory=tuple)

    def is_public(self, method: str, path: str) -> bool:
        return any(
            path.startswith(rule.path_prefix) and (rule.methods is None or method in rule.methods)
            for rule in self.public
        )


GET = frozenset({"GET"})


def build_routes(s: Settings) -> list[Route]:
    routes = [
        Route(
            "auth",
            "/api/v1/auth",
            s.auth_url,
            public=(
                PublicRule("/api/v1/auth/register"),
                PublicRule("/api/v1/auth/login"),
                PublicRule("/api/v1/auth/refresh"),
            ),
        ),
        Route("user", "/api/v1/users", s.user_url),
        # Browsing the catalogue is public; changing it needs a token (admin checked by the service)
        Route(
            "product",
            "/api/v1/products",
            s.product_url,
            public=(PublicRule("/api/v1/products", GET),),
        ),
        Route(
            "category",
            "/api/v1/categories",
            s.product_url,
            public=(PublicRule("/api/v1/categories", GET),),
        ),
        Route("inventory", "/api/v1/inventory", s.inventory_url),
        Route("cart", "/api/v1/cart", s.cart_url),
        Route("order", "/api/v1/orders", s.order_url),
        Route("payment", "/api/v1/payments", s.payment_url),
        # Dev stub so the gateway can be exercised before the real services exist
        Route(
            "template",
            "/api/v1/template",
            s.template_url,
            public=(PublicRule("/api/v1/template"),),
        ),
    ]
    # Longest prefix wins
    return sorted(routes, key=lambda r: len(r.prefix), reverse=True)


def match_route(routes: list[Route], path: str) -> Route | None:
    for route in routes:
        if path == route.prefix or path.startswith(route.prefix + "/"):
            return route
    return None

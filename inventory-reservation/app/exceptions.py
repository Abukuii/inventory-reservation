class DomainError(Exception):
    """Base class for expected business-rule failures (mapped to 4xx)."""
    http_status = 400


class InsufficientStockError(DomainError):
    http_status = 409

    def __init__(self, product_id: int):
        self.product_id = product_id
        super().__init__(f"insufficient available stock for product_id={product_id}")


class NotFoundError(DomainError):
    http_status = 404


class InvalidTransitionError(DomainError):
    """Raised when confirm/cancel is attempted from a state that forbids it."""
    http_status = 409

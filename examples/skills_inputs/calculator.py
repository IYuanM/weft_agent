"""Small fixture for the code documentation skill; no third-party dependencies."""


def total_price(price: float, quantity: int, discount: float = 0.0) -> float:
    """Return a rounded total; discount is a fraction between zero and one."""
    if price < 0 or quantity < 1 or not 0 <= discount <= 1:
        raise ValueError("Invalid price, quantity or discount")
    return round(price * quantity * (1 - discount), 2)

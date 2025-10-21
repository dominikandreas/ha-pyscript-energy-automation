def get_price(hour: int, minute: int) -> float:
    """Return price based on time of day."""
    if (hour == 23 and minute >= 30) or (hour <= 4) or (hour == 5 and minute < 30):
        return 0.175
    return 0.255


def is_low_price(price: float) -> bool:
    """Check if the price is considered low."""
    return price < 0.2

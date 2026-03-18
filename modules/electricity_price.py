@pyscript_compile
def get_price(hour: int, minute: int) -> float:
    """Return price based on time of day."""
    t = hour + minute / 60
    if 0 <= t < 5:
        return 0.1775
    return 0.2875


@pyscript_compile
def is_low_price(price: float) -> bool:
    """Check if the price is considered low."""
    return price < 0.2

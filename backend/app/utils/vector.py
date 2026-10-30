def normalize_distance_to_similarity(distance: object, collection_space: str = "") -> float:
    try:
        value = float(distance)
    except (TypeError, ValueError):
        return 0.0
    if value != value:
        return 0.0
    space = (collection_space or "").lower()
    if space in {"cosine", "ip"}:
        return max(0.0, min(1.0, 1.0 - value))
    if space == "l2":
        return max(0.0, min(1.0, 1.0 / (1.0 + value)))
    if 0.0 <= value <= 1.0:
        return max(0.0, min(1.0, 1.0 - value))
    return max(0.0, min(1.0, 1.0 / (1.0 + value)))

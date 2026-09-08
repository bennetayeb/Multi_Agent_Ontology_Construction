def format_triplet_id(index):
    if index < 1 or index > 999_999:
        raise ValueError(f"index triplet 1–999999 : {index}")
    return f"{index:06d}"

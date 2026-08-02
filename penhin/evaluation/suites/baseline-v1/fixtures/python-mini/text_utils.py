def normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def word_count(text: str) -> int:
    return len(text.split(" "))

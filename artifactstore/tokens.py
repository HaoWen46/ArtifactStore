try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")

    def estimate(text: str) -> int:
        return len(_enc.encode(text))
except Exception:
    def estimate(text: str) -> int:
        return max(1, len(text) // 4)

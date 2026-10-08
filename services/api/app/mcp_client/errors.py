"""只读取状态码分类；不将包含密钥的 SDK 异常原文回传。"""


def rate_limited(error):
    pending, seen = [error], set()
    for _ in range(32):
        if not pending:
            return False
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(getattr(current, "response", None), "status_code", None) == 429:
            return True
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)
    return False

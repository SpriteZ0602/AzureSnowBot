"""把实际发送结果写入历史，保留已确认送达的片段。"""


def record_delivery(result, text: str, append) -> bool:
    status = getattr(result, "status", "unknown")
    if status == "sent":
        if text:
            append({"role": "assistant", "content": text})
        return True
    delivered = getattr(result, "delivered_text", "")
    if isinstance(delivered, str) and delivered:
        append({"role": "assistant", "content": delivered})
    state = "失败" if status == "failed" else "状态未知"
    append({"role": "assistant", "content": f"[消息发送{state}；完整回复未确认送达，不应声称用户已收到，也不要自动重复发送。]"})
    return False

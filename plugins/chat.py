import httpx
from nonebot import on_message, get_driver
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
from nonebot.exception import FinishedException
from nonebot.log import logger

# 从 .env 读取配置
config = get_driver().config
OPENAI_API_KEY: str = getattr(config, "openai_api_key", "")
OPENAI_BASE_URL: str = getattr(config, "openai_base_url", "https://api.openai.com/v1")
OPENAI_MODEL: str = getattr(config, "openai_model", "gpt-5.4")

SYSTEM_PROMPT = "你是一个有用的助手。请用简洁的中文回答用户的问题。"

chat = on_message(priority=99, block=False)


@chat.handle()
async def handle_chat(event: PrivateMessageEvent):
    user_input = event.get_plaintext().strip()
    if not user_input:
        return

    if not OPENAI_API_KEY:
        await chat.finish("⚠️ 未配置 OpenAI API Key，请联系管理员。")

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{OPENAI_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"].strip()
            await chat.finish(reply)
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenAI API 错误: {e.response.status_code} {e.response.text}")
        await chat.finish(f"⚠️ API 请求失败 ({e.response.status_code})")
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"ChatGPT 插件异常: {e}")
        await chat.finish("⚠️ 请求出错，请稍后再试。")

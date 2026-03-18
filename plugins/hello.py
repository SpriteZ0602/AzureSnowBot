from nonebot import on_message
from nonebot.adapters.onebot.v11 import PrivateMessageEvent

hello = on_message(priority=10, block=True)


@hello.handle()
async def handle_hello(event: PrivateMessageEvent):
    if event.get_plaintext().strip() == "你好":
        await hello.finish("你好")
